# Birka

A two-sided marketplace for Swedish local services, where AI removes the friction that makes finding and hiring tradespeople slow, fragmented, and opaque.

---

## Problem Statement

The local services market — plumbers, electricians, painters, carpenters, cleaners — is one of the largest and least digitised sectors of the economy. The problems are structural and affect both sides of the market.

**For buyers:**
Finding a reliable tradesperson today means Googling, calling several companies, waiting for callbacks, getting inconsistent quotes in different formats, and having no reliable way to compare quality or availability. There is no single place to search, compare, request quotes, and pay. For renovation projects involving multiple trades, the coordination overhead is multiplied across every provider.

**For sellers:**
Most tradespeople are small operators — sole traders or micro-companies — with limited time, no marketing budget, and no digital presence beyond a basic website. Getting discovered is hard. Responding to the same questions repeatedly (price? availability? which cities?) is time-consuming. Converting an enquiry into a paid job involves multiple back-and-forth messages before anything is agreed.

**The gap:**
Existing platforms are either too generic (Blocket, Facebook groups), too rigid in their onboarding (requiring forms, structured inputs, photos), or too focused on one trade. None use AI to handle the natural messiness of how real buyers describe what they need and how sellers describe what they offer.

---

## Solution

Birka is a full-cycle marketplace — search, match, quote, negotiate, pay, rate — where Claude handles the translation between natural human communication and structured marketplace data on both sides.

**The core insight** is that both buyers and sellers already know how to communicate naturally. A buyer knows they need their kitchen renovated. A seller knows they serve Lund, charge 500 SEK/h, and are available weekdays. The gap is not information — it is structure. Birka uses AI to create that structure automatically, so neither party has to fill in a form.

**Buyer flow:**
A buyer describes what they need in plain language, uploads a photo, or asks about a project. The system understands the request — whether it is a single service or a multi-trade project — finds matching providers, displays them with prices, ratings, and availability, and lets the buyer request quotes or message sellers directly. The entire quote-to-payment cycle happens within the platform.

**Seller flow:**
A seller registers by writing a short description of their business, or by pasting a website URL. The system reads their text or scrapes their site and automatically builds structured listings — services, cities, pricing, availability. No forms. The seller's dashboard shows incoming quote requests, conversations, and payments. Quote requests from buyers are pre-formatted by AI; the seller replies in plain language and AI formats the response into a professional quote with an extracted price.

**The result** is a marketplace where both sides experience as little friction as possible. A buyer can go from "I need my kitchen renovated" to receiving structured quotes from multiple relevant tradespeople in minutes. A seller can onboard in under two minutes with no structured input.

---

## Technical Approach

### Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11 · Flask · Flask-SQLAlchemy |
| Database | SQLite (`instance/marketplace.db`) |
| AI | Anthropic Claude `claude-sonnet-4-6` via `llm.py` |
| Web scraping | Playwright (headless Chromium) + BeautifulSoup4 |
| Frontend | Vanilla HTML/CSS/JS · Jinja2 templates |
| Auth | Werkzeug password hashing · Flask session |

### Project Structure

```
app.py                    — all routes and business logic (~1 100 lines)
models.py                 — 9 SQLAlchemy models
llm.py                    — all Claude API calls (~380 lines)
scrape.py                 — website scraper (Playwright + BS4)
seed.py                   — database seeding for development
static/style.css          — single stylesheet
static/uploads/           — buyer photo uploads (created at runtime)
templates/
  base.html               — shared layout, nav, unread badge
  dashboard_buyer.html    — search, results, multi-service view, quotes, photo upload
  dashboard_seller.html   — profile, listings, transactions, incoming quotes
  chat.html               — messaging with quote and payment cards
  seller_profile.html     — public profile page
  settings.html           — seller profile editor and website import
  register_loading.html   — live progress during website-based onboarding
  payment.html · rate.html · admin.html
```

### Data Models

```
User                — buyer or seller, email/password, name, city, phone
SellerProfile       — listings (JSON), cities (JSON), avg_rating, contact info, website data
Search              — buyer query log (raw query, mapped service, city)
Conversation        — buyer ↔ seller thread
Message             — type: text | payment | quote_request | quote_response
Transaction         — payment request with amount, status (pending/completed), rating
QuoteRequest        — formatted request text, service, cities; belongs to a bundle or standalone
QuoteResponse       — formatted reply, extracted price; auto-created for fixed-price sellers
MultiServiceBundle  — groups one QuoteRequest per trade for a single project
ScrapeCache         — cached website scrape results keyed by URL
```

---

## How the AI System Works

All AI calls are in `llm.py` and use `claude-sonnet-4-6`. Claude is invoked at eight distinct points in the product flow.

---

### Seller onboarding — website import

#### `filter_relevant_pages(pages)` → `description_from_website(pages)` → `parse_seller(...)`

When a seller registers via website URL, three Claude calls run in sequence as part of a background job:

1. **`filter_relevant_pages`** — given a list of page titles and URLs discovered on the site, Claude returns which pages to actually scrape. It skips cookie policies, GDPR notices, individual review pages, and job listings, and keeps pages likely to contain services, pricing, cities, team info, and certifications.

2. **`description_from_website`** — Claude reads all kept pages and streams a first-person English business description covering every service, city, price, availability, certification, and background detail. This output is optimised for downstream parsing — it is written to be rich with structured facts.

3. The description is then passed to `parse_seller` as above.

The scraper that feeds these calls runs Playwright (headless Chromium) to execute JavaScript before parsing, which captures content rendered by page-builder frameworks (Elementor, Gutenberg) and emails obfuscated by WordPress encoder plugins. Cloudflare-encoded email addresses (`data-cfemail` attributes) are decoded at parse time.

