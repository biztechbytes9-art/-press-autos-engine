"""One engine run: discover -> cluster -> score -> triage -> fact table -> route to edition -> draft -> verify -> post.

Usage:
  python -m engine.run              # normal run (needs secrets)
  python -m engine.run --dry-run    # discover, score and triage only; posts nothing
"""
import argparse
import logging
import os
from collections import Counter

from . import blocks, checks, cluster, extract, sources, writer
from .config import ROOT, load_config, load_secrets
from .llm import BudgetExceeded, NoModelAvailable, Router
from .store import Store
from .wordpress import WordPress, build_payload

log = logging.getLogger("engine")


def summary(lines: list[str]) -> None:
    """Write a readable report to the GitHub Actions run page (and the log)."""
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


# ------------------------------------------------------------------ editions
def enabled_editions(cfg: dict) -> dict:
    eds = cfg.get("editions") or {"global": {"name": "press.autos", "url": "secret", "daily_max_articles": 12}}
    return {k: v for k, v in eds.items() if k == "global" or v.get("enabled")}


def route(cfg: dict, country: str | None) -> str | None:
    """Edition key for a story's country, or None to skip it."""
    c = (country or "global").strip().upper()
    if c in ("", "GLOBAL", "WORLD", "INTL"):
        return "global"
    if c == "UK":
        c = "GB"
    eds = enabled_editions(cfg)
    if c in eds:
        return c
    return "global" if cfg.get("local_without_edition", "global") == "global" else None


def edition_url(cfg: dict, key: str, secret_url: str) -> str:
    url = (cfg.get("editions") or {}).get(key, {}).get("url", "secret")
    return secret_url if url == "secret" else url.rstrip("/")


# ------------------------------------------------------------------ steps
def discover(cfg: dict, store: Store) -> list[dict]:
    ua = cfg["site"]["user_agent"]
    feeds = list(cfg.get("feeds", []))
    gd = dict(cfg.get("gdelt", {}))
    queries = list(gd.get("queries", []))
    for key, ed in enabled_editions(cfg).items():
        feeds += ed.get("feeds", []) or []
        queries += ed.get("gdelt_queries", []) or []
    gd["queries"] = queries
    items = sources.fetch_feeds(feeds, ua) + sources.fetch_gdelt(gd, ua)
    items = sources.recent(items, cfg["limits"]["lookback_hours"])
    uniq = {}
    for it in items:
        uniq.setdefault(it["url"], it)
    for it in uniq.values():
        store.add_item(it)
    return list(uniq.values())


def choose(cfg: dict, store: Store, items: list[dict]) -> list[dict]:
    clusters = cluster.cluster_items(items)
    done = store.drafted_keys()
    recent_titles = store.recent_draft_titles()
    fresh = []
    for c in clusters:
        if c["key"] in done or cluster.is_repeat(c, recent_titles):
            continue
        c["score"] = cluster.score_cluster(c, cfg)
        if c["score"] >= cfg["limits"]["min_score"]:
            fresh.append(c)
    fresh.sort(key=lambda c: c["score"], reverse=True)
    return fresh[:30]


def triage(ai: Router, cfg: dict, cands: list[dict]) -> list[dict]:
    if not cands:
        return []
    system, user = writer.triage_prompt(cands, cfg["categories"])
    res = ai.ask_json("triage", system, user, max_tokens=3500)
    keep = []
    for r in res if isinstance(res, list) else []:
        i = r.get("i")
        if not isinstance(i, int) or not 0 <= i < len(cands) or not r.get("keep"):
            continue
        c = cands[i]
        c["triage"] = r
        c["rank"] = 0.5 * c["score"] + 0.5 * (float(r.get("newsworthy", 5)) / 10)
        keep.append(c)
    keep.sort(key=lambda c: c["rank"], reverse=True)
    return keep


