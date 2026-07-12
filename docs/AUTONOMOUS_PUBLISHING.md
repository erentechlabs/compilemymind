# Autonomous Publishing Platform

The repository now contains a continuously scheduled Hugo publisher powered by Gemini and GitHub Actions. After the one-time GitHub setup below, it researches, writes, reviews, commits, and deploys content without routine manual operation.

The only intentional human checkpoint is infrastructure work that could be breaking: those candidates become a draft pull request instead of being applied to the live branch.

## One-time GitHub setup

Add this repository secret:

- `GEMINI_API_KEY`: a Google AI Studio/Gemini API key with access to the configured models.

Optional repository variables override the defaults in `.autopublisher/config.json`:

- `GEMINI_TEXT_MODEL`
- `GEMINI_QA_MODEL`
- `GEMINI_GROUNDED_RESEARCH_MODEL`
- `GEMINI_IMAGE_MODEL`

The workflow needs repository Actions permission set to **Read and write permissions** so its `GITHUB_TOKEN` can commit approved content. Do not use a personal access token for normal publishing.

Direct Cloudflare Pages deployment is optional because the repository can continue using Cloudflare's Git integration. To deploy from the workflow itself, add:

- Secret `CLOUDFLARE_API_TOKEN`
- Secret `CLOUDFLARE_ACCOUNT_ID`
- Repository variable `CLOUDFLARE_PAGES_PROJECT`

The Cloudflare token should be limited to the Pages account/project it deploys.

## Scheduled workflows

- `Autonomous publishing` runs four times daily. It reads the configured trusted RSS/Atom sources, enriches them with Gemini Google Search grounding, balances categories, scores topic potential, checks similarity, generates a long-form page bundle, creates diagrams/charts where useful, generates a featured image, runs deterministic and Gemini QA, builds Hugo, and pushes only approved content.
- `Autonomous content maintenance` runs every Monday and Thursday. It reviews the oldest due articles, checks external links, researches changed facts, and updates an article only when the evidence supports a meaningful correction.
- `Infrastructure maintenance` runs every January and July. It performs a baseline build, detects Hugo/npm updates, applies only low-risk patch updates when regression tests pass, and prepares a draft PR for major/minor or otherwise risky candidates.
- `Deploy Hugo site` builds every `main` push and performs an explicit Cloudflare Pages deploy when the optional Cloudflare settings are present.

The publishing and maintenance workflows deploy in the same run after pushing because commits made with `GITHUB_TOKEN` do not recursively start another Actions workflow.

## Quality and safety gates

An article is rejected and regenerated when it is too short, shallow, repetitive, missing enough trusted sources, missing a useful table, missing a requested visual, missing a featured-image prompt, or rejected by Gemini QA. Gemini QA is fail-closed: an API outage never turns an unreviewed draft into a publication.

Similarity detection uses token-based cosine and Jaccard scores against every existing Hugo post. Source URLs are accepted only when they came from the configured research inventory, and the final article must retain the configured minimum number of sources.

Articles are written as Hugo page bundles:

```text
content/posts/example-slug/
  index.md
  featured.png        # Gemini image, or a topic-labelled SVG fallback
  optional-diagram.svg
  optional-chart.svg
```

## Logs, state, and retries

- Network calls use bounded exponential retries for transient failures.
- Durable scheduling and review state is stored in `.autopublisher/state.json`.
- Runtime JSONL logs are stored in `.autopublisher/logs/` and uploaded as 30-day workflow artifacts.
- Infrastructure reports are uploaded for 180 days; risky candidates are also preserved in a draft PR for review.

## Local validation

```bash
python -m unittest discover -s tools/autopublisher -p "test_*.py"
python tools/autopublisher/autopublisher.py --mode audit
python tools/autopublisher/autopublisher.py --mode maintain --dry-run --max-articles 1
hugo --minify
```

For a real local publish, set `GEMINI_API_KEY` in the shell and run:

```bash
python tools/autopublisher/autopublisher.py --mode publish
```
