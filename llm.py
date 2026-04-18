import anthropic, json, os

SERVICES = [
    "painter", "plumber", "carpenter", "electrician", "cleaner",
    "roofer", "landscaper", "mason", "welder", "locksmith",
    "tiler", "plasterer", "flooring_installer", "hvac_technician",
    "handyman", "mover", "gardener", "pest_controller", "window_cleaner",
    "pool_service", "security_installer", "appliance_repair", "auto_mechanic",
    "upholstery", "interior_designer",
]

_client = None
def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    return _client

_SERVICE_LIST = ', '.join(SERVICES)

def _call(system: str, user: str) -> str:
    r = _get_client().messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=512,
        system=system,
        messages=[{'role': 'user', 'content': user}],
    )
    return r.content[0].text.strip()

def parse_seller_profile(business_name: str, description: str) -> dict:
    system = (
        "You are a marketplace data processor. "
        "Return ONLY valid JSON — no markdown, no explanation. "
        f"Allowed service keys: {_SERVICE_LIST}. "
        "Use only these keys. If price not mentioned use 0."
    )
    prompt = f"""Business: {business_name}
Description: {description}

Return JSON:
{{
  "services": ["<key>"],
  "cities": ["<city>"],
  "price_min": 0,
  "price_max": 0,
  "summary": "<one sentence professional summary>"
}}"""
    try:
        return json.loads(_call(system, prompt))
    except Exception:
        return {"services": [], "cities": [], "price_min": 0, "price_max": 0, "summary": description[:200]}


def parse_buyer_search(query: str) -> dict:
    system = (
        "You are a marketplace search engine. "
        "Map natural language queries to service categories strictly. "
        "Return ONLY valid JSON — no markdown, no explanation. "
        f"Allowed service keys: {_SERVICE_LIST}."
    )
    prompt = f"""Query: {query}

Return JSON:
{{
  "services": ["<key>"],
  "city": "<city or empty string>",
  "max_price": 0
}}"""
    try:
        return json.loads(_call(system, prompt))
    except Exception:
        return {"services": [], "city": "", "max_price": 0}
