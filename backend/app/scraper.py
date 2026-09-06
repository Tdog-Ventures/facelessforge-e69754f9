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

# ── FIX 3: semantic LLM-generated Pexels queries ──────────────────────
# Raw script keywords like "judgment"→gavels, "gap"→boxes. Use LLM to
# return 3 visual nouns in software-engineering / business productivity context.
_PEXELS_ABSTRACT_BLOCKLIST = {
    "judgment","judgement","gap","abstract","legal","literal","gavel","boxes",
    "moving","boxes","concept","idea","notion","theory","mindset","paradigm",
    "judgement","verdict","court","lawyer","lawsuit","contract","agreement",
    "shall","should","could","would","might","may","abstract","generic",
}

def _strip_abstract_words(query: str) -> str:
    """Remove abstract/legal/literal tokens before sending to Pexels."""
    words = [w for w in re.split(r"\W+", query.lower()) if w]
    kept = [w for w in words if w not in _PEXELS_ABSTRACT_BLOCKLIST and len(w) >= 3]
    return " ".join(kept[:4]).strip() or "business team office"

async def _llm_visual_query(sentence: str) -> str:
    """Call LLM: Generate a 3-word Pexels search query for B-roll.
    Returns visual nouns only, fallback to stripped keywords."""
    import os, httpx
    prompt = (
        'Generate a 3-word Pexels search query for B-roll that visually represents: '
        f'"{sentence[:400]}" in the context of software engineering / business productivity. '
        'Return only visual nouns, no abstract words.'
    )
    # Try DeepSeek → Kimi → Claude (same keys as generation.py), else fallback
    for provider in ("DEEPSEEK_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY", "ANTHROPIC_API_KEY"):
        key = os.environ.get(provider, "").strip()
        if not key:
            continue
        try:
            if provider == "DEEPSEEK_API_KEY":
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.post("https://api.deepseek.com/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model":"deepseek-chat","messages":[{"role":"user","content":prompt}],"temperature":0.3,"max_tokens":30})
                    if r.status_code==200:
                        txt=r.json()["choices"][0]["message"]["content"].strip().lower()
                        txt=re.sub(r"[^a-z0-9 ]"," ",txt)
                        return _strip_abstract_words(txt) or _strip_abstract_words(sentence)
            elif provider in ("KIMI_API_KEY","MOONSHOT_API_KEY"):
                url="https://api.moonshot.cn/v1/chat/completions"
                model=os.environ.get("KIMI_MODEL","kimi-k2-0711-preview")
                async with httpx.AsyncClient(timeout=20) as c:
                    r=await c.post(url, headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
                        json={"model":model,"messages":[{"role":"user","content":prompt}],"temperature":0.3,"max_tokens":30})
                    if r.status_code==200:
                        txt=r.json()["choices"][0]["message"]["content"].strip().lower()
                        return _strip_abstract_words(re.sub(r"[^a-z0-9 ]"," ",txt))
            else:
                async with httpx.AsyncClient(timeout=20) as c:
                    r=await c.post("https://api.anthropic.com/v1/messages",
                        headers={"x-api-key":key,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                        json={"model":os.environ.get("LLM_MODEL","claude-3-5-sonnet-latest"),"max_tokens":30,"messages":[{"role":"user","content":prompt}]})
                    if r.status_code==200:
                        txt="".join(b.get("text","") for b in r.json().get("content",[])).strip().lower()
                        return _strip_abstract_words(re.sub(r"[^a-z0-9 ]"," ",txt))
        except Exception:
            continue
    # Fallback deterministic: strip abstracts from raw sentence keywords
    return _strip_abstract_words(sentence)

async def semantic_pexels_query(sentence: str) -> str:
    """Public: LLM visual query with abstract filtering. Use instead of raw keywords."""
    q = await _llm_visual_query(sentence)
    return q or _strip_abstract_words(sentence)

# Also create app/scraper.py alias for import compatibility