def gather_sources(cfg: dict, c: dict) -> list[dict]:
    limit = int(cfg["limits"].get("max_sources", 3))
    chars = int(cfg["limits"].get("source_chars", 9000))
    auth = cfg.get("source_authority") or {}

    def weight(it):
        for dom, v in auth.items():
            if it["domain"] == dom or it["domain"].endswith("." + dom):
                return v
        return 0.4

    got, domains = [], set()
    for it in sorted(c["items"], key=weight, reverse=True):
        if it["domain"] in domains:
            continue
        page = extract.fetch_article(it["url"], cfg["site"]["user_agent"])
        if page:
            page["title"] = page.get("title") or it["title"]
            page["text"] = page["text"][:chars]
            got.append(page)
            domains.add(it["domain"])
        if len(got) >= limit:
            break
    return got


def build_facts(ai: Router, cfg: dict, c: dict, srcs: list[dict]) -> dict | None:
    s, u = writer.facts_prompt(c, srcs, cfg)
    facts = ai.ask_json("writer", s, u, max_tokens=2500)
    if not isinstance(facts, dict) or not facts.get("enough_for_article"):
        return None
    if c.get("triage", {}).get("category") and not facts.get("category"):
        facts["category"] = c["triage"]["category"]
    return facts


def write_story(ai: Router, cfg: dict, facts: dict, srcs: list[dict], edition: dict) -> tuple[dict, list[str]]:
    s, u = writer.write_prompt(facts, srcs, cfg, edition)
    draft = ai.ask_json("writer", s, u, max_tokens=3000)

    notes = []
    src_text = "\n".join(x["text"] for x in srcs)
    overlap = max(checks.copy_overlap(draft["body_markdown"], x["text"]) for x in srcs)
    s, u = writer.verify_prompt(draft, facts, srcs)
    ver = ai.ask_json("verify", s, u, max_tokens=1500)
    issues = list(ver.get("issues") or [])
    if overlap > 0.06:
        issues.append(f"About {overlap:.0%} of the wording matches a source; rewrite those passages in fresh words.")
    if issues:
        s, u = writer.revise_prompt(draft, issues, facts)
        draft = ai.ask_json("writer", s, u, max_tokens=3000)
        s, u = writer.verify_prompt(draft, facts, srcs)
        ver2 = ai.ask_json("verify", s, u, max_tokens=1500)
        notes += ["Fact-check: " + i for i in (ver2.get("issues") or [])]
        overlap = max(checks.copy_overlap(draft["body_markdown"], x["text"]) for x in srcs)
        if overlap > 0.06:
            notes.append(f"Wording overlap with a source is {overlap:.0%}; rephrase before publishing.")
    odd = checks.unsupported_numbers(draft["body_markdown"], src_text + "\n" + str(facts))
    if odd:
        notes.append("Numbers not found in sources (may be calculations): " + ", ".join(odd[:10]))
    if not facts.get("is_confirmed", True):
        notes.append("Story is not officially confirmed; check the wording says so.")
    hint = draft.get("image_hint")
    notes.append("Add a featured image" + (f" (suggested source: {hint})" if hint else "") + ".")
    return draft, notes


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg = load_config()
    lim = cfg["limits"]
    eds = enabled_editions(cfg)
    secrets = load_secrets(require=not args.dry_run)
    store = Store(ROOT / "state" / "engine.db")
    report = ["## press.autos engine run", ""]
    try:
        room = {k: max(0, int(v.get("daily_max_articles", 6)) - len(store.drafts_today(k))) for k, v in eds.items()}
        total_left = max(0, int(lim.get("daily_max_total_articles", 999)) - len(store.drafts_today()))
        items = discover(cfg, store)
        cands = choose(cfg, store, items)
        report.append(f"Discovered {len(items)} recent items, {len(cands)} candidate stories above score {lim['min_score']}.")
        report.append("Room left today: " + ", ".join(f"{eds[k].get('name', k)} {v}" for k, v in room.items())
                      + f". Spend today: ${store.spend_today():.2f}.")
        ai = Router(cfg["llm"], store, float(lim.get("daily_max_usd", 0) or 0))
        report.append("AI models available: " + (", ".join(ai.available()) or "none (add an API key secret)") + ".")
        if args.dry_run and not ai.available("triage"):
            report += ["", "Top candidates (no AI key, triage skipped):"]
            report += [f"- {c['score']:.2f} {c['title']} [{', '.join(c['domains'][:3])}]" for c in cands[:10]]
            return
        if sum(room.values()) <= 0 or total_left <= 0:
            report.append("Daily limits reached; nothing to do.")
            return

        if not ai.available("writer"):
            report.append("No AI key for writing is set; add GROQ_API_KEY (free) in GitHub secrets.")
            return
        picked = triage(ai, cfg, cands)
        report += ["", "### Chosen by triage"] + [
            f"- {c['rank']:.2f} [{c['triage'].get('category')}, {c['triage'].get('audience')}] {c['title']}"
            for c in picked[:10]]
        for c in cands:
            if c not in picked:
                store.reject(c["key"], "triage")
        if args.dry_run:
            return

        wps: dict[str, WordPress] = {}

        def wp_for(key: str) -> WordPress:
            if key not in wps:
                wp = WordPress(edition_url(cfg, key, secrets.wp_url), secrets.wp_user, secrets.wp_app_password,
                               cfg["site"]["user_agent"])
                wp.check()
                wps[key] = wp
            return wps[key]

        cat_count = {k: Counter(r["category"] for r in store.drafts_today(k)) for k in eds}
        made = 0
        for c in picked:
            if made >= lim["per_run_max_articles"] or made >= total_left:
                break
            guess = route(cfg, c["triage"].get("audience"))
            if guess is None:
                store.reject(c["key"], "local story without an edition")
                continue
            if room.get(guess, 0) <= 0:
                continue  # that edition is full today; the story may still be picked up tomorrow if fresh
            srcs = gather_sources(cfg, c)
            if not srcs:
                store.reject(c["key"], "no readable source")
                report.append(f"- skipped (no readable source): {c['title']}")
                continue
            try:
                facts = build_facts(ai, cfg, c, srcs)
                if facts is None:
                    store.reject(c["key"], "not enough facts")
                    report.append(f"- skipped (not enough facts): {c['title']}")
                    continue
                ed_key = route(cfg, facts.get("country"))
                if ed_key is None:
                    store.reject(c["key"], "local story without an edition")
                    continue
                if room.get(ed_key, 0) <= 0:
                    continue
                cat = facts.get("category") or c["triage"].get("category") or ""
                max_per_cat = max(1, int(int(eds[ed_key].get("daily_max_articles", 6)) * lim["max_category_share"]))
                if cat and cat_count[ed_key][cat] >= max_per_cat:
                    continue
                draft, notes = write_story(ai, cfg, facts, srcs, eds[ed_key])
                wp = wp_for(ed_key)
                html = blocks.to_blocks(draft["body_markdown"])
                payload, extra = build_payload(wp, draft, facts, html, srcs, c["key"], notes, cfg["site"]["post_status"])
                payload["meta"]["pa_edition"] = "global" if ed_key == "global" else ed_key
                post = wp.create_post(payload)
            except (BudgetExceeded, NoModelAvailable):
                raise
            except Exception as exc:
                log.exception("draft failed")
                store.reject(c["key"], f"error: {exc}"[:200])
                report.append(f"- failed: {c['title']}: {exc}")
                continue
            store.add_draft(c["key"], draft["title"], cat, post.get("id"), "posted", notes + extra, ed_key)
            store.commit()
            cat_count[ed_key][cat] += 1
            room[ed_key] -= 1
            made += 1
            report.append(f"- drafted for **{eds[ed_key].get('name', ed_key)}**: {draft['title']} "
                          f"(WordPress ID {post.get('id')}, {len(notes + extra)} review notes)")
        report.append(f"\nDrafted this run: {made}. Paid AI spend today: ${store.spend_today():.2f} (free models cost $0).")
    except BudgetExceeded as exc:
        report.append(f"Stopped: {exc}.")
    except NoModelAvailable as exc:
        report.append(f"Stopped: free AI limits reached for now ({exc}). The next run will continue.")
    finally:
        store.prune()
        store.close()
        summary(report)


if __name__ == "__main__":
    main()
