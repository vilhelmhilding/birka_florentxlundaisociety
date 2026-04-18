import requests
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
import re
import time
import logging
from collections import deque

log = logging.getLogger("birka")

MAX_PAGES = 500
REQUEST_DELAY = 0.2
REQUEST_TIMEOUT = 12
MAX_HTML_PER_PAGE = 50_000   # raw body HTML per page
MAX_STORED_CHARS = 5_000_000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

_SKIP_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
    ".css", ".js", ".zip", ".json", ".mp4", ".mp3",
    ".woff", ".woff2", ".ttf", ".eot", ".ico",
}

# Stripped at STORAGE time — binary/media tags with zero text value
_STORAGE_STRIP_TAGS = [
    "script", "style", "noscript", "iframe",
    "svg", "img", "video", "audio", "canvas", "picture",
    "link", "meta",
]

# Stripped at ANALYSIS time — structural boilerplate that isn't useful for LLM
_ANALYSIS_STRIP_TAGS = [
    "nav", "aside", "figure", "form", "button",
    "input", "select", "textarea", "label",
]

_STRIP_CLASS_PATTERNS = [
    "cookie", "gdpr", "consent", "popup", "modal",
    "overlay", "newsletter", "subscribe",
    "breadcrumb", "pagination",
]


_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}



# ── URL helpers ───────────────────────────────────────────────────────────────

def _root_domain(netloc: str) -> str:
    return netloc[4:] if netloc.startswith("www.") else netloc


def _same_domain(url: str, base_netloc: str) -> bool:
    return _root_domain(urlparse(url).netloc) == _root_domain(base_netloc)


def _normalize(url: str) -> str:
    p = urlparse(url)
    return p._replace(fragment="").geturl().rstrip("/")


def _title_from_url(url: str) -> str:
    """Derive a human-readable title hint from the URL path."""
    path = urlparse(url).path
    segments = [s for s in path.strip("/").split("/") if s]
    if not segments:
        return ""
    # Use last 2 segments for context
    parts = segments[-2:] if len(segments) >= 2 else segments
    return " / ".join(p.replace("-", " ").replace("_", " ") for p in parts)


# ── link collection ───────────────────────────────────────────────────────────

def _collect_links(soup: BeautifulSoup, page_url: str, base_netloc: str) -> list[str]:
    """Return normalised same-domain URLs found on the page."""
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(page_url, href)
        p = urlparse(full)
        if p.scheme not in ("http", "https"):
            continue
        if not _same_domain(full, base_netloc):
            continue
        if any(p.path.lower().endswith(ext) for ext in _SKIP_EXTENSIONS):
            continue
        links.append(_normalize(full))
    return links


def _collect_links_with_text(soup: BeautifulSoup, page_url: str,
                              base_netloc: str) -> list[dict]:
    """Return {url, title} pairs — anchor text used as a cheap title proxy."""
    seen: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(page_url, href)
        p = urlparse(full)
        if p.scheme not in ("http", "https"):
            continue
        if not _same_domain(full, base_netloc):
            continue
        if any(p.path.lower().endswith(ext) for ext in _SKIP_EXTENSIONS):
            continue
        norm = _normalize(full)
        if norm not in seen:
            seen[norm] = a.get_text(strip=True) or ""
    return [{"url": url, "title": text} for url, text in seen.items()]


# ── storage-time HTML prep ────────────────────────────────────────────────────

def _store_html(soup: BeautifulSoup) -> str:
    """Strip only binary/media tags (no text value) and return raw body HTML."""
    for tag in soup.find_all(_STORAGE_STRIP_TAGS):
        try:
            tag.decompose()
        except Exception:
            pass
    body = soup.find("body")
    root = body if body else soup
    return str(root)[:MAX_HTML_PER_PAGE]


# ── analysis-time cleaning + extraction ──────────────────────────────────────

def _decode_cf_email(encoded: str) -> str:
    """Decode a Cloudflare XOR-obfuscated email."""
    try:
        r = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i+2], 16) ^ r) for i in range(2, len(encoded), 2))
    except Exception:
        return ""


def _decode_cloudflare_emails(soup: BeautifulSoup) -> None:
    """Replace __cf_email__ placeholders with the real decoded email address."""
    for el in soup.find_all(attrs={"data-cfemail": True}):
        encoded = el.get("data-cfemail", "")
        decoded = _decode_cf_email(encoded)
        if decoded:
            el.replace_with(decoded)


def _strip_analysis_boilerplate(soup: BeautifulSoup) -> None:
    """Remove structural boilerplate at analysis time (nav, aside, forms, etc.)."""
    for tag in soup.find_all(_ANALYSIS_STRIP_TAGS):
        try:
            tag.decompose()
        except Exception:
            pass
    candidates = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        try:
            cls = " ".join(tag.get("class") or []).lower()
            tid = (tag.get("id") or "").lower()
        except (AttributeError, TypeError):
            continue
        if any(p in cls + " " + tid for p in _STRIP_CLASS_PATTERNS):
            candidates.append(tag)
    for tag in candidates:
        try:
            tag.decompose()
        except Exception:
            pass



