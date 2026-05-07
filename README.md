# Brookhaven Property Website

Flask-powered property website that pulls live data from the pipeline database.
Every property gets its own SEO-optimised page. Area pages cover each Brookhaven
hamlet. A lead capture form saves directly to the `leads` table.

## Quick Start

```bash
cd website

# Create and activate the virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy your broker photo into static/img/broker.jpg (optional)

# Set environment variables
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/brookhaven"

# Start the development server
python app.py          # http://localhost:5000
```

The website has its own `.venv` — it is kept separate from the pipeline's
virtual environment so their dependencies don't interfere with each other.

## Filling In Your Details

Open `app.py` and edit the `BROKER` dict near the top:

```python
BROKER = {
    "name":      "Your Name",
    "title":     "Licensed Real Estate Salesperson",
    "license":   "NY License #000000",
    "phone":     "(631) 555-0000",
    "email":     "you@yourdomain.com",
    "photo":     "broker.jpg",          # file in website/static/img/
    "brokerage": "Your Brokerage Name",
    "tagline":   "Your local Brookhaven real estate expert.",
}
```

Then set your domain before deploying:

```bash
export SITE_DOMAIN="https://www.yourdomain.com"
```

## Pages

| URL | Description |
|-----|-------------|
| `/` | Homepage — search bar, area grid, recent sales, broker intro |
| `/property/<slug>` | Individual property page — details, valuation CTA, nearby comps |
| `/area/<slug>` | Hamlet page — market stats, all properties, paginated |
| `/search?q=<query>` | JSON search endpoint (used by the homepage search bar) |
| `/api/leads` | POST endpoint — saves lead to the `leads` table |
| `/sitemap.xml` | Full XML sitemap for Google Search Console |
| `/robots.txt` | Allows all crawlers, points to sitemap |

## Customising Styles

`static/css/style.css` is divided into labelled sections — one per page.
To restyle only the property page, edit the **Section 4** block.

To override styles for a specific page without touching the main stylesheet,
add a `<link>` in that page's template after the base stylesheet:

```html
{% block extra_css %}
<link rel="stylesheet" href="{{ url_for('static', filename='css/property-custom.css') }}">
{% endblock %}
```

Then add `{% block extra_css %}{% endblock %}` inside `<head>` in `base.html`.

## SEO Features

Every property page includes:
- Unique `<title>` and `<meta name="description">`
- Canonical URL tag
- Open Graph tags (Facebook, LinkedIn previews)
- Twitter Card meta
- JSON-LD structured data (`RealEstateListing`, `LocalBusiness`, `BreadcrumbList`)
- Fast-loading Bootstrap 5 (CDN, no build step)
- Mobile-responsive layout

After deploying, submit `/sitemap.xml` to Google Search Console for faster indexing.

## Production Deployment

```bash
cd website
source .venv/bin/activate

export DATABASE_URL="postgresql://..."
export SITE_DOMAIN="https://www.yourdomain.com"
export FLASK_DEBUG=0

gunicorn -w 4 -b 0.0.0.0:8000 app:app
```

Point nginx or your hosting provider at port 8000. Add an SSL certificate
(Let's Encrypt is free) — Google gives a small ranking boost to HTTPS sites.

## Adding an Extra CSS Block per Page

To support per-page stylesheet swapping, add this to `base.html` inside `<head>`:

```html
{% block extra_css %}{% endblock %}
```

Then in any page template:

```html
{% block extra_css %}
<link rel="stylesheet" href="{{ url_for('static', filename='css/my-page.css') }}">
{% endblock %}
```
