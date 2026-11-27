"""Editor's assistant: revisits the engine's pending drafts every hour.

Images:  looks again for official press photos (makers often publish hours after the news), replaces a generated
         news card with the official photo (credited), and adds a second official photo inside the article.
SEO:     fills missing focus keywords / meta descriptions, puts the keyword in image alt text, adds "Also read"
         links to related articles published since, and writes an SEO checklist into the review notes.

It only touches drafts written by the engine (the pipeline user), never publishes, and never changes the facts:
article text is only extended with a photo or an "Also read" list; anything else goes into the review notes.

Run: python -m engine.assistant [--max-posts 15]
"""
import argparse
import html as htmlmod
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from . import blocks, extract, images
from .config import ROOT, load_config, load_secrets
from .llm import NoModelAvailable, Router
from .run import edition_url, enabled_editions
from .store import Store
from .wordpress import WordPress, upload_media

log = logging.getLogger("engine.assistant")
NOTE_MARK = "SEO check (assistant):"
KIT_MARK = "Featured image kit (Gemini app):"


# ------------------------------------------------------------------ SEO checklist (pure, testable)
def plain(html: str) -> str:
    return " ".join(htmlmod.unescape(re.sub(r"<[^>]+>", " ", html or "")).split())


def seo_audit(title: str, slug: str, content: str, meta_desc: str, keyword: str, alt: str, site_host: str) -> tuple[int, list[str]]:
    kw = (keyword or "").split(",")[0].strip().lower()
    text = plain(content).lower()
    words = len(text.split())
    missing = []
    if not kw:
        return 0, ["No focus keyword."]
    t = (title or "").lower()
    checks = [
        (kw in t, "Focus keyword is not in the title."),
        (kw in t and t.find(kw) <= max(10, len(t) // 2), "Focus keyword is not near the start of the title."),
        (kw in (meta_desc or "").lower(), "Focus keyword is not in the meta description."),
        (all(w in (slug or "") for w in re.findall(r"[a-z0-9]+", kw)), "Focus keyword is not in the URL."),
        (kw in text[: max(300, len(text) // 10)], "Focus keyword is not in the first paragraph."),
        (any(kw in plain(h).lower() for h in re.findall(r"<h[23][^>]*>(.*?)</h[23]>", content or "", re.S | re.I)),
         "Focus keyword is not in any subheading: add it to one heading."),
        (kw in (alt or "").lower(), "Focus keyword is not in the featured image alt text."),
        (0.4 <= (text.count(kw) * len(kw.split()) * 100 / max(words, 1)) <= 2.5,
         f"Keyword density is off ({text.count(kw)} uses in {words} words; aim for about 1%)."),
        (words >= 600, f"Article is {words} words; 600+ helps if the sources support more detail."),
        (bool(re.search(r'href="https?://(?!(?:[a-z0-9-]+\.)*' + re.escape(site_host) + r')', content or "")),
         "No link to an outside source in the text."),
        (bool(re.search(r'href="https?://(?:[a-z0-9-]+\.)*' + re.escape(site_host), content or "")),
         "No internal link to another press.autos article."),
        (len(title or "") <= 65, f"Title is {len(title or '')} characters; 65 or fewer avoids being cut off in Google."),
    ]
    passed = 0
    for ok, msg in checks:
        if ok:
            passed += 1
        else:
            missing.append(msg)
    return round(passed * 100 / len(checks)), missing


def merge_notes(old: str, score: int, missing: list[str], kit: str = "") -> str:
    keep = (old or "").split(NOTE_MARK)[0]
    old_kit = ""
    if KIT_MARK in keep:
        keep, old_kit = keep.split(KIT_MARK, 1)
        old_kit = KIT_MARK + old_kit
    keep = keep.rstrip()
    kit = kit or old_kit.strip()
    line = f"{NOTE_MARK} {score}/100." + ((" To improve: " + " ".join(missing)) if missing else " All checks pass.")
    return "\n\n".join(x for x in (keep, kit, line) if x).strip()


def key_facts_from(content: str) -> list[str]:
    box = re.search(r'pa-keyfacts.*?<ul[^>]*>(.*?)</ul>', content or "", re.S | re.I)
    return [plain(li) for li in re.findall(r"<li[^>]*>(.*?)</li>", box.group(1), re.S)] if box else []


# ------------------------------------------------------------------ helpers
def ensure_table(store: Store) -> None:
    store.db.execute("""CREATE TABLE IF NOT EXISTS assistant (
        site TEXT, post_id INTEGER, checked_at TEXT, photo_done INTEGER DEFAULT 0, tries INTEGER DEFAULT 0,
        PRIMARY KEY (site, post_id))""")


def due(store: Store, site: str, pid: int, now: datetime) -> bool:
    row = store.db.execute("SELECT checked_at, photo_done, tries FROM assistant WHERE site=? AND post_id=?", (site, pid)).fetchone()
    if not row:
        return True
    last = datetime.fromisoformat(row["checked_at"])
    wait = timedelta(hours=1 if row["tries"] < 3 else 4)
    if row["photo_done"]:
        wait = timedelta(hours=6)
    return now - last >= wait


def mark(store: Store, site: str, pid: int, now: datetime, photo_done: bool) -> None:
    store.db.execute("""INSERT INTO assistant (site, post_id, checked_at, photo_done, tries) VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(site, post_id) DO UPDATE SET checked_at=excluded.checked_at,
        photo_done=MAX(photo_done, excluded.photo_done), tries=tries+1""",
                     (site, pid, now.isoformat(), int(photo_done)))


def suggest_keywords(ai: Router, title: str, excerpt: str, text: str) -> dict:
    s = ("You are an SEO editor for an automotive news site. Suggest what people would type into Google to find this "
         "article. The focus keyword must be 2-5 words that appear in the title. Reply as JSON.")
    u = (f"TITLE: {title}\nEXCERPT: {excerpt}\nARTICLE START: {text[:1500]}\n\n"
         '{"focus_keyword": "...", "secondary_keywords": ["...", "..."], '
         '"meta_description": "140-155 characters, includes the focus keyword, only facts from the article"}')
    return ai.ask_json("triage", s, u, max_tokens=400)


# ------------------------------------------------------------------ one draft
def assist(cfg: dict, wp: WordPress, ai: Router | None, p: dict, site_host: str, edition: dict, ed_key: str) -> tuple[dict, list[str], bool]:
    """Returns (fields to update, report lines, photo_done)."""
    title = htmlmod.unescape((p.get("title") or {}).get("raw") or (p.get("title") or {}).get("rendered") or "")
    content = (p.get("content") or {}).get("raw") or (p.get("content") or {}).get("rendered") or ""
    excerpt = (p.get("excerpt") or {}).get("raw") or plain((p.get("excerpt") or {}).get("rendered") or "")
    meta = dict(p.get("meta") or {})
    upd: dict = {}
    new_meta: dict = {}
    did: list[str] = []

    # --- keywords and meta description
    kw = (meta.get("rank_math_focus_keyword") or "").strip()
    desc = (meta.get("rank_math_description") or "").strip()
    if (not kw or not desc) and ai is not None:
        try:
            sug = suggest_keywords(ai, title, excerpt, plain(content))
            if not kw and sug.get("focus_keyword"):
                extra = [k.strip().lower() for k in (sug.get("secondary_keywords") or []) if isinstance(k, str)]
                kw = ",".join(dict.fromkeys([sug["focus_keyword"].strip().lower()] + extra))[:250]
                new_meta["rank_math_focus_keyword"] = kw
                did.append(f"focus keyword: {kw.split(',')[0]}")
            if not desc and sug.get("meta_description"):
                desc = sug["meta_description"].strip()[:160]
                new_meta["rank_math_description"] = desc
                did.append("meta description")
        except (NoModelAvailable, ValueError, json.JSONDecodeError) as exc:
            log.info("keyword suggestion skipped: %s", exc)
    if not desc and excerpt:
        desc = plain(excerpt)[:160]
        new_meta["rank_math_description"] = desc
        did.append("meta description (from excerpt)")
    main_kw = kw.split(",")[0].strip()

    # --- featured image: replace a generated card with an official photo when one exists now
    media, alt, photo_done = {}, "", False
    ref_photo, ref_credit = "", ""
    if p.get("featured_media"):
        r = wp.s.get(f"{wp.base}/media/{p['featured_media']}", params={"context": "edit"}, timeout=30, allow_redirects=False)
        media = r.json() if r.status_code == 200 else {}
        alt = media.get("alt_text") or ""
    is_card = not media or bool(re.search(r"-(card|share)(-\d+)?\.jpe?g$", media.get("source_url", ""), re.I))
    photo_done = not is_card
    if media and is_card:
        # Generated text cards are no longer used: take them off the draft (it gets a real photo or none).
        upd["featured_media"] = 0
        media, alt = {}, ""
        did.append("generated card removed")
    if is_card and (cfg.get("images") or {}).get("official_photos", True):
        brands = []
        if p.get("pa_brand"):
            rb = wp.s.get(wp.base + "/pa_brand", params={"include": ",".join(map(str, p["pa_brand"])), "_fields": "id,name"},
                          timeout=30, allow_redirects=False)
            brands = [b["name"] for b in rb.json()] if rb.status_code == 200 else []
        try:
            src_urls = [s.get("url") for s in json.loads(meta.get("pa_sources_json") or "[]") if isinstance(s, dict)]
        except (TypeError, ValueError):
            src_urls = []
        srcs = []
        for u in src_urls[:4]:
            if u and images.is_official(cfg, u) and images.is_press_page(u):
                page = extract.fetch_article(u, cfg["site"]["user_agent"])
                if page:
                    srcs.append(page)
        draft = {"title": title, "slug": p.get("slug", ""), "focus_keyword": main_kw}
        facts = {"brands": brands}
        pick = images.choose_images(dict(cfg, images=dict(cfg.get("images") or {}, news_card=False)),
                                    srcs, facts, draft, edition, ed_key, [])
        feat = pick.get("featured")
        if feat and feat[5] == "official":
            data, mime, name, alt_new, caption, kind = feat
            m = upload_media(wp, data, mime, name, alt_new, caption)
            upd["featured_media"] = m["id"]
            alt = m.get("alt_text") or alt_new
            photo_done = True
            did.append(f"official photo set as featured image ({caption})")
            inl = pick.get("inline")
            if inl and "wp:image" not in content:
                m2 = upload_media(wp, inl[0], inl[1], inl[2], inl[3], inl[4])
                content = images.insert_after_paragraph(content, images.image_block(m2, inl[4]), 3)
                upd["content"] = content
                did.append("second official photo added inside the article")
            ref_photo, ref_credit = (m.get("source_url") or ""), caption

    # --- in-article photos with free licences (Wikimedia Commons) when the article has none yet
    query = main_kw or title
    if "wp:image" not in content and (cfg.get("images") or {}).get("commons_photos", True):
        query_words = " ".join(re.findall(r"[a-z0-9]+", query.lower())[:3])
        placed = 0
        for cand in images.commons_candidates(query_words, cfg["site"]["user_agent"], n=2):
            got = images.download_image(cand["url"], cfg["site"]["user_agent"])
            if not got:
                continue
            data, mime = images.optimise(got[0], got[1])
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
            fname = re.sub(r"[^a-z0-9]+", "-", cand["name"].lower()).strip("-")[:60] + f".{ext}"
            mi = upload_media(wp, data, mime, fname, cand["name"][:150], cand["credit_html"])
            content = images.insert_after_paragraph(content, images.image_block(mi, cand["credit_html"]), 3 + placed * 3)
            placed += 1
            if not ref_photo:
                ref_photo, ref_credit = cand.get("page") or mi.get("source_url", ""), cand["credit_text"]
        if placed:
            upd["content"] = content
            did.append(f"{placed} Wikimedia Commons photo(s) added with credits")

    # --- featured image kit for Gemini (option A: made by Karim in the Gemini app)
    kit = ""
    engine_made = not media or media.get("author") == p.get("author")
    if KIT_MARK not in (meta.get("pa_review_notes") or "") and engine_made:
        label = "" if ed_key == "global" else edition.get("name", "").replace("press.autos", "").strip()
        if not ref_photo and media.get("source_url") and not is_card:
            ref_photo, ref_credit = media["source_url"], plain(((media.get("caption") or {}).get("rendered")) or "") or "official photo"
        kit = images.featured_kit(title, key_facts_from(content), ref_photo, ref_credit, label)

    # --- keyword in the alt text of the featured image (only media the engine uploaded)
    if main_kw and media and not upd.get("featured_media") and main_kw.lower() not in alt.lower():
        new_alt = f"{main_kw[:1].upper()}{main_kw[1:]}: {title}"[:200]
        r = wp.s.post(f"{wp.base}/media/{media['id']}", json={"alt_text": new_alt}, timeout=30, allow_redirects=False)
        if r.status_code < 300:
            alt = new_alt
            did.append("keyword added to image alt text")

    # --- "Also read" internal links, once related articles exist
    if "pa-alsoread" not in content:
        cats = p.get("categories") or []
        rel = [x for x in wp.related(cats[0] if cats else None) if x.get("link") and title[:30] not in x.get("title", "")]
        if rel:
            content = content + "\n\n" + blocks.also_read(rel)
            upd["content"] = content
            did.append(f"{len(rel)} 'Also read' links")

    # --- checklist into the review notes
    score, missing = seo_audit(title, p.get("slug", ""), content, desc, kw, alt, site_host)
    notes = merge_notes(meta.get("pa_review_notes") or "", score, missing, kit)
    if notes != (meta.get("pa_review_notes") or ""):
        new_meta["pa_review_notes"] = notes
    if new_meta:
        upd["meta"] = new_meta
    line = f"- {title[:80]}: SEO {score}/100" + (f"; did: {', '.join(did)}" if did else "")
    return upd, [line], photo_done


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-posts", type=int, default=15)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    secrets = load_secrets()
    store = Store(ROOT / "state" / "engine.db")
    ensure_table(store)
    try:
        ai = Router(cfg["llm"], store, daily_max_usd=float(cfg["limits"].get("daily_max_usd", 0)))
    except Exception:  # noqa: BLE001
        ai = None
    now = datetime.now(timezone.utc)
    report = ["## press.autos editor's assistant", ""]
    done = 0
    try:
        for ed_key, ed in enabled_editions(cfg).items():
            base = edition_url(cfg, ed_key, secrets.wp_url)
            wp = WordPress(base, secrets.wp_user, secrets.wp_app_password, cfg["site"]["user_agent"])
            try:
                me = wp.check()
            except Exception as exc:  # noqa: BLE001
                report.append(f"- {ed.get('name', ed_key)}: skipped ({str(exc)[:200]})")
                continue
            r = wp.s.get(wp.base + "/posts", params={"status": "pending", "author": me.get("id"), "per_page": 50,
                                                      "context": "edit", "orderby": "date", "order": "desc"},
                         timeout=60, allow_redirects=False)
            if r.status_code != 200:
                report.append(f"- {ed.get('name', ed_key)}: could not list drafts (HTTP {r.status_code})")
                continue
            host = base.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")
            host = ".".join(host.split(".")[-2:])  # press.autos: links to any edition count as internal
            posts = r.json()
            report.append(f"**{ed.get('name', ed_key)}**: {len(posts)} pending engine drafts")
            for p in posts:
                if done >= args.max_posts:
                    break
                if not due(store, base, p["id"], now):
                    continue
                try:
                    upd, lines, photo = assist(cfg, wp, ai, p, host, ed, ed_key)
                    if upd:
                        u = wp.s.post(f"{wp.base}/posts/{p['id']}", json=upd, timeout=60, allow_redirects=False)
                        if u.status_code >= 300:
                            lines = [f"- update failed for post {p['id']}: HTTP {u.status_code} {u.text[:150]}"]
                    report += lines
                    mark(store, base, p["id"], now, photo)
                    done += 1
                except Exception as exc:  # noqa: BLE001  (one draft must never stop the others)
                    log.exception("assistant failed on %s", p.get("id"))
                    report.append(f"- post {p.get('id')}: error {exc.__class__.__name__}: {str(exc)[:150]}")
                    mark(store, base, p["id"], now, False)
        report.append(f"\nDrafts checked this run: {done}.")
    finally:
        store.close()
        save_report(report)


def save_report(lines: list[str]) -> None:
    text = "\n".join(lines)
    print(text)
    import os
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    log_path = ROOT / "state" / "assistant.md"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"<!-- run -->\n# Assistant {stamp}\n\n" + text.replace("## press.autos editor's assistant", "").strip() + "\n\n"
    old = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    runs = [r for r in old.split("<!-- run -->\n") if r.strip()][:29]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(entry + "".join("<!-- run -->\n" + r for r in runs), encoding="utf-8")


if __name__ == "__main__":
    main()
