"""
Brookhaven Property Website
Flask app serving SEO-optimised property pages, hamlet area pages,
a homepage with search, lead capture, sitemap, and robots.txt.

Setup:
    cd website
    pip install flask psycopg2-binary
    export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/brookhaven"
    python app.py

Production:
    gunicorn -w 4 -b 0.0.0.0:8000 app:app
"""

import os
import re
import math
import time
import smtplib
import logging
import threading
import requests as http_requests
from email.message import EmailMessage
from datetime import datetime, timezone
from flask import (
    Flask, render_template, request, jsonify,
    abort, Response, redirect, url_for
)
import psycopg2
import psycopg2.extras

# ── Configuration ─────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/brookhaven"
)

BROKER = {
    "name":        "James Folk",           # ← replace
    "title":       "Licensed Real Estate Salesperson",
    "license":     "NY License #000000",  # ← replace
    "phone":       "(631) 327-0064",      # ← replace
    "email":       "jamesfolk1@gmail.com",  # ← replace
    "photo":       "broker.jpg",          # drop file in static/img/
    "brokerage":   "Your Brokerage Name", # ← replace
    "tagline":     "Your local Brookhaven real estate expert.",
}

SITE_NAME   = "Brookhaven Home Values"
SITE_DOMAIN = os.environ.get("SITE_DOMAIN", "http://localhost:5000")  # no trailing slash

PROPERTIES_PER_PAGE = 30

# ── Search cache ──────────────────────────────────────────────────────────────
# Simple in-process dict cache: {query: (timestamp, results)}.
# TTL 5 minutes, max 500 entries. Shared across requests within one worker.

_search_cache: dict = {}
_search_cache_lock = threading.Lock()
_SEARCH_CACHE_TTL  = 300   # seconds
_SEARCH_CACHE_MAX  = 500


def _search_cache_get(q: str):
    with _search_cache_lock:
        entry = _search_cache.get(q)
        if entry and (time.time() - entry[0]) < _SEARCH_CACHE_TTL:
            return entry[1]
        if entry:
            del _search_cache[q]
    return None


def _search_cache_set(q: str, data) -> None:
    with _search_cache_lock:
        if len(_search_cache) >= _SEARCH_CACHE_MAX:
            oldest = min(_search_cache, key=lambda k: _search_cache[k][0])
            del _search_cache[oldest]
        _search_cache[q] = (time.time(), data)


# ── Email ─────────────────────────────────────────────────────────────────────
# Resend (https://resend.com — free 3,000 emails/month):
#   RESEND_API_KEY  — API key from resend.com/api-keys
#   NOTIFY_EMAIL    — where to send lead alerts
#   NOTIFY_FROM     — verified sender address (defaults to "leads@resend.dev"
#                     which works without domain setup during testing)
#
# SMTP fallback (local dev only — may be blocked on cloud servers):
#   SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", "")
NOTIFY_FROM    = os.environ.get("NOTIFY_FROM", "leads@resend.dev")

SMTP_HOST     = os.environ.get("SMTP_HOST", "")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

if not RESEND_API_KEY and not SMTP_HOST:
    logging.warning("No email config found — lead notifications are disabled. "
                    "Set RESEND_API_KEY.")


def _build_body(lead: dict) -> str:
    return "\n".join([
        f"Name:     {lead['name']}",
        f"Email:    {lead['email']}",
        f"Phone:    {lead.get('phone') or '—'}",
        f"Address:  {lead.get('address') or '—'}",
        f"Timeline: {lead.get('timeline') or '—'}",
        f"Message:  {lead.get('message') or '—'}",
        f"Type:     {lead.get('lead_type', 'valuation')}",
    ])


def _resend_send(subject: str, body: str) -> None:
    resp = http_requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type":  "application/json",
        },
        json={
            "from":    NOTIFY_FROM,
            "to":      [NOTIFY_EMAIL],
            "subject": subject,
            "text":    body,
        },
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Resend {resp.status_code}: {resp.text}")


def _smtp_send(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.set_content(body)

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)


def _do_send(lead: dict) -> None:
    """Try Resend first, fall back to SMTP. Called from a daemon thread."""
    subject = f"New Lead: {lead['name']} — {lead.get('address') or 'no address'}"
    body    = _build_body(lead)
    try:
        if RESEND_API_KEY and NOTIFY_EMAIL:
            _resend_send(subject, body)
        elif SMTP_HOST and SMTP_USER and SMTP_PASSWORD and NOTIFY_EMAIL:
            _smtp_send(subject, body)
    except Exception:
        logging.exception("Failed to send lead notification email")


