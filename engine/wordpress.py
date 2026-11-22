"""Post drafts to WordPress through its REST API, as the Pipeline user (cannot publish)."""
import logging

import requests

log = logging.getLogger("engine.wordpress")


class SiteNotReady(RuntimeError):
    """The edition's WordPress site does not exist yet (or redirects elsewhere)."""


class WordPress:
    def __init__(self, base_url: str, user: str, app_password: str, user_agent: str):
        self.base = base_url.rstrip("/") + "/wp-json/wp/v2"
        self.s = requests.Session()
        self.s.auth = (user, app_password.replace(" ", ""))
        self.s.headers["User-Agent"] = user_agent
        self._term_cache: dict[tuple[str, str], int | None] = {}

    def check(self) -> dict:
        r = self.s.get(self.base + "/users/me", params={"context": "edit"}, timeout=30, allow_redirects=False)
        if 300 <= r.status_code < 400 or r.status_code == 404:
            # On a multisite network a subdomain without a site redirects to the main site: never post there by mistake.
            raise SiteNotReady(f"{self.base.split('/wp-json')[0]} is not set up yet (HTTP {r.status_code}).")
        if r.status_code == 401:
            raise RuntimeError(f"{self.base}: WordPress rejected the login (401). Check WP_USER and WP_APP_PASSWORD, "
                             "that the pipeline user is added to this site, and that your host passes the Authorization header.")
        r.raise_for_status()
        me = r.json()
        caps = me.get("capabilities") or {}
        if caps and not caps.get("edit_posts"):
            site = self.base.split("/wp-json")[0]
            raise SiteNotReady(f"the pipeline user can't create posts on {site} (role: "
                               f"{', '.join(me.get('roles') or ['unknown'])}). In that site's Users screen, "
                               "set pipeline's role to 'Pipeline (automation)'.")
        return me

    def term_id(self, taxonomy: str, name_or_slug: str) -> int | None:
        key = (taxonomy, name_or_slug.lower())
        if key in self._term_cache:
            return self._term_cache[key]
        endpoint = "categories" if taxonomy == "category" else taxonomy
        found = None
        for params in ({"slug": slugify(name_or_slug)}, {"search": name_or_slug, "per_page": 20}):
            r = self.s.get(f"{self.base}/{endpoint}", params=params, timeout=30)
            if r.status_code != 200:
                break
            for t in r.json():
                if t.get("slug") == slugify(name_or_slug) or t.get("name", "").lower() == name_or_slug.lower():
                    found = t["id"]
                    break
            if found:
                break
        self._term_cache[key] = found
        return found

    def related(self, category_id: int | None, exclude_words: set[str] | None = None, n: int = 3) -> list[dict]:
        """Recent published posts in the same category, for "Also read" internal links."""
        if not category_id:
            return []
        try:
            r = self.s.get(self.base + "/posts", params={"categories": category_id, "per_page": 8, "status": "publish",
                                                         "_fields": "id,link,title"}, timeout=30, allow_redirects=False)
            if r.status_code != 200:
                return []
            out = []
            for p in r.json():
                title = ((p.get("title") or {}).get("rendered") or "").strip()
                if title and p.get("link"):
                    out.append({"title": __import__("html").unescape(title), "link": p["link"]})
            return out[:n]
        except Exception:  # noqa: BLE001
            return []

    def create_post(self, payload: dict) -> dict:
        r = self.s.post(self.base + "/posts", json=payload, timeout=60, allow_redirects=False)
        if r.status_code >= 300:
            raise RuntimeError(f"WordPress {r.status_code}: {r.text[:400]}")
        d = r.json()
        if not isinstance(d, dict) or not d.get("id"):
            raise RuntimeError("WordPress did not return a post ID")
        return d


def upload_media(wp: "WordPress", data: bytes, mime: str, filename: str, alt: str, caption: str) -> dict:
    """Upload an image to the media library (the Pipeline role may upload files)."""
    r = wp.s.post(wp.base + "/media", data=data, timeout=120, allow_redirects=False, headers={
        "Content-Type": mime, "Content-Disposition": f'attachment; filename="{filename}"'})
    if r.status_code >= 300:
        raise RuntimeError(f"media upload {r.status_code}: {r.text[:300]}")
    media = r.json()
    meta = {"alt_text": alt[:200]}
    if caption:
        meta["caption"] = caption
    r2 = wp.s.post(f"{wp.base}/media/{media['id']}", json=meta, timeout=60, allow_redirects=False)
    if r2.status_code < 300:
        media = r2.json()
    media.setdefault("alt_text", alt)
    return media


def slugify(s: str) -> str:
    import re
    s = s.lower().replace("&", "and")
    s = re.sub(r"[()]", "", s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def build_payload(wp: WordPress, draft: dict, facts: dict, content_html: str, sources: list[dict],
                  cluster_key: str, notes: list[str], status: str) -> tuple[dict, list[str]]:
    """Returns (payload, extra_notes)."""
    extra = []
    cat = wp.term_id("category", facts.get("category") or "")
    if not cat:
        extra.append(f"Category '{facts.get('category')}' not found; please set one.")
    tax = {}
    for field, taxonomy in (("brands", "pa_brand"), ("powertrain", "pa_powertrain"), ("body", "pa_body")):
        ids = []
        for name in facts.get(field) or []:
            tid = wp.term_id(taxonomy, name)
            if tid:
                ids.append(tid)
            else:
                extra.append(f"{taxonomy} term '{name}' not found; add it if needed.")
        if ids:
            tax[taxonomy] = ids
    src_meta = [{"title": s.get("title") or s["url"], "url": s["url"]} for s in sources]
    kw = (draft.get("focus_keyword") or "").strip().lower()
    extra_kw = [k.strip().lower() for k in (draft.get("secondary_keywords") or []) if isinstance(k, str) and k.strip()]
    keywords = ",".join(dict.fromkeys([k.replace(",", " ") for k in [kw] + extra_kw if k]))[:250]
    slug = slugify(draft.get("slug") or kw or draft["title"])
    slug = "-".join(slug.split("-")[:8])
    payload = {
        "title": draft["title"],
        "slug": slug,
        "content": content_html,
        "excerpt": draft.get("excerpt", ""),
        "status": status,
        "comment_status": "open",
        "meta": {
            "pa_ai_assisted": True,
            "pa_sources_json": __import__("json").dumps(src_meta, ensure_ascii=False),
            "pa_fact_table_json": __import__("json").dumps(facts, ensure_ascii=False)[:60000],
            "pa_cluster_id": cluster_key,
            "pa_edition": facts.get("country") or "global",
            "pa_review_notes": "\n".join(notes + extra),
            "pa_image_hint": draft.get("image_hint") or "",
            "rank_math_focus_keyword": keywords,
            "rank_math_description": (draft.get("excerpt") or "")[:160],
        },
        **tax,
    }
    if cat:
        payload["categories"] = [cat]
    return payload, extra
