# press.autos content engine

Runs **free** on GitHub Actions every 2 hours, using free AI models (Groq, with optional Gemini and OpenRouter fallbacks). Each run:

1. **Discovers** fresh stories from manufacturer press rooms, EV news feeds and the GDELT global news index.
2. **Groups** headlines about the same story and **scores** them (freshness, how many outlets cover it, relevance, source trust).
3. **Triage** (a fast free model) drops non-automotive or weak stories and assigns a category and country.
4. For the best 1–2 stories it **reads the source pages**, builds a **fact table** with a source for every fact, **writes** an original article, **fact-checks** it against the sources with a *different* AI model, fixes problems, and checks the wording is not copied.
5. **Posts it to WordPress as "Pending review"** with category, brand/powertrain/body tags, SEO description, sources list, AI note and review notes. It never publishes.

**Editions:** each story's country decides where it goes. Global stories go to press.autos; India-only stories go to in.press.autos. Ready to switch on in `config.yaml` (create the site first, then set `enabled: true`): UK, US, Australia, Canada, Malaysia, Singapore, Philippines, UAE, South Africa, New Zealand, Thailand, Indonesia, Italy, Germany, France. All editions are written in English; for Malaysia, Thailand, Indonesia, Italy, Germany and France the engine also reads local-language news (`source_language: any`). Local stories for countries without an enabled edition go to the main site with the country in the title (or are skipped with `local_without_edition: skip`). To add a country not listed, copy any edition block and change its code, name, url, style and `sourcecountry:` name.

Limits (in `config.yaml`): 10 drafts/day for the main site, 6/day for India, 3/day for each new edition, 20/day in total across all editions (what one person can review), 3 per run, max half of an edition's drafts in one category. Free models cost nothing; if you ever add a paid Claude key as a fallback, it is capped at $3.50/day.

## One-time setup (about 30 minutes)

### 1. WordPress: the Pipeline user
1. Update the **press.autos Core** plugin to v0.4.0 or later.
2. **Users → Add New**: username `pipeline`, any email you control, role **Pipeline (automation)**.
3. Log in as admin, open **Users → pipeline → Edit**, scroll to **Application Passwords**, name it `github-engine`, click **Add**. Copy the password shown (spaces are fine). You only see it once.

### 2. Free AI keys (no credit card)
1. **Groq (required):** sign up at https://console.groq.com → **API Keys → Create API Key**, name it `press-autos`, copy it. Check **Settings → Limits** for your free limits; if they differ from `config.yaml`, update the numbers there.
2. **Gemini (recommended extra capacity):** sign in at https://aistudio.google.com with a Google account → **Get API key → Create API key**. Check which free models your project shows; if `gemini-3.5-flash-lite` / `gemini-3.8-flash` are named differently, update them in `config.yaml`. Note: on Google's free tier your prompts (public news text) may be used to improve Google's products.
3. **OpenRouter (optional last fallback):** sign up at https://openrouter.ai → **Keys → Create**. Free models allow about 50 requests a day.

Free limits are set by each provider and can change at any time; the engine simply moves to the next model when one runs out, and stops politely when all are used up until the next run.

### 3. GitHub repository
1. Create a free account at https://github.com if you don't have one.
2. Top-right **+ → New repository**: name `press-autos-engine`, choose **Public** (unlimited free Actions minutes; your keys stay secret in Secrets, only the code and a list of headlines are visible), create.
3. On the new repo page click **uploading an existing file**, drag in **all files and folders from this zip** (including the hidden `.github` folder — if your computer hides it, see the note below), and **Commit changes**.
4. **Settings → Secrets and variables → Actions → New repository secret**, add these secrets (the last two are optional):

| Name | Value |
| --- | --- |
| `WP_URL` | `https://press.autos` |
| `WP_USER` | `pipeline` |
| `WP_APP_PASSWORD` | the application password from step 1 |
| `GROQ_API_KEY` | your Groq key |
| `GEMINI_API_KEY` | your Gemini key (optional) |
| `OPENROUTER_API_KEY` | your OpenRouter key (optional) |

5. **Settings → Actions → General → Workflow permissions**: choose **Read and write permissions**, Save. (The engine saves its memory file back to the repo.)

### 4. First test
1. **Actions** tab → **press.autos engine** → **Run workflow**, tick **Dry run**, Run.
2. Open the run when it finishes: the summary lists the stories it found and which it would write. Nothing is posted.
3. Run it again **without** Dry run. Within a few minutes a draft appears in WordPress under **Posts → Pending** and on your dashboard's review queue.

After that it runs automatically every 2 hours.

## Adding a country edition (e.g. in.press.autos)
1. **Network Admin → Sites → Add New**: Site Address `in`, Title `press.autos India`.
2. On the new site: activate Blocksy, set Permalinks to `/%category%/%postname%/`, set **Settings → General → press.autos edition country** to `IN`.
3. **Tools → Import → WordPress**: import the same starter-content XML used for the main site (categories, tags, trust pages), then edit the About page for the edition.
4. **Users → Add Existing User**: add `pipeline` with the role **Pipeline (automation)**. The same application password works on every site.
5. In `config.yaml`, set the edition's `enabled: true` (India is already on).

## Daily routine (about 20–30 minutes)
- Check the review queue on **each edition's dashboard** (main site and in.press.autos).
- Dashboard → **press.autos review queue**. Items marked **⚠ check notes** have something to verify.
- Open each draft: read the **Review notes** box (bottom of the editor), check facts against the linked sources, add a featured image, then **Publish** or **Move to Trash**.
- Drafts you trash are never re-written (the engine remembers stories it has done).

## Changing behaviour
Edit `config.yaml` on GitHub (pencil icon) — daily numbers, categories, keywords, trusted sources, feeds, GDELT searches. Changes apply on the next run.

## Troubleshooting
- **401 from WordPress**: wrong user or application password, or the host strips login headers. Add this line at the top of `.htaccess` in `public_html`:
  `SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1`
- **403 from WordPress**: a security plugin or firewall blocks the REST API; allow `/wp-json/` for the pipeline user.
- **"free AI limits reached for now"**: every free model used its allowance; the next run continues. Add a Gemini or OpenRouter key for more capacity, or lower the daily numbers.
- **A model keeps failing (404 / model not found)**: the provider renamed or retired it. Open that provider's model list and put a current free model name in `config.yaml` under `llm: models:`.
- **No drafts**: open the latest run's summary; it shows how many stories were found and why they were skipped.
- **Hidden `.github` folder**: on Mac press Cmd+Shift+. in Finder; on Windows enable "Hidden items" in File Explorer's View menu. Or create the file on GitHub: **Add file → Create new file**, name `.github/workflows/engine.yml`, paste its contents.

## GitHub minutes
Free AI models have per-minute limits, so the engine waits between requests and a run can take 5–15 minutes. A **public** repository has unlimited free Actions minutes, which is why setup recommends it. A private repository gets 2,000 minutes a month, which is not enough at this pace unless you run every 4 hours (`cron: "17 */4 * * *"` in `.github/workflows/engine.yml`).

## Cost
**$0.** GitHub Actions (public repo), GDELT, RSS feeds, Groq/Gemini/OpenRouter free tiers and WordPress REST are all free. Expect roughly 10–16 drafts a day from Groq's free allowance alone (about 20,000 tokens per article); Gemini adds more. Free models are a little weaker than paid ones, so the fact-check notes matter: read them before publishing.

Optional: adding `ANTHROPIC_API_KEY` makes paid Claude the last fallback (about $0.07–0.10 per article, capped by `daily_max_usd`).
