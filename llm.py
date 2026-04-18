import anthropic
import json
import os
import logging

log = logging.getLogger("birka")
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

CITY_RULES = """
Rules for city names:
- Always return the correct, official Swedish city name.
- Fix typos and abbreviations: "Lud" → "Lund", "Gbg" → "Göteborg", "Sthlm" → "Stockholm".
- Translate English names: "Gothenburg" → "Göteborg", "Malmo" → "Malmö".
- If a place name cannot be matched to a real Swedish city, omit it.
"""

CATEGORY_RULES = """
Rules for service categories:
- Reuse an existing category if the service is essentially the same thing (e.g. "house painter" → "painter").
- Create a NEW category only when context makes it objectively distinct and important to a customer
  choosing between providers. Good examples: "dog groomer" ≠ "hairdresser", "yacht painter" ≠ "painter".
  Bad examples: "interior painter" is still just "painter", "deep cleaner" is still "cleaner".
- Categories must be lowercase, 1–3 words, in English, no punctuation.
- Do not create overly niche categories. When in doubt, reuse the closest existing one.
"""


def _call(prompt: str, max_tokens: int = 1024) -> str:
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()


def parse_seller(description: str, city: str, existing: list[str]) -> dict:
    """Parse seller description into cities list + array of individual listings."""
    existing_str = ", ".join(existing) if existing else "(none yet)"
    prompt = f"""You are a strict data extractor for a home and local services marketplace.

Existing service categories: {existing_str}

{CITY_RULES}
{CATEGORY_RULES}

From the seller description, extract:
1. cities: JSON array of ALL cities/areas they operate in that you can confidently match to a real Swedish city. If only one city, return a single-element array. Use the provided registration city if no city is mentioned in the description.
2. unrecognized_cities: JSON array of any location strings from the description that you could NOT match to a real Swedish city (e.g. typos, unknown places). Empty array if all cities were recognized.
3. listings: JSON array where EACH DISTINCT SERVICE gets its own object with:
   - service: category string (reuse or create per rules)
   - availability_days: array of lowercase weekday names ([] if not mentioned; "weekdays" → mon–fri; "weekends" → sat–sun)
   - price_min: integer in SEK (null if not mentioned or if is_quote)
   - price_max: integer in SEK (null if not mentioned or if is_quote)
   - is_quote: true if the seller explicitly offers quote-based / on-request pricing; false or omit otherwise

CRITICAL: Only list services the seller directly performs themselves. Do NOT create listings for trades they merely subcontract, coordinate, or manage on behalf of clients. If a seller says "we coordinate electricians and plumbers" or "we handle all trades", that means they project-manage those trades — do not list electrician or plumber as their own services.

If a seller offers multiple services with different conditions, produce multiple listing objects.
If all services share the same conditions, one object per service is still required.

Return ONLY valid JSON, no explanation.
Example (fixed price):
{{"cities": ["Stockholm"], "unrecognized_cities": [], "listings": [
  {{"service": "painter", "availability_days": ["monday","tuesday","wednesday","thursday","friday"], "price_min": 500, "price_max": 1000, "is_quote": false}}
]}}
Example (quote-based):
{{"cities": ["Lund"], "unrecognized_cities": [], "listings": [
  {{"service": "electrician", "availability_days": [], "price_min": null, "price_max": null, "is_quote": true}}
]}}

Important: unrecognized_cities must only contain location strings explicitly written in the description text above. Do NOT include the registration city in unrecognized_cities — it is only a fallback and should never be flagged.

Seller description: {description}
City (from registration, use only if no city found in description): {city}"""
    try:
        raw = _call(prompt, max_tokens=2048)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)
        log.debug(f"parse_seller LLM response: {raw}")
        # Normalise: support legacy single-city response
        if "city" in result and "cities" not in result:
            result["cities"] = [result["city"]]
        return result
    except Exception as e:
        log.error(f"parse_seller FAILED: {e}")
        return {"cities": [city] if city else [], "listings": []}


