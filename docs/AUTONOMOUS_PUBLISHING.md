# Autonomous publishing platform

Compile My Mind uses a fail-closed Hugo publisher driven by GitHub Actions. A candidate is published automatically only after its topic, sources, material claims, code, originality, metadata, links, structured data, and rendered output pass the configured gates. There is no editor queue, manual approval, or pull-request approval in the content publishing path.

The daily target is a maximum, not a quota. Invalid or duplicate candidates are rejected, logged, rolled back, and skipped while later candidates and future scheduled runs continue.

## One-time GitHub setup

Install the Python dependencies and add the required repository secret:

```bash
python -m pip install -r tools/autopublisher/requirements.txt
```

- `GEMINI_API_KEY`: a Google AI Studio/Gemini API key with access to the configured models.

Optional repository variables can override the model defaults in `.autopublisher/config.json`:

- `GEMINI_TEXT_MODEL`
- `GEMINI_QA_MODEL`
- `GEMINI_GROUNDED_RESEARCH_MODEL`
- `GEMINI_IMAGE_MODEL`

The publishing workflow needs repository Actions **Read and write permissions** so its automatic `GITHUB_TOKEN` can commit accepted content. GitHub Models uses that same token for lightweight topic ranking and metadata tasks.

Optional direct Cloudflare Pages deployment still uses `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `CLOUDFLARE_PAGES_PROJECT`. Infrastructure upgrade pull requests are separate from content publishing and are never an approval gate for a valid article.

## Autonomous decision flow

1. Collect candidate pages from the configured official technical feeds.
2. Fetch each candidate, verify accessibility, canonical destination, readable title, freshness, trusted domain, and direct subject relevance.
3. Score the topic against the approved technical clusters and reject disallowed subjects before generation.
4. Compare title, slug, search intent, headings, body, n-grams, sources, category, and tags with existing content.
5. Build a topic-specific source bundle. If the feed does not contain enough directly relevant pages, run a focused grounded search and validate each returned citation before generation.
6. Generate an original article with claim-to-source evidence and at least two useful practical elements.
7. Validate source-backed claims, code and commands, hierarchy, introduction, metadata, controlled taxonomy, internal links, similarity, and quality score.
8. Run a separate AI accuracy and originality review using only the topic-specific validated excerpts.
9. Repair the failed section and repeat validation up to the configured retry limit.
10. Build Hugo, audit rendered metadata/JSON-LD/canonicals/sitemap, and publish automatically only when every critical gate passes.
11. Record failures and continue with another candidate when repair is exhausted.

Approved clusters are cybersecurity, identity and access management, networking, IT fundamentals, Azure, Entra ID, cloud certifications, system administration, practical infrastructure, and developer/IT tools. Celebrity, entertainment, political, automotive, lifestyle, random trend, consumer launch, and unrelated AI topics are explicitly blocked.

## Quality thresholds

Thresholds are centralized in `.autopublisher/config.json`. The baseline requires topic relevance of 0.78, overall quality of 0.82, three validated sources including an official source, three claim-evidence records, two practical elements, two contextual article links, source similarity no higher than 0.30, and zero critical errors.

A high aggregate score never overrides an unsupported material claim, invalid source, duplicate intent, invalid code, unsafe command without a warning, invalid canonical/JSON-LD, or human author identity.

Every claim-evidence record stores the claim, supporting source URLs, confidence, verification timestamp, and version context. New article front matter stores the site publisher, publication date, substantive update date, verification date/version, and scheduled recheck date. Formatting-only changes do not change `lastmod`.

## Recovery, state, and monitoring

Recovery can replace sources, gather additional official documentation, regenerate unsupported sections, rewrite similar text, correct code/metadata/links, and rerun scoring. Unsupported or source-similar drafts are regenerated from a clean slate instead of being pasted into the next prompt. A repeatedly stalled repair is abandoned early so the run can try another topic. The current configuration can attempt two dynamic topics and then up to three configured evergreen recovery slots backed by official documentation. Evergreen sources still undergo live URL, title, relevance, claim, similarity, and quality validation, and an evergreen topic is skipped once equivalent content exists. Every individual draft remains subject to the same fail-closed validation gates. When all attempts are exhausted, the candidate is recorded in `.autopublisher/state.json`, any public bundle is removed, and no sitemap entry is created. The workflow persists rejection state with a CI-skipping automation commit so the next run and monitoring tools retain the exact failure reason without publishing the rejected article.

`release_gate: no publication; result=rejected` means that no candidate passed the required evidence and quality checks. The release gate must not convert that result into a publication. Recovery happens upstream by collecting focused sources, regenerating the article, and moving to fresh topics.

- Runtime JSONL: `.autopublisher/logs/`
- Durable scheduler/rejection state: `.autopublisher/state.json`
- Machine-readable monitoring snapshot: `.autopublisher/dashboard.json`
- Existing-content risk inventory: `.autopublisher/reports/content-audit.json`
- Rendered-site validation: `.autopublisher/reports/rendered-site-audit.json`

The maintenance workflow audits legacy content, checks due articles and broken sources, researches changed facts, and changes visible `lastmod` only for a substantive accepted update. High-risk legacy pages are automatically noindexed until an evidence-backed repair succeeds.

## Local commands

```bash
python -m pip install -r tools/autopublisher/requirements.txt
python -m unittest discover -s tools/autopublisher -p "test_*.py"
python -m unittest discover -s tools/ci -p "test_*.py"
python tools/autopublisher/autopublisher.py --mode audit
python tools/autopublisher/autopublisher.py --mode existing-audit --dry-run
python tools/autopublisher/autopublisher.py --mode taxonomy --dry-run
hugo --gc --minify --destination public
python tools/autopublisher/autopublisher.py --mode rendered-audit --output-dir public
```

For a real autonomous local publish, set `GEMINI_API_KEY` and run:

```bash
python tools/autopublisher/autopublisher.py --mode publish
```

For an evidence-backed maintenance run:

```bash
python tools/autopublisher/autopublisher.py --mode maintain --max-articles 2
```