# ── text extraction (called at analysis time, not at scrape time) ─────────────

def _html_to_text(html: str) -> str:
    """Strip boilerplate and extract structured text from stored raw HTML."""
    soup = BeautifulSoup(html, "html.parser")
    _decode_cloudflare_emails(soup)
    _strip_analysis_boilerplate(soup)
    # Use the full body — page-builders (Elementor, Divi, etc.) place content
    # in location-divs outside <main>, so scoping to _find_main misses them.
    # <nav> removal in _strip_analysis_boilerplate already handles menu noise.
    root = soup.find("body") or soup

    parts: list[str] = []
    seen: set[str] = set()

    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6",
                              "p", "li", "dt", "dd", "td", "th"]):
        raw = el.get_text(separator=" ", strip=True)
        t = re.sub(r"\s+", " ", raw).strip()
        is_heading = el.name in ("h1", "h2", "h3", "h4", "h5", "h6")
        min_len = 4 if is_heading else 5
        if not t or len(t) < min_len:
            continue
        if t in seen:
            continue
        seen.add(t)
        name = el.name
        if name == "h1":
            parts.append(f"\n# {t}")
        elif name == "h2":
            parts.append(f"\n## {t}")
        elif name in ("h3", "h4"):
            parts.append(f"\n### {t}")
        elif name in ("h5", "h6"):
            parts.append(f"\n#### {t}")
        elif name == "li":
            parts.append(f"• {t}")
        elif name in ("dt", "dd", "td", "th"):
            parts.append(f"  {t}")
        else:
            parts.append(t)

    # Fallback for page-builder sites where content lives in <div> not p/li/h.
    if not parts:
        for line in root.get_text(separator="\n").splitlines():
            t = re.sub(r"\s+", " ", line).strip()
            if t and len(t) >= 12 and t not in seen:
                seen.add(t)
                parts.append(t)

    text = "\n".join(parts)
    result = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not result:
        log.debug(f"_html_to_text: extracted 0 chars (html={len(html)}ch)")
    return result


def pages_to_text(pages: list[dict]) -> list[dict]:
    """Extract structured text from stored pages."""
    result = []
    for p in pages:
        if "html" in p:
            text = _html_to_text(p["html"])
        else:
            text = p.get("text", "")
        if text:
            result.append({"url": p["url"], "title": p.get("title", ""), "text": text})
    return result


# ── JS-render detection + Playwright fallback ─────────────────────────────────


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _fetch(session: requests.Session, url: str) -> requests.Response | None:
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, verify=True)
        r.raise_for_status()
        return r
    except requests.exceptions.SSLError:
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT, verify=False)
            r.raise_for_status()
            return r
        except Exception as e:
            log.warning(f"scrape SSL-retry failed {url}: {e}")
            return None
    except Exception as e:
        log.warning(f"scrape fetch failed {url}: {e}")
        return None


# ── sitemap discovery ─────────────────────────────────────────────────────────

def _parse_sitemap(session: requests.Session, sitemap_url: str,
                   depth: int = 0,
                   visited: set[str] | None = None) -> set[str]:
    if visited is None:
        visited = set()
    if depth > 4 or sitemap_url in visited:
        return set()
    visited.add(sitemap_url)
    urls: set[str] = set()
    try:
        resp = _fetch(session, sitemap_url)
        if resp is None:
            return urls
        root_el = ET.fromstring(resp.content)
        for loc_el in root_el.findall(".//sm:sitemap/sm:loc", _SITEMAP_NS):
            child = (loc_el.text or "").strip()
            if child:
                urls.update(_parse_sitemap(session, child, depth + 1, visited))
        for loc_el in root_el.findall(".//sm:url/sm:loc", _SITEMAP_NS):
            page_url = (loc_el.text or "").strip()
            if page_url and not any(page_url.lower().endswith(ext) for ext in _SKIP_EXTENSIONS):
                urls.add(page_url)
        log.info(f"scrape: sitemap {sitemap_url} → {len(urls)} URLs (depth={depth})")
    except Exception as e:
        log.warning(f"scrape: sitemap parse failed {sitemap_url}: {e}")
    return urls


def _discover_sitemap_urls(session: requests.Session,
                            base_netloc: str) -> set[str]:
    root = f"https://{base_netloc}"
    candidates: set[str] = set()
    try:
        resp = _fetch(session, f"{root}/robots.txt")
        if resp and resp.ok:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    candidates.add(line.split(":", 1)[1].strip())
    except Exception:
        pass
    for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap/sitemap.xml",
                 "/wp-sitemap.xml", "/page-sitemap.xml", "/post-sitemap.xml"]:
        candidates.add(f"{root}{path}")
    visited: set[str] = set()
    all_urls: set[str] = set()
    for su in candidates:
        all_urls.update(_parse_sitemap(session, su, visited=visited))
    log.info(f"scrape: {len(all_urls)} URLs via sitemaps for {base_netloc}")
    return all_urls


# ── main entry point ──────────────────────────────────────────────────────────

