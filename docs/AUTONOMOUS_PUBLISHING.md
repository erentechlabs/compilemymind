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

The publishing workflow also grants `models: read` and uses the automatic Actions token for GitHub Models. The lightweight `openai/gpt-4o-mini` model handles topic ranking and metadata enrichment; Gemini remains responsible for research, article writing, and final QA. No additional GitHub Models API key is required.

Direct Cloudflare Pages deployment is optional because the repository can continue using Cloudflare's Git integration. To deploy from the workflow itself, add:

- Secret `CLOUDFLARE_API_TOKEN`
- Secret `CLOUDFLARE_ACCOUNT_ID`
- Repository variable `CLOUDFLARE_PAGES_PROJECT`

The Cloudflare token should be limited to the Pages account/project it deploys.

## Scheduled workflows

- `Autonomous publishing` runs four times daily. It reads the configured trusted RSS/Atom sources, enriches them with Gemini Google Search grounding, balances categories, scores topic potential with the lightweight model, checks local similarity, generates a long-form page bundle, enriches metadata, creates diagrams/charts where useful, runs deterministic and Gemini QA, builds Hugo, and pushes only approved content. If a draft fails, it receives the structured rejection feedback and is repaired up to the configured regeneration limit; if that topic still fails, the publisher selects one different non-duplicate topic before giving up.
- The research inventory deliberately spans software and language releases, Python/Java/.NET/TypeScript/Node.js, Swift and Apple platforms, iOS, Android, Kotlin, React Native, Flutter, current AI models and tools, cloud services, databases, DevOps, containers, observability, operating systems, hardware, cybersecurity, and certification topics.
- `Autonomous content maintenance` runs every Monday and Thursday. It reviews the oldest due articles, checks external links, researches changed facts, and updates an article only when the evidence supports a meaningful correction.
- `Revise existing posts` runs daily and uses Gemini for the prose revision, then the lightweight model for metadata cleanup and the shared QA gate before committing an accepted revision.
- `Gemini model maintenance` runs monthly and discovers/smoke-tests newer compatible stable Gemini models.
- `Infrastructure maintenance` runs monthly. It performs a baseline build, detects Hugo/npm updates, applies only low-risk updates when regression tests pass, and prepares a draft PR for major/minor or otherwise risky candidates.
- `Deploy Hugo site` builds every `main` push, optionally synchronizes the Hugo version through the Cloudflare API, and relies on the existing Cloudflare Pages Git integration to deploy the pushed commit.

The publishing and maintenance workflows deploy in the same run after pushing because commits made with `GITHUB_TOKEN` do not recursively start another Actions workflow.

## Quality and safety gates

An article is rejected and regenerated when it is too short, shallow, repetitive, missing enough trusted sources, missing a useful table, missing a requested visual, contains malformed Markdown, contains a diagram or chart with colliding text, or is rejected by Gemini QA. The repair prompt includes the rejected draft when a structural or completeness problem is found, so retries can correct the article instead of starting blindly from scratch. Generated visual SVGs are wrapped and checked for text collisions before the bundle is accepted. Gemini QA is fail-closed: an API outage never turns an unreviewed draft into a publication.

Similarity detection uses local token-based cosine and Jaccard scores against every existing Hugo post before generation and again during final QA. Source URLs are accepted only when they came from the configured research inventory, and feed, Atom API, and comment endpoints are rejected in favor of the article's HTML URL. The final article must retain the configured minimum number of sources. Basic Markdown structure is also checked without an AI call.

Articles are written as Hugo page bundles:

```text
content/posts/example-slug/
  index.md
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
