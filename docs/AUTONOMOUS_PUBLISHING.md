# Autonomous publishing platform

Compile My Mind uses a fail-closed Hugo publisher driven by GitHub Actions. A candidate is published automatically only after its topic, sources, material claims, code, originality, metadata, links, structured data, and rendered output pass the configured gates. There is no editor queue, manual approval, or pull-request approval in the content publishing path.

The publisher runs every six hours and can accept at most one article per run, so the hard operational ceiling is four new posts per day. This is a maximum, not a quota: invalid, weak, duplicate, or unsupported candidates are rejected internally and the publication is queued for another autonomous attempt instead of being forced public to satisfy a schedule.

## One-time GitHub setup

Install the Python dependencies and add the required repository secret:

```bash
python -m pip install -r tools/autopublisher/requirements.txt
```

- `GEMINI_API_KEY`: a Google AI Studio/Gemini API key with access to the configured models. It is required for Gemini grounded research, maintenance, and Gemini generation. A source-bound offline publication or an available GitHub Models task can still run when Gemini generation is unavailable.

Optional repository variables can override the model defaults in `.autopublisher/config.json`:

- `GEMINI_TEXT_MODEL`
- `GEMINI_QA_MODEL`
- `GEMINI_GROUNDED_RESEARCH_MODEL`
- `GEMINI_IMAGE_MODEL`

The publishing workflow needs repository Actions **Read and write permissions** so its automatic `GITHUB_TOKEN` can commit accepted content. GitHub Models can handle configured topic-selection, drafting, QA, and metadata tasks when those repository models are available. Gemini remains the configured grounded-research provider and the primary generation provider when GitHub Models cannot complete a task.

