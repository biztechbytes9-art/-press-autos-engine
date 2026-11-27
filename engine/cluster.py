"""Group headlines about the same story and score each story."""
import hashlib
import math
import re
from datetime import datetime, timezone

STOP = set("""a an and are as at be by for from has have in into is it its of on or that the this to with will
new after over says said than up out more first its how why what who when its vs via""".split())


def tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", title.lower())
    return {w for w in words if w not in STOP and len(w) > 1}


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    # overlap coefficient: robust when one headline is much longer than the other
    return inter / min(len(a), len(b))


def cluster_items(items: list[dict], threshold: float = 0.6, min_shared: int = 3) -> list[dict]:
    """Greedy clustering. Returns clusters {key, items, tokens}."""
    clusters: list[dict] = []
    for it in sorted(items, key=lambda x: x.get("published") or "", reverse=True):
        t = tokens(it["title"])
        best, best_sim = None, 0.0
        for c in clusters:
            sim = similarity(t, c["tokens"])
            if sim > best_sim and len(t & c["tokens"]) >= min_shared:
                best, best_sim = c, sim
        if best is not None and best_sim >= threshold:
            if all(x["url"] != it["url"] for x in best["items"]):
                best["items"].append(it)
                best["tokens"] |= t
        else:
            clusters.append({"items": [it], "tokens": set(t)})
    for c in clusters:
        lead = sorted(c["tokens"])
        c["key"] = hashlib.sha1(" ".join(lead[:12]).encode()).hexdigest()[:16]
        c["title"] = c["items"][0]["title"]
        c["domains"] = sorted({x["domain"] for x in c["items"]})
    return clusters


GENERIC = set("""price prices priced launch launches launched launching unveil unveils unveiled reveal reveals revealed
sales sale sold india indian global new model models update updated gets get set debut debuts specs features variant
variants rise hike cut drop jumps grows booking bookings delivery deliveries plan plans electric ev suv car cars bike
bikes motorcycle scooter rs lakh crore km kwh hp ps units year month october november december january
announces announced announce invests invest confirms confirmed teases teased spotted spied recall recalls recalled hits
crosses passes starts begins begin expands opens reports posts registers records adds reveals introduces showcases
unveiling tested testing""".split())
# Two-word brand names are not model names on their own.
BRAND_PAIRS = {("royal", "enfield"), ("maruti", "suzuki"), ("tata", "motors"), ("land", "rover"), ("aston", "martin"),
               ("rolls", "royce"), ("alfa", "romeo"), ("great", "wall"), ("hero", "motocorp"), ("ashok", "leyland"),
               ("bharat", "benz"), ("force", "motors"), ("mahindra", "mahindra"), ("hyundai", "motor"), ("kia", "india"),
               ("jsw", "motors"), ("mg", "motor"), ("ola", "electric"), ("ather", "energy"), ("bajaj", "auto")}


def model_bigrams(title: str) -> set[tuple[str, str]]:
    """Adjacent pairs of distinctive words, e.g. ("innova", "hycross"), ("pulsar", "rs200")."""
    words = [w for w in re.findall(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", title.lower()) if w not in STOP]
    out = set()
    for a, b in zip(words, words[1:]):
        if a not in GENERIC and b not in GENERIC and not a.isdigit() and not b.isdigit() and (a, b) not in BRAND_PAIRS:
            out.add((a, b))
    return out


def is_repeat(cluster: dict, recent_titles: list[str], threshold: float = 0.6,
              same_model_titles: list[str] | None = None) -> bool:
    for t in recent_titles:
        tt = tokens(t)
        if len(tt & cluster["tokens"]) >= 3 and similarity(tt, tokens(cluster["title"])) >= threshold:
            return True
    # Within the last 48 hours, a second story naming the same model is a repeat
    # (e.g. three "Toyota Innova Hycross" drafts on one day with different angles and prices).
    mine = model_bigrams(cluster["title"])
    if mine:
        for t in same_model_titles or []:
            if mine & model_bigrams(t):
                return True
    return False


def _age_hours(item: dict) -> float:
    try:
        d = datetime.fromisoformat(item["published"])
        return max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 3600)
    except (KeyError, ValueError, TypeError):
        return 24.0


def score_cluster(c: dict, cfg: dict) -> float:
    """0..1: freshness 35%, coverage 30%, relevance 20%, authority 15%."""
    newest = min(_age_hours(i) for i in c["items"])
    fresh = math.exp(-newest / 12)  # halves roughly every 8 hours
    coverage = min(1.0, (len(c["domains"]) - 1) / 4) if len(c["domains"]) > 1 else 0.1
    text = " ".join(i["title"].lower() for i in c["items"])
    rel = 0.0
    for word, w in (cfg.get("relevance_keywords") or {}).items():
        if re.search(r"\b" + re.escape(word) + r"\b", text):
            rel += w
    rel = min(1.0, rel)
    auth_map = cfg.get("source_authority") or {}

    def auth(d: str) -> float:
        for dom, v in auth_map.items():
            if d == dom or d.endswith("." + dom):
                return v
        return 0.4

    authority = max(auth(d) for d in c["domains"])
    c["score_parts"] = {"fresh": round(fresh, 2), "coverage": round(coverage, 2),
                        "relevance": round(rel, 2), "authority": round(authority, 2)}
    return round(0.35 * fresh + 0.30 * coverage + 0.20 * rel + 0.15 * authority, 3)
