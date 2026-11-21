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
        return r.json()

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

    def create_post(self, payload: dict) -> dict:
        r = self.s.post(self.base + "/posts", json=payload, timeout=60, allow_redirects=False)
        if r.status_code >= 300:
            raise RuntimeError(f"WordPress {r.status_code}: {r.text[:400]}")
        d = r.json()
        if not isinstance(d, dict) or not d.get("id"):
            raise RuntimeError("WordPress did not return a post ID")
        return d


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
    payload = {
        "title": draft["title"],
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
        },
        **tax,
    }
    if cat:
        payload["categories"] = [cat]
    return payload, extra