Optional direct Cloudflare Pages synchronization uses `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `CLOUDFLARE_PAGES_PROJECT`. Infrastructure upgrade pull requests are independent from content publishing and never block a valid article.

## Autonomous decision flow

1. Collect current candidates from configured official technical feeds and documentation sources.
2. Fetch each candidate and verify its HTTP result, canonical destination, readable title, freshness, trusted domain, content fingerprint, and direct subject relevance.
3. Score its fit against the approved technical clusters, compare rolling 7-day and 30-day category balance, and choose whether to create, update, expand, differentiate, or cancel.
4. Compare title, slug, search intent, headings, body, n-grams, sources, category, and tags with existing content.
5. Build a topic-specific source bundle. If the feed is insufficient, run focused grounded research and revalidate every returned citation.
6. Generate an original article using a type-specific structure for tutorials, troubleshooting guides, explainers, certification guides, product/platform articles, and comparisons.
7. Validate claim-to-source mappings, source use, numerical context, code and commands, verification claims, practical depth, repetition, generic filler, metadata, controlled taxonomy, internal links, similarity, and quality score.
8. Run a separate Gemini accuracy and originality review using only the validated topic-specific evidence.
9. Repair the failed section and repeat the complete deterministic and AI review up to the configured retry limit.
10. Build Hugo, audit rendered metadata, JSON-LD, canonicals, navigation counts, accessibility basics, and sitemap membership, then publish only when every critical gate passes.
11. Record the invalid candidate, queue a source-bound retry when possible, and continue to another candidate when repair is exhausted. Failure on one article does not stop later candidates or future scheduled runs.

Approved clusters are cybersecurity, identity and access management, networking, IT fundamentals, Azure, Entra ID, cloud certifications, system administration, practical infrastructure, and developer/IT tools. Celebrity, entertainment, political, automotive, lifestyle, random trend, consumer launch, and unrelated AI topics are explicitly blocked.

## Editorial and evidence gates

Thresholds are centralized in `.autopublisher/config.json`. The baseline requires topic relevance of 0.78, overall quality of 0.82, three validated sources including an official source, three claim-evidence records, two practical elements, two contextual article links, source similarity no higher than 0.30, and zero critical errors.

A high aggregate score never overrides a critical problem. Blocking checks include unsupported or overconfident material claims, unqualified hard numbers, unused or duplicate source content, invalid code, unsafe commands without warnings, vague troubleshooting, repeated or templated prose, misleading verification language, duplicate reader intent, invalid canonical or JSON-LD, fabricated authorship, and weak type-specific coverage.

Every claim-evidence record stores the claim, supporting source URLs, confidence, verification timestamp, and version context. New front matter records the site publisher, publication date, substantive update date, verification date/version/status, and scheduled recheck date. Allowed verification statuses describe the actual evidence, such as documentation review or test-account verification. Formatting-only edits do not change `lastmod`.

The current single-article design intentionally omits featured images. Automated page bundles can contain evidence-backed Mermaid diagrams, charts, tables, and other body visuals, while social metadata uses the site's default Open Graph image. The media gate rejects decorative or unsupported chart data and requires descriptive alt text and dimensions for raster images that do appear.

## Recovery, maintenance, and monitoring

Recovery can replace sources, gather additional official documentation, regenerate unsupported sections, rewrite similar text, correct code, metadata, and links, and rerun scoring. Unsupported or source-similar drafts are regenerated from a clean slate instead of being pasted into the next prompt. A repeatedly stalled repair is abandoned early so the run can try another topic. The current configuration can attempt three dynamic topics, construct a conservative fallback only when at least three coherent prevalidated sources support it, and then use up to three configured evergreen recovery slots backed by official documentation. An evergreen topic is skipped once equivalent content exists, and all evergreen candidates pass the same live source, evidence, originality, and quality checks.

When all attempts are exhausted, the invalid candidate is recorded in `.autopublisher/state.json`, any public bundle is removed, and no sitemap entry is created. The run records `retry_scheduled`, preserves a compact topic and validated-source bundle when one is safe to reuse, and tries it again on the next six-hour cycle. After three failed cycles for the same topic, that topic is rotated out so a bad candidate cannot block future publication. A successful publication clears the pending retry.

Billing-credit exhaustion opens a persistent seven-day circuit for the affected Gemini path instead of repeatedly spending requests that cannot succeed. GitHub Models and reviewed source-bound offline fallbacks remain eligible while that circuit is open. The offline recovery catalog includes Windows Time, Docker health-check, and Microsoft Entra Conditional Access workflows in addition to the previously published recovery topics. These fallbacks still require live validation of three official sources and the complete deterministic quality gate.

This retry policy improves eventual recovery; it does not promise that every scheduled run will publish. If no in-scope, nonduplicate, source-supported article passes all critical gates, the correct result is a queued retry with no public content change.

Content maintenance runs twice weekly, revision selection runs daily, and infrastructure maintenance runs approximately every six months. Maintenance audits due articles and broken sources, changes visible `lastmod` only after a substantive accepted update, and automatically noindexes a high-risk legacy page until evidence-backed repair succeeds. Dependency automation inventories Python, Hugo, GitHub Actions, Gemini model, image-tool, and requirements state; it auto-applies only regression-tested patch upgrades whose release notes contain no breaking-change signals, exercises the publisher in dry-run mode when provider credentials are available, and puts higher-risk candidate pins plus their report into a tested draft review pull request without deploying them.

Operational evidence is stored at:

- Runtime JSONL: `.autopublisher/logs/`
- Durable scheduler and rejection state: `.autopublisher/state.json`
- Machine-readable monitoring snapshot: `.autopublisher/dashboard.json`
- Existing-content risk inventory: `.autopublisher/reports/content-audit.json`
- Latest maintenance outcome: `.autopublisher/reports/maintenance-latest.json`
- Rendered-site validation: `.autopublisher/reports/rendered-site-audit.json`
- Timestamped infrastructure inventories: `.autopublisher/reports/infrastructure-maintenance-*.md`

The dashboard exposes discovered and rejected topics, rejection stages, source failures, unsupported/numerical/generic/repeated claim counts, code and secret-scan failures, similarity results, maintenance outcomes, and the last run per mode.

## Local verification

```bash
python -m pip install -r tools/autopublisher/requirements.txt
python -m unittest discover -s tools/autopublisher -p "test_*.py"
python -m unittest discover -s tools/ci -p "test_*.py"
python -m unittest discover -s tools/maintenance -p "test_*.py"
python tools/autopublisher/autopublisher.py --mode audit
python tools/autopublisher/autopublisher.py --mode existing-audit --dry-run
python tools/autopublisher/autopublisher.py --mode taxonomy --dry-run
hugo --gc --minify --cleanDestinationDir --destination public
python tools/autopublisher/autopublisher.py --mode rendered-audit --output-dir public
```

For a real autonomous local publish or evidence-backed maintenance run, set `GEMINI_API_KEY` and use one of these commands:

```bash
python tools/autopublisher/autopublisher.py --mode publish
python tools/autopublisher/autopublisher.py --mode maintain --max-articles 2
```

Do not use those live modes merely to test configuration: they make provider calls and publish mode can create an accepted page bundle. Use the unit, audit, and rendered-audit commands for deterministic local validation.
