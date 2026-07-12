# Autonomous Publishing Platform

This repository now includes an autonomous publishing layer for the Hugo site. It uses GitHub Actions for scheduling and Gemini for topic research, article generation, image generation, and quality review.

## Required Secret

Add this repository secret in GitHub:

- `GEMINI_API_KEY`: API key from Google AI Studio or your Gemini API project.

Optional model overrides can be added as workflow environment variables:

- `GEMINI_TEXT_MODEL`
- `GEMINI_QA_MODEL`
- `GEMINI_GROUNDED_RESEARCH_MODEL`
- `GEMINI_IMAGE_MODEL`

The default models are configured in `.autopublisher/config.json`.

## Workflows

- `Autonomous Publishing`: runs three times per day. It researches trusted sources, chooses one non-duplicate topic, generates a long-form article, creates article assets, runs deterministic and Gemini QA, validates Hugo, then commits approved content to `main`.
- `Content Maintenance`: runs twice per week. It reviews older posts, checks external links, uses Gemini grounded research to detect outdated facts, updates only when meaningful, validates Hugo, then commits approved maintenance changes.
- `Infrastructure Maintenance`: runs every January and July. It checks Hugo and theme tooling updates, validates candidate updates, then opens a PR for manual review. It does not push infrastructure changes directly to `main`.

## Quality Gates

The publisher rejects or regenerates content when it is too short, too similar to existing posts, missing required sources, missing useful tables, missing requested diagrams/charts, or rejected by Gemini QA.

Similarity checks compare the candidate article and title against existing posts with token-based cosine and Jaccard scoring. Thresholds live in `.autopublisher/config.json`.

## Content Assets

New posts are written as Hugo page bundles:

```text
content/posts/example-slug/
  index.md
  featured.png
  optional-diagram.svg
  optional-chart.svg
```

Gemini image generation creates `featured.png` when available. If image generation fails, the system writes a branded fallback `featured.svg` so the post still has a featured image.

## Logs And State

- Runtime logs are written to `.autopublisher/logs/` and uploaded as GitHub Actions artifacts.
- Durable state is stored in `.autopublisher/state.json`.
- Infrastructure reports are stored in `.autopublisher/reports/` and committed only with maintenance PRs.

## Local Commands

Run an inventory audit:

```bash
python tools/autopublisher/autopublisher.py --mode audit
```

Run a dry-run maintenance pass:

```bash
python tools/autopublisher/autopublisher.py --mode maintain --dry-run --max-articles 1
```

Run a real publishing pass locally:

```bash
GEMINI_API_KEY=your-key python tools/autopublisher/autopublisher.py --mode publish
```

Validate the site:

```bash
hugo --minify
```
