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
    return {"title": title, "text": text[:20000], "image": image}


def fetch_article(url: str, user_agent: str, timeout: int = 25) -> dict | None:
    try:
        r = requests.get(url, headers={"User-Agent": user_agent, "Accept-Language": "en"}, timeout=timeout)
        r.raise_for_status()
        if "html" not in r.headers.get("Content-Type", "html"):
            return None
        data = extract_text(r.text)
    except Exception as exc:
        log.warning("fetch failed %s: %s", url, exc)
        return None
    if len(data["text"]) < 400:  # paywall, consent wall or empty page
        log.info("too little text at %s", url)
        return None
    data["url"] = url
    return data
