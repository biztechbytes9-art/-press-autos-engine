"""Weekly evergreen guides (the "utility" part of the content mix).

Topics live in config.yaml under `guides:`, per edition, each with official sources (ministries, NHAI, Parivahan,
PIB press releases, PDFs) and optional Wikipedia background. Each run writes the next unused topic for each edition
as a PENDING draft, through the same fact table -> writer -> second-model fact check -> revise pipeline as news.

Run: python -m engine.guides [--topics 1] [--dry-run]
"""
import argparse
import hashlib
import logging
from datetime import datetime, timezone

from . import blocks, extract
from .config import ROOT, load_config, load_secrets
from .llm import NoModelAvailable, Router
from .run import build_facts, edition_url, enabled_editions, summary, write_story
from .store import Store
from .wordpress import WordPress, build_payload

log = logging.getLogger("engine.guides")

GUIDE_STYLE = (
    "This is an EVERGREEN GUIDE, not a news report. Open with a one-sentence answer to the reader's main question. "
    "Use question-style '## ' headings in this order where the facts allow: what it is; who is eligible or who it is for; "
    "how to apply or use it, as a numbered step-by-step list; costs, fees or limits; documents needed; common mistakes; "
    "then '## Frequently asked questions' with 3-5 '### ' questions. For any rule, fee or limit that can change, write "
    "'as of {month}' and name the authority that sets it. Never invent a fee, date, form number, website or step that is "
    "not in the fact table; if something is unknown, say readers should check the official site and name it. "
    "Title format: '<Topic>: <what the reader gets>' (e.g. 'BH series registration: eligibility, road tax and how to apply')."
)


def ensure_table(store: Store) -> None:
    store.db.execute("CREATE TABLE IF NOT EXISTS guides (edition TEXT, topic_id TEXT, created_at TEXT, wp_id INTEGER, "
                     "PRIMARY KEY (edition, topic_id))")


def topic_id(t: dict) -> str:
    return t.get("id") or hashlib.md5(t["topic"].lower().encode()).hexdigest()[:10]


def next_topic(store: Store, ed_key: str, topics: list[dict]) -> dict | None:
    done = {r["topic_id"] for r in store.db.execute("SELECT topic_id FROM guides WHERE edition=?", (ed_key,))}
    for t in topics:
        if topic_id(t) not in done:
            return t
    return None


def collect_sources(cfg: dict, topic: dict) -> list[dict]:
    ua = cfg["site"]["user_agent"]
    chars = int(cfg["limits"].get("guide_source_chars", 6000))
    out = []
    for url in topic.get("sources") or []:
        page = extract.fetch_article(url, ua)
        if page:
            page["text"] = page["text"][:chars]
            out.append(page)
    for title in topic.get("wikipedia") or []:
        page = extract.wikipedia_text(title, ua)
        if page:
            page["text"] = page["text"][:chars]
            out.append(page)
    return out[: int(cfg["limits"].get("guide_max_sources", 4))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topics", type=int, default=1, help="guides per edition this run")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    secrets = load_secrets(require=not args.dry_run)
    store = Store(ROOT / "state" / "engine.db")
    ensure_table(store)
    report = ["## press.autos guides", ""]
    try:
        ai = Router(cfg["llm"], store, daily_max_usd=float(cfg["limits"].get("daily_max_usd", 0)))
        month = datetime.now(timezone.utc).strftime("%B %Y")
        for ed_key, ed in enabled_editions(cfg).items():
            topics = ((cfg.get("guides") or {}).get(ed_key)) or []
            for _ in range(args.topics):
                t = next_topic(store, ed_key, topics)
                if not t:
                    report.append(f"- {ed.get('name', ed_key)}: no unused guide topics left; add more under `guides:` in config.yaml.")
                    break
                srcs = collect_sources(cfg, t)
                if sum(len(s["text"]) for s in srcs) < 1500 or not any("wikipedia" not in s["url"] for s in srcs):
                    report.append(f"- skipped '{t['topic']}': official sources could not be read "
                                  f"({', '.join(sorted(extract.FAILS)) or 'no text'}). Check the URLs in config.yaml.")
                    store.db.execute("INSERT OR REPLACE INTO guides VALUES (?, ?, ?, ?)",
                                     (ed_key, topic_id(t) + "-skipped-" + datetime.now(timezone.utc).strftime("%Y%m%d"),
                                      store_now(), None))
                    continue
                pseudo = {"title": t["topic"], "key": "guide-" + topic_id(t), "triage": {"category": t.get("category")}}
                facts = build_facts(ai, cfg, pseudo, srcs)
                if not facts:
                    report.append(f"- skipped '{t['topic']}': not enough facts in the sources.")
                    continue
                facts["category"] = t.get("category") or facts.get("category")
                facts["country"] = ed_key if ed_key != "global" else "global"
                facts["model"] = ""
                style_ed = dict(ed, style=(ed.get("style", "") + " " + GUIDE_STYLE.format(month=month)).strip(),
                                local_section="")
                draft, notes = write_story(ai, cfg, facts, srcs, style_ed)
                if args.dry_run:
                    report.append(f"- (dry run) would draft guide for {ed.get('name', ed_key)}: {draft.get('title')}")
                    continue
                wp = WordPress(edition_url(cfg, ed_key, secrets.wp_url), secrets.wp_user, secrets.wp_app_password,
                               cfg["site"]["user_agent"])
                wp.check()
                html = blocks.key_facts(draft.get("key_facts") or []) + blocks.to_blocks(draft["body_markdown"])
                notes = ["Evergreen guide: check every rule, fee and step against the official sources before publishing; "
                         "add 'Last checked: " + month + "' if you change anything."] + notes
                payload, extra = build_payload(wp, draft, facts, html, srcs, pseudo["key"], notes, cfg["site"]["post_status"])
                payload["meta"]["pa_edition"] = ed_key
                post = wp.create_post(payload)
                store.db.execute("INSERT OR REPLACE INTO guides VALUES (?, ?, ?, ?)",
                                 (ed_key, topic_id(t), store_now(), post.get("id")))
                store.commit()
                report.append(f"- drafted guide for **{ed.get('name', ed_key)}**: {draft['title']} (WordPress ID {post.get('id')})")
    except NoModelAvailable as exc:
        report.append(f"Stopped: free AI limits reached for now ({exc}).")
    finally:
        store.commit()
        store.close()
        summary("\n".join(report))


def store_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    main()