def send_lead_email(lead: dict) -> None:
    """Dispatch email in a background thread so the HTTP response is never delayed."""
    if not NOTIFY_EMAIL:
        return
    if not (RESEND_API_KEY or (SMTP_HOST and SMTP_USER and SMTP_PASSWORD)):
        return
    threading.Thread(target=_do_send, args=(lead,), daemon=True).start()

# ── App ───────────────────────────────────────────────────────────────────────

app = Flask(__name__)

# ── Database helpers ──────────────────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn

def query(sql, params=None, one=False):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()

def execute(sql, params=None):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
    finally:
        conn.close()

# ── Template context helpers ──────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    from datetime import datetime
    nav_areas = query("SELECT name, slug FROM areas ORDER BY name")
    return {
        "broker":     BROKER,
        "site_name":  SITE_NAME,
        "site_domain": SITE_DOMAIN,
        "nav_areas":  nav_areas,
        "now":        datetime.now(),
    }

def fmt_currency(value):
    if value is None:
        return "N/A"
    return f"${value:,.0f}"

def fmt_class(code):
    """Return a human-readable property class label."""
    CLASS_MAP = {
        "210": "Single Family",
        "215": "Single Family",
        "220": "Two Family",
        "230": "Three Family",
        "240": "Four or More Family",
        "250": "Rural Residential",
        "260": "Mobile Home",
        "270": "Mobile Home Park",
        "280": "Multi-use Residential",
        "300": "Vacant Land",
        "311": "Res. Vacant Land",
        "312": "Res. Land w/ Minor Improvements",
        "314": "Rural Vacant",
        "320": "Rural Vacant Land",
        "330": "Commercial Vacant",
        "340": "Industrial Vacant",
        "400": "Commercial",
        "411": "Supermarket",
        "431": "Auto Sales",
        "450": "Retail",
        "460": "Bank",
        "481": "Attached Row Building",
        "482": "Detached Row Building",
        "484": "One Story Small Structure",
        "486": "Minimart",
        "500": "Recreation / Entertainment",
        "600": "Community Service",
        "610": "Education",
        "620": "Religious",
        "630": "Human Services",
        "640": "Health Care",
        "650": "Government",
        "660": "Protective",
        "670": "Utility",
        "680": "Cultural / Recreation",
        "700": "Industrial",
        "800": "Public Services",
        "900": "Wild / Conservation",
    }
    if not code:
        return "Residential"
    prefix = code[:3]
    return CLASS_MAP.get(prefix, CLASS_MAP.get(code, f"Class {code}"))

