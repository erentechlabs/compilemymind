#!/usr/bin/env python3
"""Rewrite existing posts into readable, article-style Markdown with Gemini QA."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "autopublisher"))
import autopublisher  # noqa: E402


REVISION_STATE_PATH = ROOT / ".autopublisher" / "revision-state.json"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_state() -> dict[str, Any]:
    if not REVISION_STATE_PATH.exists():
        return {"schema_version": 1, "completed": [], "attempts": {}, "last_run": {}}
    return json.loads(REVISION_STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    REVISION_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def markdown_images(markdown: str) -> list[str]:
    pattern = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")
    return [match.group(1) or match.group(2) for match in pattern.finditer(markdown)]


def code_fences(markdown: str) -> list[tuple[str, str]]:
    return re.findall(r"```([^\n]*)\n(.*?)```", markdown, flags=re.S)


def prose_paragraph_count(markdown: str) -> int:
    count = 0
    for block in re.split(r"\n\s*\n", markdown.strip()):
        block = block.strip()
        if not block or block.startswith(("#", "```", "- ", "* ", "+ ", ">", "|", "![")):
            continue
        if re.match(r"^\d+[.)]\s", block):
            continue
        if len(re.findall(r"\b\w+[\w'-]*\b", block)) >= 20:
            count += 1
    return count


def readability_signals(body: str) -> dict[str, int]:
    fences = code_fences(body)
    bullet_lines = len(re.findall(r"(?m)^\s*(?:[-*+] |\d+[.)]\s)", body))
    prose_fence_bullets = 0
    for language, fenced_body in fences:
        if language.strip().lower() in {"", "text", "plaintext", "plain"}:
            prose_fence_bullets += len(re.findall(r"(?m)^\s*(?:[-*+] |\d+[.)]\s)", fenced_body))
    return {
        "words": len(re.findall(r"\b[\w+#.-]+\b", body)),
        "paragraphs": prose_paragraph_count(body),
        "code_fences": len(fences),
        "code_examples": sum(1 for language, _body in fences if language.strip().lower() not in {"", "text", "plaintext", "plain"}),
        "bullet_lines": bullet_lines,
        "prose_fence_bullets": prose_fence_bullets,
    }


def revision_priority(post: autopublisher.Post) -> tuple[int, int, str]:
    signals = readability_signals(post.body)
    priority = 0
    if signals["prose_fence_bullets"]:
        priority += 100
    if signals["paragraphs"] < 5:
        priority += 35
    if signals["bullet_lines"] >= 20:
        priority += 25
    if signals["words"] > 2500 and signals["paragraphs"] < 12:
        priority += 20
    return (-priority, -signals["words"], post.slug)


def choose_post(
    posts: list[autopublisher.Post], state: dict[str, Any], attempted_this_run: set[str] | None = None
) -> tuple[autopublisher.Post | None, dict[str, int]]:
    completed = set(state.get("completed", []) or [])
    attempted_this_run = attempted_this_run or set()
    pending = [post for post in posts if post.slug not in completed and post.slug not in attempted_this_run]
    if not pending:
        return None, {}
    pending.sort(key=revision_priority)
    post = pending[0]
    return post, readability_signals(post.body)


def existing_sources(post: autopublisher.Post) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for url in autopublisher.extract_links(post.body):
        if not url.startswith(("http://", "https://")):
            continue
        if urllib.parse.urlparse(url).netloc.lower() in {"compilemymind.com", "www.compilemymind.com"}:
            continue
        if url in {source["url"] for source in sources}:
            continue
        sources.append({"title": urllib.parse.urlparse(url).netloc, "url": url})
    return sources


def revision_prompt(post: autopublisher.Post, signals: dict[str, int], config: dict[str, Any]) -> str:
    sources = existing_sources(post)
    return f"""
You are the senior technical editor for Compile My Mind. Rewrite this existing technical blog post into a readable, standard long-form article.

Readability problems to correct:
- Do not put explanations, ordinary prose, bullet lists, or pseudo-code inside fenced code blocks.
- Keep fenced blocks only for real source code, commands, configuration, SQL, JSON, YAML, structured output, or short terminal output.
- Convert dense cheat-sheet fragments into explanatory paragraphs with transitions.
- Use bullet lists only when they improve scanning; do not make the article a wall of bullets.
- Use tables for genuine comparisons, definitions, or structured reference data.
- Retain useful diagrams, charts, statistics, examples, internal links, and source links.

