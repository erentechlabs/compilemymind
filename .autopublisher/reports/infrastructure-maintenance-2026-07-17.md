# Infrastructure Maintenance Report

Generated: 2026-07-17T20:40:45Z

This report was created by the autonomous maintenance workflow. Only patch updates may be committed automatically after candidate-version validation passes; minor and major changes require review.

## Decision

- Manual review required: `False`
- Safe changes: `none`
- Review candidates: `none`
- Review reasons: none

## Hugo

- Current: `0.164.0`
- Latest: `0.164.0`
- Action: none
- Release: https://github.com/gohugoio/hugo/releases/tag/v0.164.0
- Published: `2026-07-06T17:48:54Z`
- Release-note risk terms: `deprecated`

### Release-note excerpt

Notable new features in this release are:

* The Chroma highlighter styles now introduces [dark/light pairs](https://gohugo.io/quick-reference/syntax-highlighting-styles/#modes). See also the new flags on the [hugo gen chromastyles](/commands/hugo_gen_chromastyles/) command.
* New template funcs [encoding.HexEncode](https://gohugo.io/functions/encoding/hexencode/), [encoding.HexDecode](https://gohugo.io/functions/encoding/hexdecode/), and [crypto.Hash](https://gohugo.io/functions/crypto/hash/).
* New [markup.rst.syntaxHighlight](https://gohugo.io/configuration/markup/#syntaxhighlight) option.
* We added Pandoc citation support.
* We now spport sub paths in layouts passed to [Page.Render](https://gohugo.io/methods/page/render/#article).
* This release also fixes a performance regression introduced in Hugo v0.128.0. This should mostly be prominent in bigger sites. See [this discussion](https://discourse.gohugo.io/t/hugo-building-slowly-from-release-0-128-0/57314/20) for some background.  

## Notes

* tpl/resources: Deprecate resources.PostProcess in favour of templates.Defer 29ed9325 @bep #15086 

## Changes

* all: Rewrite deprecated constructs in tests 5a5f4a54 @bep 
* tpl/tplimpl: Support sub paths in layouts passed to .Render d83ce27a @bep #15056 
* Add markup.rst.syntaxHighlight option c6acc246 @bep #5349 
* tpl/resources: Deprecate resources.PostProcess in favour of templates.Defer 29ed9325 @bep #15086 
* tpl/collections: Include key in IsSet unsupported-type warning 671897ae @bejaratommy #11794 
* create: Keep new content placeholders buildable 499794d1 @sjh9714 #15078 
* hugio: Speedup hasBytesWriter 65c82178 @bep 
* tpl/crypto: Add crypto.Hash dfb35dcd @bep #15072 
* Add encoding.HexDecode/Encode a5ec5423 @bep #15068 #15060 
* tpl/tplimpl: Make template name lookup case-insensitive e46d37a9 @jmooring #15057 
* hugolib: Return error from .Render when template not found fe067352 @jmooring #15052 
* markup/pandoc: Add citation support 128fb17c @jmooring #15062 

## Dependency Updates

* build(deps): bump github.com/JohannesKaufmann/html-to-markdown/v2 921db7b5 @dependabot[bot] 
* build(deps): bump golang.org/x/tools from 0.45.0 to 0.47.0 786ce71e @dependabot[bot] 
* build(deps): bump golang.org/x/image from 0.42.0 to 0.43.0 5ad28461 @dependabot[bot] 
* build(deps): bump golang.org/x/net from 0.55.0 to 0.56.0 36ad9f58 @dependabot[bot] 
* build(deps): bump github.com/pelletier/go-toml/v2 from 2.4.2 to 2.4.3 7c0a0bc9 @dependabot[bot] 
* build(deps): bump github.com/getkin/kin-openapi from 0.139.0 to 0.140.0 a879ebfa @dependabot[bot] 
* build(deps): bump golang.org/x/mod from 0.36.0 to 0.37.0 332d5ec8 @dependabot[bot] 
* build(deps): bump github.com/pelletier/go-toml/v2 from 2.3.1 to 2.4.2 212cc11a @dependabot[bot] 
* deps: Upgrade github.com/evanw/esbuild v0.28.0 => v0.28.1 884439b9 @bep #15033 
* deps: Add Chroma dark/light mode support 790a8aa4 @bep #15017 







### Migration and rollback

Before merging a review candidate, resolve every release-note risk above and document any required template, configuration, or command migration in the pull request.
Rollback restores the previous `.hugo-version` shown as Current above and the previous `themes/mana/package-lock.json`, then reruns the complete regression and rendered-site gates.

## npm Theme Tooling

No npm package updates were reported.

## Dependency Inventory

```json
{
  "python": "3.12.13",
  "hugo_pin": "0.164.0",
  "workflow_actions": {
    "autonomous-maintenance.yml": [
      "actions/checkout@v6",
      "actions/setup-python@v6",
      "actions/upload-artifact@v7"
    ],
    "autonomous-publish.yml": [
      "actions/checkout@v6",
      "actions/setup-python@v6",
      "actions/upload-artifact@v7"
    ],
    "deploy.yml": [
      "actions/checkout@v6",
      "actions/setup-python@v6"
    ],
    "gemini-model-maintenance.yml": [
      "actions/checkout@v6",
      "actions/setup-python@v6"
    ],
    "infrastructure-maintenance.yml": [
      "actions/checkout@v6",
      "actions/setup-python@v6",
      "actions/upload-artifact@v7"
    ],
    "revise-existing-posts.yml": [
      "actions/checkout@v6",
      "actions/setup-python@v6",
      "actions/upload-artifact@v7"
    ]
  },
  "gemini_models": {
    "text_model": "gemini-3.5-flash",
    "qa_model": "gemini-3.5-flash",
    "grounded_research_model": "gemini-3.5-flash",
    "image_model": "gemini-3.1-flash-image"
  },
  "image_tools": {
    "hugo": "/home/runner/work/_temp/hugo/hugo",
    "magick": null,
    "cwebp": null,
    "avifenc": null
  },
  "python_requirements": [
    "PyYAML==6.0.3"
  ]
}
```

## Validation

- `/home/runner/work/_temp/hugo/hugo --minify`: passed
