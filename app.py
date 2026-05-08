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
    "email":       "you@yourdomain.com",  # ← replace
    "photo":       "broker.jpg",          # drop file in static/img/
    "brokerage":   "Your Brokerage Name", # ← replace
    "tagline":     "Your local Brookhaven real estate expert.",
}

SITE_NAME   = "Brookhaven Home Values"
SITE_DOMAIN = os.environ.get("SITE_DOMAIN", "http://localhost:5000")  # no trailing slash

PROPERTIES_PER_PAGE = 30

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
    """Full-text property search — returns JSON for the search bar."""
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify([])

    rows = query("""
        SELECT address, city, zip, slug
        FROM properties
        WHERE to_tsvector('english', address || ' ' || COALESCE(city,'') || ' ' || COALESCE(zip,''))
              @@ plainto_tsquery('english', %s)
        ORDER BY full_market_value DESC NULLS LAST
        LIMIT 10
    """, (q,))

    return jsonify([dict(r) for r in rows])


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

    return jsonify({"success": True, "id": row["id"]}), 201


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