app.jinja_env.globals["fmt_currency"] = fmt_currency
app.jinja_env.globals["fmt_class"]    = fmt_class

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Homepage — search bar + area cards + broker intro."""

    # Area stats
    areas = query("""
        SELECT
            a.name,
            a.slug,
            COUNT(p.id)                      AS total,
            ROUND(AVG(p.full_market_value))  AS avg_value,
            COUNT(p.id) FILTER (
                WHERE p.last_sale_date >= NOW() - INTERVAL '12 months'
            )                                AS sold_last_year
        FROM areas a
        LEFT JOIN properties p
            ON p.zip = ANY(a.zip_codes)
           AND p.full_market_value > 0
        GROUP BY a.name, a.slug
        ORDER BY a.name
    """)

    # Recent activity (last 10 sales)
    recent = query("""
        SELECT address, city, zip, slug, full_market_value, last_sale_date
        FROM properties
        WHERE last_sale_date IS NOT NULL
          AND full_market_value > 0
        ORDER BY last_sale_date DESC
        LIMIT 10
    """)

    total_properties = query("SELECT COUNT(*) AS n FROM properties", one=True)["n"]

    return render_template(
        "index.html",
        areas=areas,
        recent=recent,
        total_properties=total_properties,
        page_title=f"Brookhaven NY Home Values | {BROKER['name']}",
        meta_description=(
            f"Search home values for every property in Brookhaven, NY. "
            f"Get a free home valuation from {BROKER['name']}, "
            f"your local real estate expert."
        ),
    )


@app.route("/search")
def search():
    """Address-aware property search — returns JSON for the search bar.

    Matching rules (all case-insensitive):
      "70"      → addresses whose house number is exactly 70  (e.g. "70 Oak St")
                  i.e. address ILIKE '70 %'  — avoids matching "700 Main St"
      "70 ca"   → addresses that start with "70 ca"           (e.g. "70 Capri Dr")
                  i.e. address ILIKE '70 ca%'
      "oak"     → addresses / cities that contain "oak"
                  i.e. address ILIKE '%oak%'
      "11772"   → zip code prefix match
    """
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    cached = _search_cache_get(q)
    if cached is not None:
        return jsonify(cached)

    first_token = q.split()[0]

    if first_token.isdigit():
        if q == first_token:
            # Pure number → exact house-number match: "70" matches "70 Oak" not "700 Oak"
            address_pattern = q + " %"
        else:
            # Number + street prefix → prefix match: "70 ca" matches "70 Capri…"
            address_pattern = q + "%"
    else:
        # Text-only → substring match anywhere in address/city
        address_pattern = "%" + q + "%"

    city_zip_pattern = "%" + q + "%"

    rows = query("""
        SELECT address, city, zip, slug
        FROM properties
        WHERE address ILIKE %s
           OR city    ILIKE %s
           OR zip     ILIKE %s
        ORDER BY
            CASE WHEN address ILIKE %s THEN 0 ELSE 1 END,
            full_market_value DESC NULLS LAST
        LIMIT 50
    """, (address_pattern, city_zip_pattern, city_zip_pattern, address_pattern))

    data = [dict(r) for r in rows]
    _search_cache_set(q, data)
    return jsonify(data)


@app.route("/property/<slug>")
def property_page(slug):
    """Individual property page — the core SEO unit."""

    prop = query(
        "SELECT * FROM properties WHERE slug = %s", (slug,), one=True
    )
    if not prop:
        abort(404)

    # Nearby properties (within ~0.5 mile by lat/lon box)
    nearby = []
    if prop["latitude"] and prop["longitude"]:
        lat, lon = prop["latitude"], prop["longitude"]
        nearby = query("""
            SELECT address, city, zip, slug, full_market_value, property_class
            FROM properties
            WHERE slug != %s
              AND latitude  BETWEEN %s AND %s
              AND longitude BETWEEN %s AND %s
              AND full_market_value > 0
            ORDER BY full_market_value DESC
            LIMIT 6
        """, (slug, lat - 0.007, lat + 0.007, lon - 0.009, lon + 0.009))

    full_address = (
        f"{prop['address']}, {prop['city']}, {prop['state'] or 'NY'} {prop['zip']}"
    ).strip(", ")

    page_title       = f"{full_address} | Home Value & Details"
    meta_description = (
        f"{full_address} — estimated market value "
        f"{fmt_currency(prop['full_market_value'])}. "
        f"Get a free home valuation from {BROKER['name']}."
    )

    return render_template(
        "property.html",
        prop=prop,
        nearby=nearby,
        full_address=full_address,
        page_title=page_title,
        meta_description=meta_description,
        canonical=f"{SITE_DOMAIN}/property/{slug}",
    )


@app.route("/area/<slug>")
def area_page(slug):
    """Hamlet / area page."""

    area = query(
        "SELECT * FROM areas WHERE slug = %s", (slug,), one=True
    )
    if not area:
        abort(404)

    page_num = max(1, int(request.args.get("page", 1)))
    offset   = (page_num - 1) * PROPERTIES_PER_PAGE

    # Area market stats
    stats = query("""
        SELECT
            COUNT(*)                              AS total,
            ROUND(AVG(full_market_value))         AS avg_value,
            ROUND(MIN(full_market_value))         AS min_value,
            ROUND(MAX(full_market_value))         AS max_value,
            COUNT(*) FILTER (
                WHERE last_sale_date >= NOW() - INTERVAL '12 months'
            )                                     AS sold_last_year
        FROM properties
        WHERE zip = ANY(%s)
          AND full_market_value > 0
    """, (area["zip_codes"],), one=True)

    # Property listings for this area
    props = query("""
        SELECT address, city, zip, slug, full_market_value,
               assessed_value, property_class, last_sale_date
        FROM properties
        WHERE zip = ANY(%s)
          AND full_market_value > 0
        ORDER BY full_market_value DESC
        LIMIT %s OFFSET %s
    """, (area["zip_codes"], PROPERTIES_PER_PAGE, offset))

    total_pages = math.ceil((stats["total"] or 0) / PROPERTIES_PER_PAGE)

    page_title       = f"{area['name']}, NY Real Estate | Home Values"
    meta_description = (
        f"Browse {stats['total']} properties in {area['name']}, NY. "
        f"Average home value {fmt_currency(stats['avg_value'])}. "
        f"Get a free valuation from {BROKER['name']}."
    )

    return render_template(
        "area.html",
        area=area,
        stats=stats,
        props=props,
        page_num=page_num,
        total_pages=total_pages,
        page_title=page_title,
        meta_description=meta_description,
        canonical=f"{SITE_DOMAIN}/area/{slug}",
    )


# ── Lead capture ──────────────────────────────────────────────────────────────

@app.route("/api/leads", methods=["POST"])
def capture_lead():
    data = request.get_json(silent=True) or {}

    name    = (data.get("name")    or "").strip()
    email   = (data.get("email")   or "").strip()
    phone   = (data.get("phone")   or "").strip()
    address = (data.get("address") or "").strip()
    message = (data.get("message") or "").strip()
    timeline = (data.get("timeline") or "").strip()
    lead_type = (data.get("lead_type") or "valuation").strip()

    if not name or not email:
        return jsonify({"error": "Name and email are required."}), 400
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "Invalid email address."}), 400

    full_message = f"Timeline: {timeline}\n{message}".strip() if timeline else message

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO leads (name, email, phone, address, message, lead_type)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
            """, (name, email, phone or None, address or None,
                  full_message or None, lead_type))
            row = cur.fetchone()
    finally:
        conn.close()

    send_lead_email({
        "name":      name,
        "email":     email,
        "phone":     phone,
        "address":   address,
        "timeline":  data.get("timeline", ""),
        "message":   message,
        "lead_type": lead_type,
    })

    return jsonify({"success": True, "id": row["id"]}), 201