def parse_buyer(query: str, default_city: str, existing: list[str]) -> dict:
    """Map buyer query to structured search parameters."""
    existing_str = ", ".join(existing) if existing else "(none yet)"
    prompt = f"""You are a strict query mapper for a home and local services marketplace.

Existing service categories: {existing_str}

{CITY_RULES}
{CATEGORY_RULES}

From the buyer query below, extract:
1. service: single best matching service category — ONLY if the query clearly and unambiguously describes a real service need. If the query is too short, gibberish, or does not clearly indicate a service, return null.
2. cities: JSON array of ALL cities/areas they want to search in that you can confidently match to a real Swedish city. Use [default_city] if no city is mentioned.
3. unrecognized_cities: JSON array of any location strings you could NOT match to a real Swedish city. Empty array if all were recognized.
4. price_max: maximum budget in SEK as integer (null if not mentioned)
5. requested_day: a single lowercase weekday name if they mention a specific day (null if not mentioned)

Return ONLY valid JSON, no explanation.
Example (one city): {{"service": "painter", "cities": ["Göteborg"], "unrecognized_cities": [], "price_max": 5000, "requested_day": "friday"}}
Example (one bad city): {{"service": "cleaner", "cities": ["Lund"], "unrecognized_cities": ["Upps"], "price_max": null, "requested_day": null}}
Unclassifiable example: {{"service": null, "cities": ["Göteborg"], "unrecognized_cities": [], "price_max": null, "requested_day": null}}

Important: unrecognized_cities must only contain location strings explicitly written in the query above. Do NOT include the default city in unrecognized_cities.

Buyer query: {query}
Default city (use only if no city found in query): {default_city}"""
    try:
        raw = _call(prompt)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)
        log.debug(f"parse_buyer LLM response: {raw}")
        # Normalise: support legacy single-city response
        if "city" in result and "cities" not in result:
            result["cities"] = [result["city"]] if result["city"] else []
        return result
    except Exception as e:
        log.error(f"parse_buyer FAILED: {e}")
        return {"service": None, "cities": [default_city] if default_city else [], "price_max": None, "requested_day": None}


def parse_buyer_multi(query: str, default_city: str, existing: list[str]) -> dict:
    """Like parse_buyer but extracts ALL services mentioned in the query.
    Returns {services: [{service, price_max, requested_day}, ...], cities: [...], unrecognized_cities: [...]}
    """
    existing_str = ", ".join(existing) if existing else "(none yet)"
    prompt = f"""You are a strict query mapper for a home and local services marketplace.

Existing service categories: {existing_str}

{CITY_RULES}
{CATEGORY_RULES}

From the buyer query below, extract ALL distinct services mentioned (e.g. "painter and electrician" → two services).
1. services: JSON array — one object per distinct service:
   - service: category string (reuse/create per rules), null if unclear
   - price_max: integer SEK budget for this service (use shared budget if mentioned; null otherwise)
   - requested_day: lowercase weekday if mentioned, null otherwise
   If only one service is mentioned, return a single-element array.
2. cities: JSON array of Swedish cities shared across all services. Use [default_city] if none mentioned.
3. unrecognized_cities: cities that could not be matched. Empty array if all matched.

Return ONLY valid JSON.
Example: {{"services": [{{"service": "painter", "price_max": 5000, "requested_day": "sunday"}}, {{"service": "plumber", "price_max": null, "requested_day": "sunday"}}], "cities": ["Lund"], "unrecognized_cities": []}}

Buyer query: {query}
Default city (use only if no city found in query): {default_city}"""
    try:
        raw = _call(prompt)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)
        log.debug(f"parse_buyer_multi LLM response: {raw}")
        return result
    except Exception as e:
        log.error(f"parse_buyer_multi FAILED: {e}")
        return {"services": [{"service": None, "price_max": None, "requested_day": None}],
                "cities": [default_city] if default_city else [], "unrecognized_cities": []}


