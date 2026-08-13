# Infrastructure Maintenance Report

Generated: 2026-08-13T17:50:22Z

This report covers Hugo, the Mana theme dependencies, and Cloudflare Pages compatibility. It does not inspect, generate, revise, or publish blog content.

## Decision

- Manual review required: `True`
- Safe changes: `none`
- Review candidates: `Hugo 0.164.0 -> 0.165.0`
- Review reasons: Hugo medium-risk update 0.164.0 -> 0.165.0

## Hugo

- Current: `0.164.0`
- Latest: `0.165.0`
- Risk: `medium`
- Release: https://github.com/gohugoio/hugo/releases/tag/v0.165.0
- Release-note risk terms: `none`

## Mana theme dependencies

No theme dependency updates were reported.

## Infrastructure inventory

```json
{
  "python": "3.12.13",
  "hugo_pin": "0.164.0",
  "workflow_actions": {
    "infrastructure-maintenance.yml": [
      "actions/checkout@v6",
      "actions/setup-node@v6",
      "actions/setup-python@v6",
      "actions/upload-artifact@v7"
    ]
  },
  "theme": {
    "name": "mana-theme",
    "version": "1.5.0",
    "dependencies": {
      "@awmottaz/prettier-plugin-void-html": "^2.0.0",
      "prettier": "^3.7.4",
      "prettier-plugin-go-template": "^0.0.15"
    },
    "lockfile": false
  }
}
```

## Validation

- `/home/runner/work/_temp/hugo-current/hugo --minify`: passed
