# Autonomous publishing platform

Compile My Mind uses a fail-closed Hugo publisher driven by GitHub Actions. A candidate enters a private ready queue only after its topic, sources, material claims, code, originality, metadata, links, structured data, and rendered output pass the configured gates. The public publishing workflow promotes an already approved queue item before making any model or research request. There is no manual approval or pull-request approval in the content publishing path.

The preparer runs once per day and fills a four-article ready queue. The publisher runs on Monday, Wednesday, and Friday and accepts at most one article per run, so the hard operational ceiling is three new posts per week. Once the queue is populated, short provider outages, quota failures, bad topic selections, and draft repair failures do not cause a missed public publishing slot. Invalid candidates are rejected during preparation instead of being forced public to satisfy a schedule.

## One-time GitHub setup

Install the Python dependencies:

```bash
python -m pip install -r tools/autopublisher/requirements.txt
```

No user-supplied AI API key is required for scheduled generation. Each GitHub Actions job receives an automatic, short-lived `GITHUB_TOKEN` and grants it `models: read`. The publisher uses that token with OpenAI GPT-4.1 through GitHub Models, then falls back to OpenAI GPT-4.1 mini. The direct OpenAI API and Gemini routes are disabled in the production configuration, and the workflows do not inject `OPENAI_API_KEY` or `GEMINI_API_KEY`.

The request budget is deliberately compatible with the current GitHub Models free tier: at most 8,000 input tokens and 4,000 output tokens per request, with a much lower schedule. Article targets and repair depth are not reduced: the model still targets about 1,500 words and can make two evidence-driven repair attempts. Free access is rate-limited and is a preview service, so GitHub can change its limits or availability. If the free quota is temporarily exhausted, the publisher fails closed and retries later; it never switches to a paid provider or a generic offline template.

The publishing workflow needs repository Actions **Read and write permissions** so its automatic `GITHUB_TOKEN` can commit accepted content. GitHub Models must also be allowed for the repository or owning organization. Source discovery continues from trusted feeds and related official documentation without Gemini grounded search.