def match_sellers(parsed: dict, sellers) -> dict:
    """
    Match on individual listings, not whole profiles.
    Returns {
      "by_city": {"Stockholm": [(seller, listing, flags), ...], "Lund": [...]},
      "other":   [(seller, listing, flags), ...]
    }
    flags includes "matched_city" for local results.
    """
    service = (parsed.get("service") or "").lower()
    buyer_cities = [c.strip().lower() for c in (parsed.get("cities") or []) if c.strip()]
    price_max = parsed.get("price_max")
    requested_day = (parsed.get("requested_day") or "").lower()

    # Preserve original casing for display
    city_display = {c.lower(): c for c in (parsed.get("cities") or [])}

    by_city = {c: [] for c in buyer_cities}
    other = []

    for seller in sellers:
        profile = seller.seller_profile
        seller_cities = profile.get_cities()  # list of lowercase strings

        for listing in profile.get_listings():
            if service != listing.get("service", "").lower():
                continue
            over_budget = bool(price_max and listing.get("price_min")
                               and listing["price_min"] > price_max)
            avail = listing.get("availability_days") or []
            unavailable = bool(requested_day and avail and requested_day not in avail)
            flags = {"over_budget": over_budget, "unavailable": unavailable}

            matched = next(
                (bc for bc in buyer_cities
                 if any(bc in sc or sc in bc for sc in seller_cities)),
                None
            )
            if matched:
                flags["matched_city"] = city_display.get(matched, matched.capitalize())
                by_city[matched].append((seller, listing, flags))
            else:
                other.append((seller, listing, flags))

    key = lambda x: sum(v for k, v in x[2].items() if isinstance(v, bool))
    for lst in by_city.values():
        lst.sort(key=key)
    other.sort(key=key)
    return {"by_city": by_city, "other": other}


def format_quote_request(raw_text: str, service: str, cities: list, price_max, requested_day) -> str:
    """Format buyer's casual message into a short professional quote request."""
    ctx = [f"Service: {service}"]
    if cities:
        ctx.append(f"Location: {', '.join(cities)}")
    if price_max:
        ctx.append(f"Max budget: {price_max} SEK")
    if requested_day:
        ctx.append(f"Preferred day: {requested_day}")
    prompt = f"""Format the following buyer message into a short professional quote request (2–4 sentences).
Keep all specific details. Write in English. Start directly — no greeting.

Context: {' · '.join(ctx)}
Message: {raw_text}

Return only the formatted text."""
    try:
        return _call(prompt)
    except Exception as e:
        log.error(f"format_quote_request FAILED: {e}")
        return raw_text


def summarise_to_profile(raw_description: str) -> str:
    """Condense a full description into 1–3 sentences for the profile card."""
    prompt = f"""Condense the following business description into 1–3 short sentences for a profile card.
Keep the most important facts: main services, key cities/regions, and pricing if mentioned.
Write in third person in English (e.g. "SERA BYGG offers…", "A Malmö-based painter…"). No headings, no bullet points.

Description:
{raw_description[:5000]}

Return only the short profile text."""
    try:
        return _call(prompt)
    except Exception as e:
        log.error(f"summarise_to_profile FAILED: {e}")
        return ""


def description_from_website(pages: list[dict]):
    """Stream a seller description from scraped website pages. Yields text chunks."""
    if not pages:
        return
    combined = ""
    for p in pages:
        title = p.get("title") or p.get("url", "")
        text = p.get("text", "")
        if text:
            combined += f"\n\n--- {title} ---\n{text}"
    prompt = f"""Extract business info from these {len(pages)} scraped website pages and write a first-person description for a Swedish local services marketplace.

Include everything relevant: every service and specialisation, every city/area served, all pricing (SEK amounts, ranges, hourly rates), availability (days/hours), company background (age, certifications, team size), and any detail useful to a buyer choosing between providers. Be complete — this text is parsed by AI to build structured listings.

Write in English using "we" (not "I"). Return only the description.

--- PAGES ---
{combined}"""
    log.info(f"description_from_website: {len(pages)} pages, {len(combined)} combined chars → Claude")
    try:
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for chunk in stream.text_stream:
                yield chunk
    except Exception as e:
        log.error(f"description_from_website stream failed: {e}")
        raise


