#!/usr/bin/env python3
"""Regenerate the Open Source Contributions section of the profile README.

Queries GitHub for every PR the profile owner has opened against EXTERNAL
repositories (merged + still-open), renders a summary-count badge line plus a
collapsible table, and replaces whatever sits between the OSS markers in
README.md. Closed-unmerged PRs are intentionally excluded — the public profile
shows positive signal only, and a closed PR simply drops off the table.

Data source: `gh search prs` (two queries: --merged and --state open).
Curated one-line summaries come from oss_contributions_overrides.json; any PR
without an override falls back to a cleaned-up PR title.

Usage:
    python3 scripts/oss_contributions.py          # rewrite README.md in place
    python3 scripts/oss_contributions.py --check   # exit 1 if it would change
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

AUTHOR = "somaz94"
OWN_PREFIXES = ("somaz94/", "somaz-devops/")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
README = REPO_ROOT / "README.md"
OVERRIDES = SCRIPT_DIR / "oss_contributions_overrides.json"

MARKER_START = "<!-- OSS:START -->"
MARKER_END = "<!-- OSS:END -->"

MERGED_BADGE = "https://img.shields.io/badge/Merged-{n}-2EA44F?style=for-the-badge"
REVIEW_BADGE = "https://img.shields.io/badge/Review-{n}-0969DA?style=for-the-badge"

# Peel a leading "[scope]" tag and/or a Conventional-Commit "type:" prefix off
# a PR title when no curated override exists.
BRACKET_RE = re.compile(r"^\s*\[[^\]]+\]\s*")
CONV_RE = re.compile(
    r"^\s*(fix|feat|build|chore|docs|refactor|test|ci|perf|style)"
    r"(\([^)]+\))?!?:\s*",
    re.IGNORECASE,
)

JSON_FIELDS = "number,title,url,repository,createdAt"


def gh_search(*extra: str) -> list[dict]:
    """Run `gh search prs` for the author and return the parsed JSON array."""
    cmd = [
        "gh", "search", "prs",
        "--author", AUTHOR,
        "--limit", "200",
        "--json", JSON_FIELDS,
        *extra,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout or "[]")


def external(prs: list[dict]) -> list[dict]:
    """Drop PRs that target the owner's own repositories."""
    return [
        p for p in prs
        if not p["repository"]["nameWithOwner"].startswith(OWN_PREFIXES)
    ]


def clean_title(title: str) -> str:
    """Strip a leading [scope] tag / conventional-commit prefix and capitalize."""
    s = title.strip()
    for _ in range(3):  # peel stacked prefixes like "[mesheryctl] feat: ..."
        peeled = CONV_RE.sub("", BRACKET_RE.sub("", s)).strip()
        if peeled == s:
            break
        s = peeled
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s or title.strip()


def row(pr: dict, status: str, overrides: dict) -> str:
    repo = pr["repository"]["nameWithOwner"]
    key = f"{repo}#{pr['number']}"
    override = overrides.get(key, {})
    project = override.get("project", repo)
    summary = override.get("summary") or clean_title(pr["title"])
    return f"| {project} | [#{pr['number']}]({pr['url']}) | {summary} | {status} |"


def render(merged: list[dict], review: list[dict], overrides: dict) -> str:
    lines = [
        MARKER_START,
        "",
        f"![Merged]({MERGED_BADGE.format(n=len(merged))}) "
        f"![Review]({REVIEW_BADGE.format(n=len(review))})",
        "",
        "<details>",
        f"<summary><b>View all contributions ({len(merged) + len(review)})</b></summary>",
        "",
        "<br/>",
        "",
        "| Project | PR | Contribution | Status |",
        "|---|---|---|---|",
    ]
    lines += [row(p, "✅ Merged", overrides) for p in merged]
    lines += [row(p, "🔵 Review", overrides) for p in review]
    lines += ["", "</details>", "", MARKER_END]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if README.md is out of date instead of rewriting it",
    )
    args = parser.parse_args()

    overrides = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}

    merged = external(gh_search("--merged"))
    review = external(gh_search("--state", "open"))
    merged.sort(key=lambda p: p["createdAt"], reverse=True)
    review.sort(key=lambda p: p["createdAt"], reverse=True)

    block = render(merged, review, overrides)

    text = README.read_text()
    pattern = re.compile(
        re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
        re.DOTALL,
    )
    if not pattern.search(text):
        print(
            f"error: markers {MARKER_START} / {MARKER_END} not found in {README}",
            file=sys.stderr,
        )
        return 2

    new_text = pattern.sub(lambda _: block, text)

    if new_text == text:
        print(f"OSS section already up to date ({len(merged)} merged, {len(review)} in review).")
        return 0
    if args.check:
        print("OSS section is OUT OF DATE (run without --check to update).", file=sys.stderr)
        return 1

    README.write_text(new_text)
    print(f"Updated OSS section: {len(merged)} merged, {len(review)} in review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
