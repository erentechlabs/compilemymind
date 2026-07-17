# Source-first content pipeline

This is the complete replacement for the former autopublisher, maintenance scripts, release gate, model updater, and workflow set.

## Why it is different

The previous system asked a model to propose a topic and to select a matching three-source bundle from a broad research pool. A topic could be sound but still be rejected because one of the model-selected URLs was only adjacent to it. The run in question hit exactly that condition.

The new pipeline is source-first. `automation/content-pipeline.json` is an ordered editorial backlog. Every brief already contains three distinct official sources. The publisher chooses the first unpublished brief, verifies those exact pages are reachable, asks Gemini to write only from their excerpts, runs deterministic article checks, then runs Hugo. There is no dynamic topic selection and no separate release-gate process.

If a source is unavailable or a draft fails the checks, the event is recorded in `.content-pipeline/state.json`, no post is written, and the pipeline tries the next brief. A provider outage is recorded as retryable and is attempted again on the next schedule. Quality gates never relax to force a post through.

## Setup

Configure these repository Actions secrets:

- `GEMINI_API_KEY`
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_PAGES_PROJECT`

`GEMINI_MODEL` is an optional repository variable. It overrides the model in `automation/content-pipeline.json`.

Give GitHub Actions read/write workflow permissions so the publishing workflow can commit an approved post. The Cloudflare token needs permission to deploy the configured Pages project.

The workflows deploy directly with Wrangler. If the Pages project is Git-integrated, disable automatic production and preview branch builds in Cloudflare Pages to avoid duplicate deployments.

## Workflows

| Workflow | Trigger | Responsibility |
| --- | --- | --- |
| `Validate site` | Pull request, push to `main`, manual | Validates the backlog, runs the new pipeline tests, and builds Hugo. No secrets are exposed. |
| `Publish source-backed article` | Daily at 06:17 UTC, manual | Validates exact sources, writes at most one quality-approved article, commits it, and deploys it directly to Cloudflare Pages. |
| `Deploy production site` | Push to `main`, manual | Builds and deploys the current production branch directly to Cloudflare Pages. |

## Local commands

```powershell
python tools/content_pipeline/publish.py --mode validate
python -m unittest discover -s tools/content_pipeline -p "test_*.py"
hugo --gc --minify --cleanDestinationDir --destination public
```

To run a real local publish, set `GEMINI_API_KEY` in the shell, install the pinned Hugo version, then run:

```powershell
python tools/content_pipeline/publish.py --mode publish
```

## Adding a future topic

Add a new object to `briefs` in `automation/content-pipeline.json`. Give it a unique slug, clear reader goal, categories/tags, and at least three live official documentation URLs from `trusted_domains`. The next scheduled run will select it after every earlier unpublished brief.