def filter_relevant_pages(pages: list[dict]) -> set[str]:
    """Given [{url, title}] pairs, return URLs to keep. Errs on side of keeping."""
    if not pages:
        return set()
    lines = "\n".join(
        f"{i+1}. {p.get('title') or '(no title)'}  [{p['url']}]"
        for i, p in enumerate(pages)
    )
    prompt = f"""You are filtering pages from a company website before scraping.

KEEP pages that are likely to contain business-relevant content:
services offered, service areas/cities, pricing, about/team/company info,
certifications, contact details, references/portfolio, FAQ, news about the company.

SKIP pages that are clearly irrelevant:
individual customer reviews or testimonials (e.g. "Review from John"),
individual job application pages, privacy policy, cookie policy, terms & conditions,
GDPR notices, error pages (404 etc.).

When in doubt → KEEP.

Pages to review ({len(pages)} total):
{lines}

Return ONLY a JSON array of the page numbers (1-indexed) to KEEP. Example: [1, 3, 5, 7]
No explanation."""
    try:
        raw = _call(prompt, max_tokens=2048)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        indices = json.loads(raw)
        return {pages[i - 1]["url"] for i in indices if isinstance(i, int) and 1 <= i <= len(pages)}
    except Exception as e:
        log.error(f"filter_relevant_pages FAILED: {e} — keeping all")
        return {p["url"] for p in pages}


def extract_contact_info(description: str) -> dict:
    """Extract best name, email, phone from description. Returns partial dict with non-null values only."""
    prompt = f"""From this business description, extract the primary contact details.
- name: company name or primary contact person name (string or null)
- email: best business email address (string or null). If multiple exist, prefer the general contact (info@, hej@) over department-specific ones.
- phone: best business phone number in original format (string or null). If multiple exist, prefer the main switchboard or owner's number.

Return ONLY valid JSON: {{"name": "...", "email": "...", "phone": "..."}}
Use null for any field not found.

Description:
{description}"""
    try:
        raw = _call(prompt)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        data = json.loads(raw)
        return {k: v for k, v in data.items() if v}
    except Exception as e:
        log.error(f"extract_contact_info FAILED: {e}")
        return {}


def analyze_photo_for_service(image_b64: str, media_type: str, default_city: str, existing: list[str]) -> dict:
    """Use Claude Vision to identify what home/local service is needed from a photo."""
    existing_str = ", ".join(existing) if existing else "(none yet)"
    prompt = f"""You are a service identifier for a Swedish home and local services marketplace.

Existing service categories: {existing_str}

{CITY_RULES}
{CATEGORY_RULES}

Look at this image and determine what home or local service the buyer likely needs.

Extract:
1. service: the best matching service category (e.g. "painter", "plumber", "electrician", "cleaner"). Must be lowercase, 1-3 words, in English. Return null if the image clearly has nothing to do with a home or local service need.
2. description: a 1-2 sentence plain-English description of what you see and why this service is needed.
3. cities: JSON array of any Swedish cities visible or mentioned in the image. Use ["{default_city}"] if none found.

Return ONLY valid JSON.
Example: {{"service": "painter", "description": "The photo shows peeling paint on an interior wall that needs repainting.", "cities": ["{default_city}"]}}
Unrecognizable example: {{"service": null, "description": "The image does not appear to show a service need.", "cities": ["{default_city}"]}}"""
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": prompt}
            ]}]
        )
        raw = msg.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)
        log.debug(f"analyze_photo_for_service LLM response: {raw}")
        return result
    except Exception as e:
        log.error(f"analyze_photo_for_service FAILED: {e}")
        return {"service": None, "description": "Could not analyse the image.", "cities": [default_city] if default_city else []}


def format_quote_response(raw_text: str, quote_body: str) -> tuple[str, int | None]:
    """Format seller's casual reply into a clean quote response. Returns (formatted_text, price_sek_or_None)."""
    prompt = f"""Format the following seller reply into a short professional quote response (2–4 sentences).
Keep all details and pricing. Write in English.
Extract any offered price as an integer SEK value; null if none mentioned.

Quote request: {quote_body}
Seller reply: {raw_text}

Return ONLY valid JSON: {{"response": "...", "price": 2500}} or {{"response": "...", "price": null}}"""
    try:
        raw = _call(prompt)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        data = json.loads(raw)
        return data.get("response", raw_text), data.get("price")
    except Exception as e:
        log.error(f"format_quote_response FAILED: {e}")
        return raw_text, None
