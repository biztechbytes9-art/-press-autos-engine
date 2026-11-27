"""Official launch videos from the maker's own YouTube channel (embedding is allowed by YouTube).

Needs a free YouTube Data API key (secret YOUTUBE_API_KEY). A video is used only if:
- its title contains the model words of our article (e.g. "innova hycross"),
- it was published recently (default 60 days),
- the channel name starts with the brand and looks official ("Toyota India", "Bajaj Auto", "Mahindra Automotive"),
  and the channel has at least 50,000 subscribers (fan channels rarely pass both).
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("engine.video")
API = "https://www.googleapis.com/youtube/v3"
OFFICIAL_WORDS = ("auto", "automotive", "motor", "motors", "india", "official", "global", "cars", "motorcycles",
                  "motorrad", "group", "usa", "uk", "electric", "ev", "two wheelers", "2 wheelers", "kirloskar")


def find_official_video(brand: str, model_words: str, cfg: dict | None = None, env=None) -> dict | None:
    env = env if env is not None else os.environ
    key = env.get("YOUTUBE_API_KEY", "")
    ycfg = (cfg or {}).get("youtube") or {}
    if not key or not brand or not model_words or not ycfg.get("enabled", True):
        return None
    words = [w for w in re.findall(r"[a-z0-9]+", model_words.lower()) if w not in brand.lower().split()]
    if not words:
        return None
    after = (datetime.now(timezone.utc) - timedelta(days=int(ycfg.get("max_age_days", 60)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get(API + "/search", timeout=20, params={
            "key": key, "part": "snippet", "type": "video", "maxResults": 10, "order": "relevance",
            "q": f"{brand} {' '.join(words)}", "publishedAfter": after, "videoEmbeddable": "true"})
        r.raise_for_status()
        items = r.json().get("items") or []
    except Exception as exc:  # noqa: BLE001
        log.info("youtube search failed: %s", exc)
        return None
    b = brand.lower()
    cands = []
    for it in items:
        sn = it.get("snippet") or {}
        ch = (sn.get("channelTitle") or "").lower()
        title = (sn.get("title") or "").lower()
        if not ch.startswith(b.split()[0]):
            continue
        rest = ch[len(b):] if ch.startswith(b) else ch[len(b.split()[0]):]
        if rest.strip() and not any(w in rest for w in OFFICIAL_WORDS):
            continue
        if not all(w in title for w in words):
            continue
        cands.append({"id": (it.get("id") or {}).get("videoId"), "title": sn.get("title", ""),
                      "channel": sn.get("channelTitle", ""), "channel_id": sn.get("channelId", "")})
    if not cands:
        return None
    try:
        ids = ",".join({c["channel_id"] for c in cands})
        r = requests.get(API + "/channels", timeout=20, params={"key": key, "part": "statistics", "id": ids})
        r.raise_for_status()
        subs = {c["id"]: int((c.get("statistics") or {}).get("subscriberCount") or 0) for c in r.json().get("items") or []}
    except Exception as exc:  # noqa: BLE001
        log.info("youtube channel lookup failed: %s", exc)
        return None
    for c in cands:
        if c["id"] and subs.get(c["channel_id"], 0) >= int(ycfg.get("min_subscribers", 50000)):
            return c
    return None


def embed_block(video: dict) -> str:
    url = f"https://www.youtube.com/watch?v={video['id']}"
    cap = f"Video: {video['channel']}"
    return ('<!-- wp:embed {"url":"' + url + '","type":"video","providerNameSlug":"youtube","responsive":true,'
            '"className":"wp-embed-aspect-16-9 wp-has-aspect-ratio"} -->\n'
            '<figure class="wp-block-embed is-type-video is-provider-youtube wp-block-embed-youtube wp-embed-aspect-16-9 '
            'wp-has-aspect-ratio"><div class="wp-block-embed__wrapper">\n' + url + '\n</div>'
            f'<figcaption class="wp-element-caption">{cap}</figcaption></figure>\n<!-- /wp:embed -->')
