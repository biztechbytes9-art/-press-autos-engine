"""One engine run: discover -> cluster -> score -> triage -> fact table -> route to edition -> draft -> verify -> post.

Usage:
  python -m engine.run              # normal run (needs secrets)
  python -m engine.run --dry-run    # discover, score and triage only; posts nothing
"""
import argparse
import logging
import os
from collections import Counter

from . import blocks, checks, cluster, extract, images, sources, writer
from .config import ROOT, load_config, load_secrets
from .llm import BudgetExceeded, NoModelAvailable, Router
from .store import Store
from .wordpress import SiteNotReady, WordPress, build_payload, upload_media

log = logging.getLogger("engine")


def summary(lines: list[str]) -> None:
    """Write a readable report to the GitHub Actions run page (and the log)."""
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    # Also keep the latest reports in the repository (state/runs.md, newest first, last 30 runs),
    # so they can be read without opening the Actions page.
    try:
        from datetime import datetime, timezone
        log_path = ROOT / "state" / "runs.md"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"<!-- run -->\n# Run {stamp}\n\n" + text.replace("## press.autos engine run", "").strip() + "\n\n"
        old = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        runs = [r for r in old.split("<!-- run -->\n") if r.strip()][:29]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(entry + "".join("<!-- run -->\n" + r for r in runs), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save run report: %s", exc)


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
def discover(cfg: dict, store: Store, health: dict | None = None) -> list[dict]:
    ua = cfg["site"]["user_agent"]
    feeds = list(cfg.get("feeds", []))
    gd = dict(cfg.get("gdelt", {}))
    queries = list(gd.get("queries", []))
    ed_queries = {}
    for key, ed in enabled_editions(cfg).items():
        if key == "global":
            continue
        feeds += [dict(f, edition=key) for f in (ed.get("feeds") or [])]
        for q in ed.get("gdelt_queries") or []:
            queries.append(q)
            ed_queries[q] = key
    gd["queries"] = queries
    items = (sources.fetch_feeds(feeds, ua, health=health)
             + sources.fetch_gdelt(gd, ua, health=health, edition_queries=ed_queries))
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
    eds = enabled_editions(cfg)
    fresh, local = [], {}
    for c in clusters:
        if c["key"] in done or cluster.is_repeat(c, recent_titles):
            continue
        c["score"] = cluster.score_cluster(c, cfg)
        ed = next((it.get("edition") for it in c["items"] if it.get("edition") in eds), None)
        c["edition_hint"] = ed
        if c["score"] >= cfg["limits"]["min_score"]:
            fresh.append(c)
        elif ed and c["score"] >= float(eds[ed].get("min_score", cfg["limits"]["min_score"])):
            # Local news is usually reported by one outlet, so it can't earn "coverage" points:
            # country editions use their own (lower) bar.
            local.setdefault(ed, []).append(c)
    fresh.sort(key=lambda c: c["score"], reverse=True)
    # Reserve places for each country edition so global stories can't crowd them out.
    for ed, extra in local.items():
        extra.sort(key=lambda c: c["score"], reverse=True)
        have = sum(1 for c in fresh if c.get("edition_hint") == ed)
        want = int(eds[ed].get("candidate_slots", 8))
        fresh += extra[:max(0, want - have)]
    fresh.sort(key=lambda c: c["score"] + (0.15 if c.get("edition_hint") else 0), reverse=True)
    # One busy site must not fill the queue: keep at most N candidates led by the same website.
    cap = int(cfg["limits"].get("max_candidates_per_source", 4))
    per, out = Counter(), []
    for c in fresh:
        d = lead_domain(c)
        if per[d] >= cap:
            continue
        per[d] += 1
        out.append(c)
    return out[:30]


def interleave_editions(cfg: dict, picked: list[dict]) -> list[dict]:
    """Best story of each edition first, then the second best of each, so one edition can't take every slot in a run."""
    groups: dict[str, list[dict]] = {}
    for c in picked:
        groups.setdefault(str(route(cfg, (c.get("triage") or {}).get("audience"))), []).append(c)
    out = []
    while any(groups.values()):
        for k in list(groups):
            if groups[k]:
                out.append(groups[k].pop(0))
    return out


def lead_domain(c: dict) -> str:
    doms = Counter(it.get("domain", "") for it in c.get("items", []))
    return doms.most_common(1)[0][0] if doms else ""


def short_note(text: str, limit: int = 260) -> str:
    """Fact-check models sometimes think out loud; keep the first sentence or two."""
    t = " ".join(str(text).split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "))
    return (cut[:end + 1] if end > 80 else cut.rstrip() + "…")


def official_domain(cfg: dict, url: str) -> bool:
    d = sources.domain_of(url or "")
    for dom, v in (cfg.get("source_authority") or {}).items():
        if (d == dom or d.endswith("." + dom)) and float(v) >= 1.0:
            return True
    return False


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
    issues += checks.headline_mismatch(draft.get("title", ""), draft.get("excerpt", ""), draft["body_markdown"])
    if overlap > 0.06:
        issues.append(f"About {overlap:.0%} of the wording matches a source; rewrite those passages in fresh words.")
    if issues:
        s, u = writer.revise_prompt(draft, issues, facts)
        draft = ai.ask_json("writer", s, u, max_tokens=3000)
        s, u = writer.verify_prompt(draft, facts, srcs)
        ver2 = ai.ask_json("verify", s, u, max_tokens=1500)
        notes += ["Fact-check: " + short_note(i) for i in (ver2.get("issues") or [])[:5]]
        overlap = max(checks.copy_overlap(draft["body_markdown"], x["text"]) for x in srcs)
        if overlap > 0.06:
            notes.append(f"Wording overlap with a source is {overlap:.0%}; rephrase before publishing.")
    notes += ["Headline: " + p for p in checks.headline_mismatch(draft.get("title", ""), draft.get("excerpt", ""),
                                                                   draft["body_markdown"])]
    odd = checks.unsupported_numbers(draft["body_markdown"], src_text + "\n" + str(facts))
    if odd:
        notes.append("Numbers not found in sources (may be calculations): " + ", ".join(odd[:10]))
    if not facts.get("is_confirmed", True):
        notes.append("Story is not officially confirmed; check the wording says so.")
    return draft, notes


def attach_images(cfg: dict, wp: WordPress, srcs: list[dict], facts: dict, draft: dict,
                  edition: dict, ed_key: str, html: str) -> tuple[str, int | None, list[str]]:
    """Official press photo (credited) or a generated news card. Returns (html, featured media id, notes)."""
    if not (cfg.get("images") or {}).get("enabled", True):
        return html, None, ["Add a featured image: the maker's official press/media site or your own photo."]
    try:
        pick = images.choose_images(cfg, srcs, facts, draft, edition, ed_key)
        feat = pick.get("featured")
        if not feat:
            return html, None, ["Add a featured image: the maker's official press/media site or your own photo."]
        data, mime, name, alt, caption, kind = feat
        media = upload_media(wp, data, mime, name, alt, caption)
        notes = []
        if kind == "official":
            notes.append(f"Featured image: official press photo ({caption}). Check it shows the right model.")
        else:
            notes.append("Featured image: generated news card. For a big story, replace it with an official press "
                         "photo or your own (never a news site's photo).")
        inl = pick.get("inline")
        if inl:
            m2 = upload_media(wp, inl[0], inl[1], inl[2], inl[3], inl[4])
            html = images.insert_after_paragraph(html, images.image_block(m2, inl[4]), 3)
        return html, media.get("id"), notes
    except Exception as exc:  # noqa: BLE001  (an image problem must never lose the article)
        log.warning("image step failed: %s", exc)
        return html, None, ["Add a featured image: the maker's official press/media site or your own photo."]


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
        health: dict = {}
        items = discover(cfg, store, health)
        cands = choose(cfg, store, items)
        report.append(f"Discovered {len(items)} recent items, {len(cands)} candidate stories above score {lim['min_score']}.")
        if health:
            bad = [k for k, v in health.items() if isinstance(v, str) and v.startswith("failed")]
            empty = [k for k, v in health.items() if v == 0]
            good = [f"{k} {v}" for k, v in health.items() if isinstance(v, int) and v > 0]
            report.append("Sources working: " + (", ".join(good) or "none") + ".")
            if "GDELT news search" in health:
                report.append("GDELT: " + str(health["GDELT news search"]) + ".")
            if bad or empty:
                report.append("Sources not working: " + ", ".join([f"{k} ({health[k]})" for k in bad] + [f"{k} (0 items)" for k in empty]) + ".")
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
        picked = interleave_editions(cfg, triage(ai, cfg, cands))
        report += ["", "### Chosen by triage", "",
                   "| Score | Category | Country | Would go to | Story |", "| --- | --- | --- | --- | --- |"]
        by_site, by_cat = Counter(), Counter()
        for c in picked[:15]:
            t = c["triage"]
            ed = route(cfg, t.get("audience"))
            site = "skipped (no edition)" if ed is None else (
                "press.autos (main)" if ed == "global" else f"{eds[ed].get('name', ed)}")
            by_site[site] += 1
            by_cat[t.get("category") or "?"] += 1
            title = c["title"].replace("|", "/")
            report.append(f"| {c['rank']:.2f} | {t.get('category') or '?'} | {t.get('audience') or 'global'} | {site} | {title} |")
        if picked:
            report += ["", "By site: " + ", ".join(f"{k} {v}" for k, v in by_site.most_common()),
                       "By category: " + ", ".join(f"{k} {v}" for k, v in by_cat.most_common())]
        if args.dry_run:
            report[0] = "## press.autos engine run (DRY RUN)"
            report += ["", "Dry run: nothing was written or posted. The country is checked again from the "
                       "source pages before a real draft is routed."]
            return
        for c in cands:
            if c not in picked:
                store.reject(c["key"], "triage")

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
        not_ready: set[str] = set()
        used_sources: set[str] = set()
        for c in picked:
            if made >= lim["per_run_max_articles"] or made >= total_left:
                break
            guess = route(cfg, c["triage"].get("audience"))
            if guess is None:
                store.reject(c["key"], "local story without an edition")
                continue
            if room.get(guess, 0) <= 0 or guess in not_ready:
                continue  # that edition is full today (or not set up); the story may be picked up later if fresh
            if lead_domain(c) in used_sources:
                continue  # one draft per website per run, so one busy site can't fill the queue
            if guess != "global":
                try:
                    wp_for(guess)
                except SiteNotReady as exc:
                    not_ready.add(guess)
                    report.append(f"- waiting: {eds[guess].get('name', guess)} — {exc} Stories for it are kept, not dropped.")
                    continue
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
                if room.get(ed_key, 0) <= 0 or ed_key in not_ready:
                    continue
                cat = facts.get("category") or c["triage"].get("category") or ""
                max_per_cat = max(1, int(int(eds[ed_key].get("daily_max_articles", 6)) * lim["max_category_share"]))
                if cat and cat_count[ed_key][cat] >= max_per_cat:
                    continue
                try:
                    wp = wp_for(ed_key)
                except SiteNotReady as exc:
                    not_ready.add(ed_key)
                    report.append(f"- waiting: {eds[ed_key].get('name', ed_key)} — {exc} Stories for it are kept, not dropped.")
                    continue
                draft, notes = write_story(ai, cfg, facts, srcs, eds[ed_key])
                html = blocks.key_facts(draft.get("key_facts") or []) + blocks.to_blocks(draft["body_markdown"])
                html, media_id, img_notes = attach_images(cfg, wp, srcs, facts, draft, eds[ed_key], ed_key, html)
                notes += img_notes
                payload, extra = build_payload(wp, draft, facts, html, srcs, c["key"], notes, cfg["site"]["post_status"])
                if media_id:
                    payload["featured_media"] = media_id
                payload["meta"]["pa_edition"] = "global" if ed_key == "global" else ed_key
                post = wp.create_post(payload)
            except (BudgetExceeded, NoModelAvailable):
                raise
            except Exception as exc:
                log.exception("draft failed")
                msg = str(exc)
                store.reject(c["key"], f"error: {msg}"[:200])
                report.append(f"- failed: {c['title']}: {msg[:200]}")
                if msg.startswith(("WordPress 401", "WordPress 403")):
                    # The site refuses this user: stop sending it stories this run (they are retried next run).
                    not_ready.add(ed_key)
                    report.append(f"- waiting: {eds[ed_key].get('name', ed_key)} refused the post; check the pipeline "
                                  "user's role on that site. Its stories will be retried.")
                continue
            store.add_draft(c["key"], draft["title"], cat, post.get("id"), "posted", notes + extra, ed_key)
            store.commit()
            cat_count[ed_key][cat] += 1
            room[ed_key] -= 1
            made += 1
            used_sources.add(lead_domain(c))
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
