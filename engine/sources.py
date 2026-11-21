"""Discovery: RSS/Atom feeds and the GDELT news search API."""
import logging
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from lxml import etree

log = logging.getLogger("engine.sources")

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        d = parsedate_to_datetime(text)  # RSS (RFC 822)
    except (TypeError, ValueError):
        d = None
    if d is None:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                d = datetime.strptime(text.replace("Z", "+0000") if fmt.endswith("%z") else text, fmt)
                break
            except ValueError:
                continue
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _text(el, *paths) -> str:
    for p in paths:
        found = el.find(p)
        if found is not None:
            if found.text and found.text.strip():
                return found.text.strip()
            href = found.get("href")
            if href:
                return href.strip()
    return ""


def parse_feed(xml: bytes, source_name: str) -> list[dict]:
    """Parse RSS 2.0 or Atom into items {url,title,published,summary,source,domain}."""
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(xml, parser=parser)
    if root is None:
        return []
    ns_atom = "{http://www.w3.org/2005/Atom}"
    items = []
    entries = root.findall(".//item") or root.findall(f".//{ns_atom}entry")
    for e in entries:
        if e.tag == "item":
            url = _text(e, "link", "guid")
            title = _text(e, "title")
            published = _text(e, "pubDate", "{http://purl.org/dc/elements/1.1/}date")
            summary = _text(e, "description")
        else:
            link = None
            for l in e.findall(f"{ns_atom}link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href")
                    break
            url = link or _text(e, f"{ns_atom}id")
            title = _text(e, f"{ns_atom}title")
            published = _text(e, f"{ns_atom}published", f"{ns_atom}updated")
            summary = _text(e, f"{ns_atom}summary", f"{ns_atom}content")
        if not url or not title or not url.startswith("http"):
            continue
        d = _parse_date(published)
        items.append({
            "url": url,
            "title": " ".join(title.split()),
            "published": d.isoformat() if d else "",
            "summary": summary[:500],
            "source": source_name,
            "domain": domain_of(url),
        })
    return items


def fetch_feeds(feeds: list[dict], user_agent: str, timeout: int = 20, health: dict | None = None) -> list[dict]:
    """health (optional) is filled with {feed name: item count, or an error string}."""
    out = []
    for f in feeds:
        try:
            r = requests.get(f["url"], headers={"User-Agent": user_agent,
                                                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"},
                             timeout=timeout)
            r.raise_for_status()
            got = parse_feed(r.content, f["name"])
            for it in got:
                if f.get("country"):
                    it["country"] = f["country"]
                if f.get("edition"):
                    it["edition"] = f["edition"]
            log.info("feed ok: %s (%d items)", f["name"], len(got))
            out.extend(got)
            if health is not None:
                health[f["name"]] = len(got)
        except Exception as exc:  # a broken feed must never stop the run
            log.warning("feed failed: %s: %s", f["name"], exc)
            if health is not None:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                health[f["name"]] = f"failed ({code})" if code else "failed"
    return out


def parse_gdelt(data: dict, query: str) -> list[dict]:
    out = []
    for a in data.get("articles", []) or []:
        url = a.get("url") or ""
        title = a.get("title") or ""
        if not url.startswith("http") or not title:
            continue
        d = _parse_date(a.get("seendate"))
        out.append({
            "url": url,
            "title": " ".join(title.split()),
            "published": d.isoformat() if d else "",
            "summary": "",
            "source": "GDELT",
            "domain": a.get("domain") or domain_of(url),
            "language": a.get("language", ""),
            "country": a.get("sourcecountry", ""),
        })
    return out


def fetch_gdelt(cfg: dict, user_agent: str, timeout: int = 30, health: dict | None = None,
                edition_queries: dict | None = None) -> list[dict]:
    edition_queries = edition_queries or {}
    if not cfg.get("enabled"):
        return []
    out, ok, failed = [], 0, 0
    for i, q in enumerate(cfg.get("queries", [])):
        if i:
            time.sleep(8)  # GDELT allows one request every 5 seconds; leave a margin
        params = {"query": q, "mode": "artlist", "format": "json",
                  "maxrecords": cfg.get("max_records", 75), "timespan": cfg.get("timespan", "12h"), "sort": "datedesc"}
        try:
            r = None
            for attempt in range(2):  # GDELT often refuses a request when busy: wait and try once more
                r = requests.get(GDELT_URL, params=params, headers={"User-Agent": user_agent}, timeout=timeout)
                if r.status_code == 429 or r.status_code >= 500 or not r.text.lstrip().startswith("{"):
                    if attempt == 0:
                        time.sleep(15)
                        continue
                break
            r.raise_for_status()
            got = parse_gdelt(r.json(), q)
            for it in got:
                if q in edition_queries:
                    it["edition"] = edition_queries[q]
            log.info("gdelt ok: %d items for %s", len(got), q[:60])
            out.extend(got)
            ok += 1
        except Exception as exc:
            log.warning("gdelt failed for %s: %s", q[:60], exc)
            failed += 1
    if health is not None:
        health["GDELT news search"] = f"{len(out)} items ({ok} searches ok, {failed} failed)"
    return out


def recent(items: list[dict], hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    keep = []
    for it in items:
        d = _parse_date(it.get("published")) if it.get("published") else None
        if d is None or d >= cutoff:  # undated items are kept; scoring treats them as older
            keep.append(it)
    return keep