Optional direct Cloudflare Pages synchronization uses `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `CLOUDFLARE_PAGES_PROJECT`. Infrastructure upgrade pull requests are independent from content publishing and never block a valid article.

## Autonomous decision flow

1. Collect current candidates from configured official technical feeds and documentation sources.
2. Fetch each candidate and verify its HTTP result, canonical destination, readable title, freshness, trusted domain, content fingerprint, and direct subject relevance.
3. Score its fit against the approved technical clusters, compare rolling 7-day and 30-day category balance, and choose whether to create, update, expand, differentiate, or cancel.
4. Compare title, slug, search intent, headings, body, n-grams, sources, category, and tags with existing content.
5. Build a topic-specific source bundle. When a validated feed item links to related same-site documentation, rank and validate those pages first. If the bundle is still insufficient and Gemini search is available, run focused grounded research and revalidate every returned citation.
6. Generate an original article using a type-specific structure for tutorials, troubleshooting guides, explainers, certification guides, product/platform articles, and comparisons.
7. Validate claim-to-source mappings, source use, numerical context, code and commands, verification claims, practical depth, repetition, generic filler, metadata, controlled taxonomy, internal links, similarity, and quality score.
8. Run a separate accuracy and originality review using only the validated topic-specific evidence.
9. Repair the failed section and repeat the complete deterministic and AI review up to the configured retry limit. Repair prompts retain only a compact structural summary so a rejected draft cannot exceed a provider's input limit; HTTP 413 responses receive one smaller retry.
10. Build Hugo, audit rendered metadata, JSON-LD, canonicals, navigation counts, accessibility basics, and sitemap membership, then move the approved page bundle into the private ready queue.
11. On each public publishing run, promote the oldest non-expired approved queue bundle, refresh its publication and review dates, rebuild Hugo, and commit it without making a provider call.
12. Record invalid preparation candidates, preserve a source-bound retry when possible, and continue on later preparation runs. Failure on one candidate does not consume a buffered public publishing slot.

Approved clusters are cybersecurity, identity and access management, networking, IT fundamentals, Azure, Entra ID, cloud certifications, system administration, practical infrastructure, and developer/IT tools. Celebrity, entertainment, political, automotive, lifestyle, random trend, consumer launch, and unrelated AI topics are explicitly blocked.

New articles normally receive three topic tags. Metadata generation is given the site's existing tag vocabulary and usage counts, and the publisher deterministically chooses relevant existing tags before accepting a new tag. A new tag is permitted only when the reusable vocabulary cannot adequately describe the article. Category slugs are not repeated as substitute tags when more specific choices exist.

## Editorial and evidence gates

Thresholds are centralized in `.autopublisher/config.json`. The baseline requires topic relevance of 0.78, overall quality of 0.82, three validated sources including an official source, three claim-evidence records, two practical elements, two contextual article links, source similarity no higher than 0.30, and zero critical errors.

A high aggregate score never overrides a critical problem. Blocking checks include unsupported or overconfident material claims, unqualified hard numbers, unused or duplicate source content, invalid code, unsafe commands without warnings, vague troubleshooting, repeated or templated prose, misleading verification language, duplicate reader intent, invalid canonical or JSON-LD, fabricated authorship, and weak type-specific coverage.

Every claim-evidence record stores the claim, supporting source URLs, confidence, verification timestamp, and version context. New front matter records the site publisher, publication date, substantive update date, verification date/version/status, and scheduled recheck date. Allowed verification statuses describe the actual evidence, such as documentation review or test-account verification. Formatting-only edits do not change `lastmod`.

The current single-article design intentionally omits decorative featured images. Every newly approved article must contain a useful Markdown table, at least one rendered topic-specific SVG diagram, and at least one fenced code, command, configuration, query, or structured-data example. Code and a diagram are both mandatory; neither can replace the other. Each visual must appear inside the section whose concept, workflow, comparison, or example it explains. Charts are added only when validated source data provides the values, units, version, workload, and limitations; invented graphs and decorative media are rejected. Terminal visual summaries and diagram galleries are also rejected. Diagrams select among sequence, architecture, diagnostic-decision, comparison, and lifecycle layouts, use category-aware color themes, and must explain at least three nodes with topic-specific detail instead of generic step boxes. Social metadata uses the site's default Open Graph image. Raster images, when genuinely useful, require descriptive alt text and dimensions.

## Recovery, maintenance, and monitoring

Recovery can replace sources, gather additional official documentation, regenerate unsupported sections, rewrite similar text, correct code, metadata, and links, and rerun scoring. Unsupported or source-similar drafts are regenerated from a clean slate instead of being pasted into the next prompt. A repeatedly stalled repair is abandoned early so the run can try another topic. Source selection requires subject-specific overlap; a one-source candidate is accepted only when that validated page already exposes relevant official links that can be expanded and revalidated before drafting. The current configuration can attempt three dynamic topics, including when the evergreen catalog is exhausted, construct a conservative fallback only when at least three coherent validated sources support it, and then use up to three evergreen recovery slots. An evergreen topic is skipped once equivalent content exists, and all evergreen candidates pass the same live source, evidence, originality, and quality checks.

When all preparation attempts are exhausted, the invalid candidate is recorded in `.autopublisher/state.json`, any temporary public bundle is removed, and no sitemap entry is created. Preparation and public publishing keep separate pending-topic lanes, so an old public retry cannot pin every queue-filling run to the same failed candidate. The preparation run records `retry_scheduled`, preserves a compact topic and validated-source bundle when one is safe to reuse, and tries it again on a later daily cycle. After three failed cycles for the same topic, that topic is rotated out so a bad candidate cannot block future preparation. A successfully queued candidate clears both obsolete retry lanes.

Quota exhaustion opens a persistent cooldown for the affected model path instead of repeatedly making requests that cannot succeed. GPT-4.1 mini remains eligible when the primary GPT-4.1 route is unavailable. Production publishing does not use deterministic offline article templates: if model generation or QA cannot complete, it records the evidence and waits for a later run. Repeated empty `no_valid_topic` retries are coalesced so schedules continue without creating timestamp-only commits and deployments.

The queue is the delivery guarantee boundary: with at least one approved bundle available, every scheduled public run publishes without depending on live model availability. An absolutely unconditional guarantee is not technically honest if the queue has never been populated, is exhausted for longer than its buffer, GitHub itself is unavailable, or Hugo rejects the stored bundle after a repository-wide change. The four-item target provides more than one week of publishing inventory at the configured cadence, while daily preparation has enough opportunities to replenish three weekly publication slots. Quality gates remain fail-closed.

Content maintenance runs twice weekly and reviews up to four articles per run, enough to cycle the current library inside the normal 60-day review window. It checks live links, fetches current text from the article's existing trusted sources, asks the OpenAI model whether facts remain supported, and records source-backed verification without changing visible `lastmod`; `lastmod` changes only after a substantive accepted correction. The content audit also compares every existing post with every other post for matching reader intent and repeated five-word prose patterns, and revision priority increases when an older article reuses a house template too heavily. High-risk legacy content is noindexed until evidence-backed repair succeeds. Revision selection runs weekly, and infrastructure maintenance runs approximately every six months. Dependency automation inventories Python, Hugo, GitHub Actions, image-tool, and requirements state; it auto-applies only regression-tested patch upgrades whose release notes contain no breaking-change signals, exercises the publisher in dry-run mode with the workflow token, and puts higher-risk candidate pins plus their report into a tested draft review pull request without deploying them. Only a validated, non-review Hugo pin is synchronized to Cloudflare Pages. Gemini model maintenance is manual-only while Gemini is disabled.

Operational evidence is stored at:

- Runtime JSONL: `.autopublisher/logs/`
- Durable scheduler and rejection state: `.autopublisher/state.json`
- Pre-approved private page bundles: `.autopublisher/queue/ready/`
- Machine-readable monitoring snapshot: `.autopublisher/dashboard.json`
- Existing-content risk inventory: `.autopublisher/reports/content-audit.json`
- Latest maintenance outcome: `.autopublisher/reports/maintenance-latest.json`
- Rendered-site validation: `.autopublisher/reports/rendered-site-audit.json`
- Timestamped infrastructure inventories: `.autopublisher/reports/infrastructure-maintenance-*.md`

The dashboard exposes ready-queue depth, target and minimum health, discovered and rejected topics, rejection stages, source failures, unsupported/numerical/generic/repeated claim counts, code and secret-scan failures, similarity results, maintenance outcomes, and the last run per mode.

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

For a real local publish or evidence-backed maintenance run, set `GITHUB_MODELS_TOKEN` or `GITHUB_TOKEN` to a GitHub token with `models: read`. The automatic workflow token exists only inside GitHub Actions; scheduled jobs need no secret created by you.

```bash
python tools/autopublisher/autopublisher.py --mode publish
python tools/autopublisher/autopublisher.py --mode prepare
python tools/autopublisher/autopublisher.py --mode maintain --max-articles 2
```

Do not use those live modes merely to test configuration: they make provider calls and publish mode can create an accepted page bundle. Use the unit, audit, and rendered-audit commands for deterministic local validation.
