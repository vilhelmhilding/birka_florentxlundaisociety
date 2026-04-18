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


def _call(prompt: str) -> str:
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()


def parse_seller(description: str, city: str, existing: list[str]) -> dict:
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
        raw = _call(prompt)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)
        log.debug(f"parse_seller LLM response: {raw}")
        if "city" in result and "cities" not in result:
            result["cities"] = [result["city"]]
        return result
    except Exception as e:
        log.error(f"parse_seller FAILED: {e}")
        return {"cities": [city] if city else [], "listings": []}


def parse_buyer(query: str, default_city: str, existing: list[str]) -> dict:
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
        if "city" in result and "cities" not in result:
            result["cities"] = [result["city"]] if result["city"] else []
        return result
    except Exception as e:
        log.error(f"parse_buyer FAILED: {e}")
        return {"service": None, "cities": [default_city] if default_city else [], "price_max": None, "requested_day": None}


def match_sellers(parsed: dict, sellers) -> dict:
    service = (parsed.get("service") or "").lower()
    buyer_cities = [c.strip().lower() for c in (parsed.get("cities") or []) if c.strip()]
    price_max = parsed.get("price_max")
    requested_day = (parsed.get("requested_day") or "").lower()

    city_display = {c.lower(): c for c in (parsed.get("cities") or [])}

    by_city = {c: [] for c in buyer_cities}
    other = []

    for seller in sellers:
        profile = seller.seller_profile
        seller_cities = profile.get_cities()

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


def format_quote_response(raw_text: str, quote_body: str) -> tuple[str, int | None]:
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