Editorial requirements:
- Preserve the factual meaning and technical scope of the original.
- Do not invent statistics, benchmarks, product capabilities, versions, prices, or citations.
- Keep existing local image filenames exactly unchanged so the page bundle remains valid.
- Add a clear introduction, explanatory sections, practical examples, and a concise conclusion where missing.
- Keep actual code examples complete and correctly fenced with a language identifier.
- Do not add YAML front matter, a top-level H1, featured images, thumbnails, or hero images.
- Use Markdown body only.

Current readability signals:
{json.dumps(signals, ensure_ascii=False, indent=2)}

Existing metadata:
{json.dumps({"title": post.title, "description": post.description, "tags": post.tags, "categories": post.categories}, ensure_ascii=False, indent=2)}

Existing source URLs:
{json.dumps(sources, ensure_ascii=False, indent=2)}

Current Markdown:
{post.body[:50000]}

Return JSON only:
{{
  "action": "update",
  "updated_markdown": "Markdown body only",
  "description": "updated 145-180 character description",
  "tags": [],
  "categories": [],
  "sources": [],
  "revision_notes": "short summary of readability improvements"
}}
""".strip()


def validate_revision(
    post: autopublisher.Post,
    updated: str,
    payload: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    updated = updated.strip()
    if not updated:
        return ["The revision returned an empty body."]
    issues.extend(autopublisher.markdown_format_issues(updated))
    if re.search(r"(?m)^#\s+", updated):
        issues.append("The body contains a top-level H1.")
    if updated.count("```") % 2:
        issues.append("Code fences are unbalanced.")
    current_words = len(re.findall(r"\b[\w+#.-]+\b", post.body))
    updated_words = len(re.findall(r"\b[\w+#.-]+\b", updated))
    ratio = float(config.get("revision", {}).get("min_preserved_length_ratio", 0.72))
    if updated_words < max(500, int(current_words * ratio)):
        issues.append(f"The rewrite is too short: {updated_words} words versus {current_words} original words.")
    min_paragraphs = int(config.get("revision", {}).get("min_paragraphs", 5))
    if prose_paragraph_count(updated) < min_paragraphs:
        issues.append("The rewrite does not contain enough explanatory paragraphs.")
    for language, fenced_body in code_fences(updated):
        if language.strip().lower() in {"", "text", "plaintext", "plain"} and re.search(
            r"(?m)^\s*(?:[-*+] |\d+[.)]\s)", fenced_body
        ):
            issues.append("A prose-style code fence still contains a bullet or numbered list.")
            break

    original_images = [ref for ref in markdown_images(post.body) if not ref.startswith(("http://", "https://", "data:", "/"))]
    missing_images = [ref for ref in original_images if ref not in updated]
    if missing_images:
        issues.append(f"The rewrite dropped existing local image references: {missing_images[:5]}")

    original_urls = set(autopublisher.extract_links(post.body))
    site_base = str(config.get("site", {}).get("base_url", "")).rstrip("/")
    new_external = [
        url for url in autopublisher.extract_links(updated)
        if url.startswith(("http://", "https://")) and url not in original_urls and not (site_base and url.startswith(site_base))
    ]
    if new_external:
        issues.append(f"The rewrite introduced uncited external links: {new_external[:5]}")

    current_code = readability_signals(post.body)["code_examples"]
    updated_code = readability_signals(updated)["code_examples"]
    if current_code >= 2 and updated_code < max(1, int(current_code * 0.6)):
        issues.append("The rewrite removed too many actual code examples.")
    if payload.get("action", "update").lower() not in {"update", "none"}:
        issues.append(f"Unsupported revision action: {payload.get('action')}")
    return issues


def record_attempt(state: dict[str, Any], slug: str, result: str, **fields: Any) -> None:
    attempts = state.setdefault("attempts", {})
    entry = attempts.setdefault(slug, {"count": 0, "history": []})
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["history"].append({"time": now_iso(), "result": result, **fields})
    entry["history"] = entry["history"][-5:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-posts", type=int, default=None)
    args = parser.parse_args()

    config = autopublisher.load_config()
    if not config.get("revision", {}).get("enabled", True):
        print("Existing-post revision is disabled.")
        return 0
    state = read_state()
    log = autopublisher.EventLog()
    client = autopublisher.GeminiClient(config, log)
    client.require_key()
    posts = autopublisher.load_posts(config)
    limit = args.max_posts or int(config.get("revision", {}).get("max_posts_per_run", 1))
    changed = False
    attempted_this_run: set[str] = set()
    selected_any = False

    for _ in range(max(1, limit)):
        post, signals = choose_post(posts, state, attempted_this_run)
        if not post:
            print("No eligible existing post remains for this revision run.")
            break
        attempted_this_run.add(post.slug)
        selected_any = True
        log.log("revision_started", slug=post.slug, title=post.title, signals=signals)
        try:
            payload = client.generate_json(revision_prompt(post, signals, config), task="revision")
        except Exception as error:
            record_attempt(state, post.slug, "generation_failed", error=str(error))
            log.log("revision_generation_failed", slug=post.slug, error=str(error))
            continue

        action = str(payload.get("action", "update")).lower()
        updated = autopublisher.remove_accidental_frontmatter(str(payload.get("updated_markdown", "")))
        issues = validate_revision(post, updated, payload, config) if action == "update" else []
        if issues:
            record_attempt(state, post.slug, "rejected", issues=issues)
            log.log("revision_rejected", slug=post.slug, issues=issues)
            continue

        if action == "none":
            state.setdefault("completed", []).append(post.slug)
            record_attempt(state, post.slug, "no_change", reason=payload.get("revision_notes", ""))
            log.log("revision_no_change", slug=post.slug)
            changed = True
            continue

        sources = existing_sources(post)
        supplied_sources = payload.get("sources", []) or []
        allowed_urls = {source["url"] for source in sources}
        for source in supplied_sources:
            if isinstance(source, dict) and str(source.get("url", "")).strip() in allowed_urls:
                url = str(source["url"]).strip()
                if url not in {item["url"] for item in sources}:
                    sources.append({"title": str(source.get("title", "")).strip() or urllib.parse.urlparse(url).netloc, "url": url})

        metadata_article = {
            "title": post.title,
            "description": str(payload.get("description") or post.description),
            "summary": str(payload.get("summary") or ""),
            "categories": payload.get("categories") or post.categories,
            "tags": payload.get("tags") or post.tags,
            "article_markdown": updated,
        }
        autopublisher.enrich_article_metadata(
            client,
            metadata_article,
            {"title": post.title, "categories": post.categories, "tags": post.tags},
            config,
            log,
        )
        article_for_qa = {
            "title": post.title,
            "description": metadata_article["description"],
            "summary": metadata_article.get("summary", ""),
            "categories": metadata_article["categories"],
            "tags": metadata_article["tags"],
            "sources": sources,
            "diagrams": [],
            "charts": [],
            "article_markdown": updated,
        }
        try:
            qa = autopublisher.ai_qa(client, article_for_qa, {"title": post.title, "search_intent": "Editorial readability revision"}, config, log)
        except Exception as error:
            record_attempt(state, post.slug, "qa_failed", error=str(error))
            log.log("revision_qa_failed", slug=post.slug, error=str(error))
            continue
        try:
            qa_score = float(qa.get("score", 0) or 0)
        except (TypeError, ValueError):
            qa_score = 0.0
        approved = (qa.get("approved") is True or str(qa.get("approved", "")).lower() == "true") and qa_score >= float(config.get("publishing", {}).get("quality_min_score", 0.78))
        if not approved:
            record_attempt(state, post.slug, "qa_rejected", score=qa_score, feedback=qa.get("issues", []))
            log.log("revision_qa_rejected", slug=post.slug, score=qa_score, issues=qa.get("issues", []))
            continue

        original_text = post.path.read_text(encoding="utf-8")
        autopublisher.update_post_file(
            post,
            {
                "updated_markdown": updated,
                "description": metadata_article["description"],
                "summary": metadata_article.get("summary", ""),
                "tags": metadata_article["tags"],
                "categories": metadata_article["categories"],
                "sources": sources,
            },
            config,
        )
        asset_issues = autopublisher.content_asset_issues(config)
        if asset_issues:
            post.path.write_text(original_text, encoding="utf-8")
            record_attempt(state, post.slug, "asset_rejected", issues=asset_issues)
            log.log("revision_asset_rejected", slug=post.slug, issues=asset_issues)
            continue

        state.setdefault("completed", []).append(post.slug)
        record_attempt(state, post.slug, "updated", score=qa_score, notes=payload.get("revision_notes", ""))
        log.log("revision_updated", slug=post.slug, score=qa_score, notes=payload.get("revision_notes", ""))
        changed = True
        posts = autopublisher.load_posts(config)

    state["last_run"] = {"time": now_iso(), "changed": changed}
    if selected_any:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