---

### Seller onboarding — understanding free-text descriptions

#### `parse_seller(description, city, existing_categories)`

When a seller saves their profile, their free-text description is sent to Claude along with the current list of service categories already in use across the platform.

Claude returns structured JSON:

```json
{
  "cities": ["Lund", "Malmö"],
  "listings": [
    {
      "service": "painter",
      "availability_days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
      "price_min": 400,
      "price_max": 600,
      "is_quote": false
    }
  ]
}
```

Two rule sets are enforced in the prompt:

**City rules** — Claude normalises Swedish city names: "Sthlm" → "Stockholm", "Gbg" → "Göteborg", "Malmo" → "Malmö". Cities that cannot be matched to a real Swedish city are flagged in `unrecognized_cities` and shown to the seller.

**Category rules** — Claude reuses an existing category if the service is essentially the same ("interior painter" → "painter"). A new category is only created when the distinction is objectively meaningful to a buyer choosing between providers ("yacht painter" is still "painter"; "dog groomer" is not "hairdresser"). Categories are lowercase, 1–3 words, English.

---

### Supporting seller profile calls

#### `summarise_to_profile(description)`
Condenses the full description to 1–3 sentences for the seller's public profile card. Written in third person.

#### `extract_contact_info(description)`
Extracts name, primary email, and primary phone from the full description. If multiple emails exist, Claude prefers the general contact address (info@, hej@). Used to pre-fill user fields after website onboarding.

---

### Buyer search — understanding natural language queries

#### `parse_buyer_multi(query, default_city, existing_categories)`

Every buyer search goes through this function. It handles both explicit service queries and project descriptions:

- *"painter in Lund, budget 5 000 SEK, available weekends"* → one service with filters
- *"painter and electrician in Lund"* → two services extracted
- *"kitchen renovation in Stockholm"* → multiple services **inferred** (plumber, electrician, carpenter, painter, tiler — Claude uses judgement on which trades are most likely needed)

Returns:

```json
{
  "services": [
    {"service": "carpenter", "price_max": null, "requested_day": null},
    {"service": "electrician", "price_max": null, "requested_day": null}
  ],
  "cities": ["Stockholm"],
  "unrecognized_cities": []
}
```

Single-service results are shown as a ranked list. Multi-service results are shown as a responsive grid — one card per trade — so the buyer can evaluate each market in parallel.

---

### Buyer search — matching

#### `match_sellers(parsed, sellers)` — no LLM call

Pure Python. For each service in the parsed query, iterates every seller's every listing:

- Filters by exact service category match
- Groups results by city (substring match to handle "Lund" matching "Lund" in seller's city list)
- Flags `over_budget` if `listing.price_min > buyer.price_max`
- Flags `unavailable` if `buyer.requested_day` is not in `listing.availability_days`
- Sorts by number of flags ascending (best matches first)

Returns `{by_city: {city: [(seller, listing, flags)]}, other: [...]}`.

---

### Quote workflow

#### `format_quote_request(raw_text, service, cities, price_max, requested_day)`

The buyer writes a casual message ("I need someone to repaint my living room, around 3 000 SEK, preferably on a Saturday"). Claude reformats this into a short professional quote request (2–4 sentences) keeping all specific details, and sends it as a `quote_request` message to every matched seller.

For multi-service bundles, this is called once per trade with trade-specific context.

**Fixed-price sellers are auto-answered.** When a multi-service bundle is sent, sellers whose listing has a fixed price (not `is_quote`) receive an automatic `QuoteResponse` immediately with their listed price — no action required from them. Sellers with quote-on-request pricing receive the message and respond manually.

#### `format_quote_response(raw_text, quote_body)`

The seller reads the quote request and types a casual reply. Claude reformats it into a professional quote response and extracts the offered price as an integer SEK value. The formatted text is sent as a `quote_response` message; the price is stored on the `QuoteResponse` record and shown in the buyer's quotes tab sorted lowest-first.

---

### Photo search — computer vision

#### `analyze_photo_for_service(image_b64, media_type, default_city, existing_categories)`

The buyer uploads a photo from the search form. Claude Vision identifies the home or property problem in the image and returns:

- `service` — best matching category (e.g. `"plumber"` for a burst pipe, `"painter"` for peeling walls)
- `description` — 1–2 sentence explanation of what Claude sees, shown to the buyer in a green banner above results
- `cities` — any Swedish cities visible in the image (falls back to buyer's registered city)

The result is used to run a full search immediately. The photo thumbnail and Claude's reasoning are shown above the results so the buyer can verify the interpretation before acting on it.

---

## How to Run

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)
- Playwright (only needed for website-based seller onboarding)

### Setup

```bash
git clone <repo-url> && cd birka

pip install -r requirements.txt
playwright install chromium   # optional, for website import

cp .env.example .env          # or create manually:
# ANTHROPIC_API_KEY=sk-ant-...
# SECRET_KEY=any-random-string
# ADMIN_PASSWORD=yourpassword
```

### Seed the database

```bash
python seed.py
```

Creates 25 sellers across 5 categories (plumber, electrician, painter, carpenter, appliance repair) in Lund and Malmö, with varied ratings (1.9–4.8 stars, 10–40 reviews), pricing (fixed 400/500/600 SEK/h or quote on request), and availability patterns.

| Role | Email | Password |
|---|---|---|
| Buyer | buyer@test.com | a |
| Seller | erik.rornas@example.com | a |
| Any seller | see `seed.py` for full list | a |

### Start

```bash
python app.py
# → http://localhost:5002
```

Admin panel: `/admin` — enter `ADMIN_PASSWORD` from `.env`.
