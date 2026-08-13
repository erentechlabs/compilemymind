# Infrastructure maintenance

The repository contains one GitHub Actions workflow: `.github/workflows/infrastructure-maintenance.yml`. It runs every six months and can also be started manually.

The workflow is limited to three responsibilities:

1. Discover and validate a newer Hugo Extended release.
2. Discover and validate dependency updates for the Mana theme.
3. Synchronize the validated `HUGO_VERSION` setting with Cloudflare Pages.

It does not generate, revise, queue, review, or publish blog posts. When the Hugo pin changes, the workflow updates the Hugo badge and Tech Stack version in `README.md` in the same candidate. The change validator rejects any workflow mutation outside `.hugo-version`, those README version markers, the Mana theme package files, and `.maintenance/reports`.

Every candidate is checked with a complete site build, theme checks, and mutation validation. Candidates that pass all checks are committed directly to the default branch, including minor, major, and release-note-risk upgrades. Risk signals remain available in the maintenance report for diagnostics, but they do not create a pull request or require confirmation.

Cloudflare synchronization requires these repository settings:

- Secret: `CLOUDFLARE_API_TOKEN`
- Secret: `CLOUDFLARE_ACCOUNT_ID`
- Variable: `CLOUDFLARE_PAGES_PROJECT`

If any setting is missing, Hugo and theme maintenance still runs, but the Cloudflare synchronization step reports that it was skipped.

Local verification:

```powershell
python -m unittest discover -s tools/maintenance -p "test_*.py"
python tools/maintenance/validate_infrastructure_changes.py
hugo --minify
```
