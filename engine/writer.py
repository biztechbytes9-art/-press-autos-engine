"""The editorial steps: triage, fact table, draft, verification and revision."""
import json

HOUSE_RULES = """You write for press.autos, a global automotive magazine (cars, motorcycles, EVs).
Rules you must never break:
- Use ONLY facts present in the provided SOURCE TEXTS. Never invent numbers, dates, prices, quotes, test drives or experiences.
- Never copy sentences from the sources. Write fresh wording; short factual phrases like model names are fine.
- Attribute facts: "according to Toyota", "NHTSA said", "data from SMMT shows".
- Rumours and leaks must be labelled as unconfirmed.
- Sources may be in any language. Always write in English. Translate quoted statements faithfully and say they were translated (e.g. "said in Italian").
- Plain, clear English. Short paragraphs (max 3 sentences). Sentences under 25 words. No hype, no clickbait, no emoji.
- Give metric units first; add miles or US units in brackets where the story is US/UK.
- The first sentence states the news and its key number.
- On the global edition: if the story is only about one country, say so in the first sentence and add the country in brackets at the end of the title, e.g. "(US)", "(India)", "(UK)". On a country edition, never add the country to the title.
"""


def triage_prompt(clusters: list[dict], categories: dict) -> tuple[str, str]:
    cats = "\n".join(f"- {k}: {v}" for k, v in categories.items())
    lines = []
    for i, c in enumerate(clusters):
        heads = " | ".join(x["title"] for x in c["items"][:4])
        local = f" (local source for our {c['edition_hint']} edition)" if c.get("edition_hint") else ""
        lines.append(f"{i}. [{', '.join(c['domains'][:5])}]{local} {heads}")
    system = "You are the news editor of press.autos, a global automotive magazine. You choose which stories are worth covering."
    user = f"""Our categories:
{cats}

Candidate stories (index. [source domains] headlines):
{chr(10).join(lines)}

For EACH candidate return an object:
{{"i": index, "keep": true/false, "category": one category slug or null, "newsworthy": 1-10,
  "audience": "global" if relevant to readers in many countries, else the ONE ISO country code it mainly concerns ("IN","US","GB","DE","CN"...),
  "reason": "max 12 words"}}
Keep only real automotive news useful to car/bike buyers or enthusiasts: launches, prices, recalls, sales data,
specs, regulations, charging, major industry moves. Reject: stock-market chatter, opinion pieces, listicles, deals/discounts,
celebrity, crime, accidents involving individuals, sponsored content, anything not automotive.
We run country editions: local news (a launch, price, recall or sales figure for one country) is valuable even if
only one outlet reports it, so keep good local stories and set "audience" to that country. Spy shots and rumours
score low unless the source has strong details.
Return a JSON array."""
    return system, user


def facts_prompt(cluster: dict, sources: list[dict], cfg: dict) -> tuple[str, str]:
    src = "\n\n".join(f"SOURCE {i + 1} ({s['url']}):\n{s['text'][:9000]}" for i, s in enumerate(sources))
    system = HOUSE_RULES
    user = f"""Story: {cluster['title']}

{src}

Build a fact table for this story. Return JSON:
{{"headline_fact": "the core news in one sentence",
  "facts": [{{"fact": "...", "value": "number/date/name if any", "source": "URL it came from"}}],
  "primary_source": "URL of the most authoritative source (manufacturer, regulator, data body) if one is linked or quoted, else the best source URL",
  "is_confirmed": true/false,
  "country": "global" if the news matters to readers in many countries (global launches, global sales data, major tech), else the ONE ISO country code it concerns (e.g. IN for an India-only launch, price or recall),
  "category": one of {list(cfg['categories'].keys())},
  "brands": subset of {cfg['brands']} (exact spelling, empty if none),
  "powertrain": subset of {cfg['powertrains']},
  "body": subset of {cfg['bodies']},
  "enough_for_article": true/false,
  "missing": "what a reader would want that the sources do not say"}}
Include only facts stated in the sources. 6-20 facts."""
    return system, user


def write_prompt(facts: dict, sources: list[dict], cfg: dict, edition: dict | None = None) -> tuple[str, str]:
    src_list = "\n".join(f"- {s.get('title') or s['url']} ({s['url']})" for s in sources)
    system = HOUSE_RULES
    if edition:
        system += f"\nThis article is for the {edition.get('name', 'press.autos')} edition. {edition.get('style', '')}\n"
    user = f"""FACT TABLE:
{json.dumps(facts, ensure_ascii=False, indent=1)}

SOURCES:
{src_list}

Write the news article from the fact table only. 300-600 words.
Structure: opening paragraph with the news; then sections with "## " headings (e.g. key details, prices/specs,
what it means for buyers/owners). Use a "| table |" only when there are 3+ comparable numbers. Bullet lists with "- ".
Do not add a Sources section (the site adds it automatically). Do not add a conclusion that repeats the article.

Return JSON:
{{"title": "accurate headline, max 75 characters, no clickbait",
  "excerpt": "meta description, 140-160 characters",
  "focus_keyword": "2-5 word search phrase",
  "body_markdown": "the article",
  "image_hint": "best URL for an official press image or press release (from the sources), or empty",
  "card_stats": ["up to 3 very short key figures from the fact table for the image card, max 22 characters each, e.g. \"Rs 12.5 lakh\", \"465 km range\", \"19,000 cars\"; empty list if none"]}}"""
    return system, user


def verify_prompt(draft: dict, facts: dict, sources: list[dict]) -> tuple[str, str]:
    src = "\n\n".join(f"SOURCE {i + 1} ({s['url']}):\n{s['text'][:9000]}" for i, s in enumerate(sources))
    system = "You are a strict fact-checker for an automotive news site. You compare a draft against its sources."
    user = f"""DRAFT TITLE: {draft['title']}
DRAFT:
{draft['body_markdown']}

{src}

Check every number, date, name, price, spec and claim in the draft against the sources.
Also check: the title matches the article; nothing is presented as confirmed if the source says it is a rumour;
no sentence is copied from a source.
Report only real errors, at most 5. Write each issue as ONE short sentence (under 40 words) that names the
wrong statement and gives the correct value from the source. Do not explain your reasoning, do not list
things that are correct, and do not report style preferences.
Return JSON: {{"ok": true/false, "issues": ["specific problem and the correct value from the source", ...]}}
Only list real problems. Rounding (e.g. 74.7% -> nearly 75%) is fine."""
    return system, user


def revise_prompt(draft: dict, issues: list[str], facts: dict) -> tuple[str, str]:
    system = HOUSE_RULES
    user = f"""Fix these problems in the draft. Change only what is needed.

PROBLEMS:
{chr(10).join('- ' + i for i in issues)}

FACT TABLE:
{json.dumps(facts, ensure_ascii=False, indent=1)}

DRAFT JSON:
{json.dumps(draft, ensure_ascii=False)}

Return the corrected draft as JSON with the same keys."""
    return system, user