# ── Email diagnostics (remove before going public) ────────────────────────────

@app.route("/api/test-email")
def test_email():
    """Hit this URL to test email config. Returns a JSON report. Remove before going public."""
    report = {
        "backend":        "resend" if RESEND_API_KEY else "smtp",
        "RESEND_API_KEY": "set" if RESEND_API_KEY else "(not set)",
        "NOTIFY_EMAIL":   NOTIFY_EMAIL or "(not set)",
        "NOTIFY_FROM":    NOTIFY_FROM  or "(not set)",
        "SMTP_HOST":      SMTP_HOST    or "(not set)",
        "SMTP_PORT":      SMTP_PORT,
        "SMTP_USER":      SMTP_USER    or "(not set)",
        "SMTP_PASSWORD":  "set" if SMTP_PASSWORD else "(not set)",
    }

    try:
        if RESEND_API_KEY and NOTIFY_EMAIL:
            _resend_send(
                "Test email from Brookhaven lead site",
                "This is a test. Resend is working correctly.",
            )
            report["result"] = "OK — sent via Resend"
        elif SMTP_HOST and SMTP_USER and SMTP_PASSWORD and NOTIFY_EMAIL:
            _smtp_send(
                "Test email from Brookhaven lead site",
                "This is a test. SMTP is working correctly.",
            )
            report["result"] = "OK — sent via SMTP (fallback)"
        else:
            report["result"] = "SKIPPED — no complete email config found"
    except Exception as exc:
        report["result"] = f"FAILED — {type(exc).__name__}: {exc}"

    return jsonify(report)


# ── SEO utilities ─────────────────────────────────────────────────────────────

@app.route("/sitemap.xml")
def sitemap():
    """XML sitemap — homepage + all areas + all properties."""

    urls = [
        {"loc": SITE_DOMAIN + "/", "priority": "1.0", "changefreq": "weekly"},
    ]

    # Area pages
    areas = query("SELECT slug FROM areas ORDER BY slug")
    for a in areas:
        urls.append({
            "loc":        f"{SITE_DOMAIN}/area/{a['slug']}",
            "priority":   "0.8",
            "changefreq": "monthly",
        })

    # Property pages — chunked to keep memory low
    batch_size = 5000
    offset = 0
    while True:
        rows = query(
            "SELECT slug FROM properties ORDER BY id LIMIT %s OFFSET %s",
            (batch_size, offset)
        )
        if not rows:
            break
        for r in rows:
            urls.append({
                "loc":        f"{SITE_DOMAIN}/property/{r['slug']}",
                "priority":   "0.6",
                "changefreq": "yearly",
            })
        offset += batch_size

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    xml = render_template("sitemap.xml", urls=urls, now=now)
    return Response(xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    # Reads the static file so you can edit it directly;
    # the Sitemap line is updated at runtime from SITE_DOMAIN.
    static_path = os.path.join(app.static_folder, "robots.txt")
    with open(static_path) as f:
        lines = f.readlines()
    # Replace any Sitemap line with the live domain
    lines = [
        f"Sitemap: {SITE_DOMAIN}/sitemap.xml\n"
        if line.startswith("Sitemap:") else line
        for line in lines
    ]
    return Response("".join(lines), mimetype="text/plain")


# ── Error pages ───────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html", page_title="Page Not Found"), 404


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
