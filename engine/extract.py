"""Fetch a source page and pull out the readable article text (facts are taken only from this)."""
import logging
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("engine.extract")

DROP = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg", "figure"]


def extract_text(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    image = ""
    ogi = soup.find("meta", property="og:image")
    if ogi and ogi.get("content"):
        image = ogi["content"].strip()
    images = [image] if image else []
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content") and tw["content"].strip() not in images:
        images.append(tw["content"].strip())
    for img in soup.select("article figure img, main figure img, figure img")[:6]:
        src = img.get("data-src") or img.get("src") or ""
        if src.startswith(("http", "/")) and not src.endswith(".svg") and src not in images:
            images.append(src)
    site = soup.find("meta", property="og:site_name")
    site_name = site["content"].strip() if site and site.get("content") else ""
    for tag in soup(DROP):
        tag.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    # pick the element holding the most paragraph text
    best, best_len = container, 0
    for el in container.find_all(["article", "main", "div", "section"], recursive=True):
        n = sum(len(p.get_text(" ", strip=True)) for p in el.find_all("p", recursive=False))
        if n > best_len:
            best, best_len = el, n
    parts = []
    for el in best.find_all(["h2", "h3", "p", "li", "td", "th"]):
        t = " ".join(el.get_text(" ", strip=True).split())
        if len(t) >= 3:
            parts.append(t)
    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return {"title": title, "text": text[:20000], "image": image, "images": images[:5], "site_name": site_name}


FAILS: dict = {}   # domain -> list of reasons, reported in the run summary


def _fail(url: str, reason: str) -> None:
    from urllib.parse import urlparse
    FAILS.setdefault(urlparse(url).netloc.lower().removeprefix("www."), []).append(reason)


def fetch_article(url: str, user_agent: str, timeout: int = 25) -> dict | None:
    try:
        r = requests.get(url, headers={"User-Agent": user_agent, "Accept-Language": "en"}, timeout=timeout)
        if r.status_code >= 400:
            _fail(url, f"HTTP {r.status_code}")
            return None
        ctype = r.headers.get("Content-Type", "html").lower()
        if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
            text = pdf_text(r.content)
            if len(text) < 400:
                _fail(url, "unreadable PDF")
                return None
            return {"url": url, "title": url.rsplit("/", 1)[-1], "text": text[:20000], "image": "", "images": [], "site_name": ""}
        if "html" not in ctype:
            _fail(url, "not a web page")
            return None
        data = extract_text(r.text)
    except Exception as exc:
        log.warning("fetch failed %s: %s", url, exc)
        _fail(url, exc.__class__.__name__)
        return None
    if len(data["text"]) < 400:  # paywall, consent wall, bot check or empty page
        log.info("too little text at %s", url)
        _fail(url, "too little text (paywall or bot check?)")
        return None
    data["url"] = url
    return data


def pdf_text(data: bytes) -> str:
    """Text of a PDF (official notifications and FAQs are often PDFs)."""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        parts = [(pg.extract_text() or "") for pg in reader.pages[:25]]
        return " ".join(" ".join(parts).split())
    except Exception as exc:  # noqa: BLE001
        log.info("pdf read failed: %s", exc)
        return ""


def wikipedia_text(title: str, user_agent: str, lang: str = "en", timeout: int = 20) -> dict | None:
    """Plain text of a Wikipedia article (useful background; never the only source for rules or prices)."""
    try:
        r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", timeout=timeout, headers={"User-Agent": user_agent},
                         params={"action": "query", "format": "json", "prop": "extracts", "explaintext": 1,
                                 "redirects": 1, "titles": title})
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
        page = next(iter(pages.values()), {})
        text = page.get("extract") or ""
        if len(text) < 400:
            return None
        url = f"https://{lang}.wikipedia.org/wiki/" + page.get("title", title).replace(" ", "_")
        return {"url": url, "title": page.get("title", title) + " (Wikipedia)", "text": text[:20000], "image": "",
                "images": [], "site_name": "Wikipedia"}
    except Exception as exc:  # noqa: BLE001
        log.info("wikipedia failed for %s: %s", title, exc)
        return None