def scrape_website(start_url: str, on_event=None) -> dict:
    """
    Three-phase scrape:
      1. Discover all URLs (sitemap + start-page link collection).
      2. AI filter — one Claude call decides which pages are relevant.
      3. Full scrape of approved pages only (clean HTML stored).

    on_event(dict) is called with typed progress events throughout.
    """
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    if not start_url.startswith(("http://", "https://")):
        start_url = "https://" + start_url

    base_netloc = urlparse(start_url).netloc
    session = requests.Session()
    session.headers.update(_HEADERS)

    # ── Phase 1: discover all URLs ────────────────────────────────────────────
    emit({"type": "discovering"})

    sitemap_urls = _discover_sitemap_urls(session, base_netloc)

    # Fetch start page: get both links-with-text (title proxies) and plain links
    link_titles: dict[str, str] = {}   # url → anchor text
    resp = _fetch(session, _normalize(start_url))
    if resp and "text/html" in resp.headers.get("Content-Type", ""):
        start_soup = BeautifulSoup(resp.text, "html.parser")
        for item in _collect_links_with_text(start_soup, start_url, base_netloc):
            link_titles[item["url"]] = item["title"]

    # Union of all known URLs
    all_urls: set[str] = {_normalize(start_url)}
    for u in sitemap_urls:
        if _same_domain(u, base_netloc):
            all_urls.add(_normalize(u))
    all_urls.update(link_titles.keys())

    log.info(f"scrape: {len(all_urls)} URLs discovered for {base_netloc}")
    emit({"type": "discovered", "count": len(all_urls)})

    # Build (url, title) pairs for filtering
    # Prefer anchor text, fall back to URL path heuristic
    url_title_pairs = []
    for url in sorted(all_urls):
        title = link_titles.get(url) or _title_from_url(url)
        url_title_pairs.append({"url": url, "title": title})

    # ── Phase 2: AI relevance filter ─────────────────────────────────────────
    emit({"type": "filtering", "total": len(url_title_pairs)})
    try:
        from llm import filter_relevant_pages
        relevant = filter_relevant_pages(url_title_pairs)
    except Exception as e:
        log.error(f"scrape: AI filter failed ({e}) — keeping all URLs")
        relevant = {p["url"] for p in url_title_pairs}

    # Always include the start URL regardless of filter decision
    relevant.add(_normalize(start_url))
    log.info(f"scrape: {len(relevant)}/{len(all_urls)} URLs approved by AI filter")
    emit({"type": "filtered", "kept": len(relevant), "total": len(all_urls)})

    # ── Phase 3: full scrape of approved pages ────────────────────────────────
    approved_queue: deque[str] = deque(u for u in sorted(all_urls) if u in relevant)
    visited: set[str] = set()
    queued: set[str] = set(approved_queue)
    pages: list[dict] = []
    total_chars = 0

    def enqueue(u: str) -> None:
        n = _normalize(u)
        if n not in queued and n not in visited and n in relevant:
            approved_queue.append(n)
            queued.add(n)

    # Use Playwright as primary fetcher — it executes JS so email-encoder plugins,
    # lazy-loaded content, and client-side SPAs all work correctly.
    # Fall back to requests if Playwright is not installed.
    _pw = _pw_browser = _pw_page = None
    try:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        _pw_browser = _pw.chromium.launch(headless=True)
        _pw_ctx = _pw_browser.new_context(user_agent=_HEADERS["User-Agent"], locale="sv-SE")
        _pw_page = _pw_ctx.new_page()
        log.info("scrape: playwright ready (JS rendering enabled)")
    except ImportError:
        log.info("scrape: playwright not installed — using requests")
    except Exception as e:
        log.warning(f"scrape: playwright init failed ({e}) — using requests")

    try:
        while approved_queue and len(pages) < MAX_PAGES and total_chars < MAX_STORED_CHARS:
            url = approved_queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            raw_html: str | None = None

            if _pw_page:
                try:
                    _pw_page.goto(url, wait_until="load", timeout=20_000)
                    raw_html = _pw_page.content()
                except Exception as e:
                    log.warning(f"scrape playwright page failed {url}: {e} — falling back to requests")

            if not raw_html:
                resp = _fetch(session, url)
                if resp is None or "text/html" not in resp.headers.get("Content-Type", ""):
                    continue
                raw_html = resp.text

            soup = BeautifulSoup(raw_html, "html.parser")
            title = (soup.title.string or "").strip() if soup.title else ""
            links = _collect_links(soup, url, base_netloc)
            html = _store_html(soup)

            if html:
                pages.append({"url": url, "title": title, "html": html})
                total_chars += len(html)
                log.info(f"scrape [{len(pages)}] {len(html)}ch  '{title[:50]}'")
                emit({"type": "progress", "pages": len(pages), "title": title})

            for link in links:
                enqueue(link)

            if approved_queue and not _pw_page:
                time.sleep(REQUEST_DELAY)

    finally:
        if _pw_browser:
            try:
                _pw_browser.close()
            except Exception:
                pass
        if _pw:
            try:
                _pw.stop()
            except Exception:
                pass

    log.info(f"scrape done: pages={len(pages)}  chars={total_chars}  start={start_url}")
    return {"pages": pages, "total_chars": total_chars}
