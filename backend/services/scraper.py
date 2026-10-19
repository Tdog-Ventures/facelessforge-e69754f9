"""Website scraper for FacelessForge/VideoForge — text extraction, products, brand tone, auto keyword mapping.

Provides:
  scrape_website(url) -> {url, title, text, products, brand_tone, keywords, visual_direction, search_terms}
Uses httpx + BeautifulSoup (already in requirements) + app.visual_query / app.scene_terms for keyword mapping.
"""
from __future__ import annotations
import re
import logging
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("facelessforge.scraper")

# Brand tone heuristics — matches tone values used in ProjectCreate
_TONE_KEYWORDS = {
    "calm luxury": ["luxury", "premium", "elegant", "minimalist", "serene", "calm", "soft", "organic", "botanical"],
    "energetic": ["bold", "hype", "viral", "fast", "dynamic", "power", "energy"],
    "cinematic": ["cinematic", "film", "story", "narrative", "visual"],
    "mysterious": ["mystery", "secret", "hidden", "unknown", "dark"],
    "curious-expert": ["expert", "science", "research", "guide", "how to"],
}

def _detect_brand_tone(text: str) -> str:
    lower = text.lower()
    scores = {k: sum(1 for w in v if w in lower) for k, v in _TONE_KEYWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "calm-authoritative"
    return best

def _extract_products(soup: BeautifulSoup) -> list[str]:
    products = []
    # Look for product-like elements
    for sel in soup.select("h1,h2,h3,[class*=product],[class*=item],[class*=card]"):
        t = " ".join(sel.get_text(separator=" ").split())
        if 3 < len(t) < 100 and len(t.split()) >= 2 and len(t.split()) <= 8:
            # Skip navigation/generic
            if t.lower() in ("products", "services", "about us", "contact"):
                continue
            if t not in products:
                products.append(t)
        if len(products) >= 6:
            break
    return products[:6]

def _extract_text(soup: BeautifulSoup, max_chars: int = 4000) -> str:
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text[:max_chars].strip()

async def scrape_website(url: str, timeout: float = 12.0) -> dict:
    """Fetch URL and extract website text, products, brand tone, auto-mapped keywords."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (FacelessForge Scraper)"}) as client:
        r = await client.get(url)
        r.raise_for_status()
        html = r.text
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    meta_desc = ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        meta_desc = meta["content"].strip()
    text = _extract_text(soup)
    products = _extract_products(soup)
    brand_tone = _detect_brand_tone(text + " " + title + " " + meta_desc)

    # Auto keyword mapping using existing app logic
    try:
        from app.visual_query import extract_visual_keywords
        keywords = extract_visual_keywords(text, top_n=8)
    except Exception:
        # Fallback simple
        words = re.findall(r"[a-z]{4,}", text.lower())
        seen=set()
        keywords=[]
        for w in words:
            if w not in seen:
                seen.add(w)
                keywords.append(w)
            if len(keywords)>=8:
                break

    # Build visual_direction and search_terms for scene mapping
    visual_direction = ", ".join(products[:3]) if products else (title or text[:120])
    search_terms = products[:3] + keywords[:3]
    # Deduplicate
    seen=set()
    uniq=[]
    for t in search_terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    search_terms = uniq[:6]

    return {
        "url": url,
        "title": title,
        "meta_description": meta_desc,
        "text": text,
        "text_length": len(text),
        "products": products,
        "brand_tone": brand_tone,
        "keywords": keywords,
        "visual_direction": visual_direction[:200],
        "search_terms": search_terms,
    }

# Also create app/scraper.py alias for import compatibility
