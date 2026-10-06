#!/usr/bin/env python3
"""Regenerate the Open Source Contributions catalog of the profile repo.

Queries GitHub for every PR the profile owner has opened against EXTERNAL
repositories (merged + still-open) and writes three outputs:

- OSS_CONTRIBUTIONS.md — the full catalog: count badges, a per-area summary
  table, and one table per area.
- data/oss-stats.json — the merged / review / total counts. The README badges
  are shields.io dynamic badges that read this file, so a count change never
  rewrites README.md.
- README.md — only the block between the OSS markers: the dynamic badges and a
  link to the catalog. The block is constant, so README.md changes only when
  this template does, or when a hand-edit inside the markers is reverted.

Closed-unmerged PRs are intentionally excluded — the public profile shows
positive signal only, and a closed PR simply drops off the catalog.

One exception: a PR a maintainer squash-merges under a fresh commit SHA stays
CLOSED (not MERGED) on GitHub, so `gh search prs --merged` can't see it and it
would silently vanish despite being a real merge. Flag such a PR `"merged": true`
in oss_contributions_overrides.json to re-include it as merged.

Data source: `gh search prs` (two queries: --merged and --state open), plus
`gh pr view` for any forced-merged override. Curated one-line summaries come
from oss_contributions_overrides.json; any PR without an override falls back to
a cleaned-up PR title.

Usage:
    python3 scripts/oss_contributions.py          # rewrite the outputs in place
    python3 scripts/oss_contributions.py --check   # exit 1 if any would change
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

AUTHOR = "somaz94"
OWN_PREFIXES = ("somaz94/", "somaz-devops/")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
README = REPO_ROOT / "README.md"
CATALOG = REPO_ROOT / "OSS_CONTRIBUTIONS.md"
STATS = REPO_ROOT / "data" / "oss-stats.json"
OVERRIDES = SCRIPT_DIR / "oss_contributions_overrides.json"

MARKER_START = "<!-- OSS:START -->"
MARKER_END = "<!-- OSS:END -->"

# README.md renders on the profile page as well as in the repo; absolute URLs
# resolve the same in both.
CATALOG_URL = f"https://github.com/{AUTHOR}/{AUTHOR}/blob/main/OSS_CONTRIBUTIONS.md"
STATS_RAW_URL = f"https://raw.githubusercontent.com/{AUTHOR}/{AUTHOR}/main/data/oss-stats.json"

MERGED_COLOR = "2EA44F"
REVIEW_COLOR = "0969DA"

# The search API stops at 1000 results, so a query that fills the limit has
# been truncated — fail instead of silently dropping the oldest PRs.
SEARCH_LIMIT = 1000

# Peel a leading "[scope]" tag and/or a Conventional-Commit "type:" prefix off
# a PR title when no curated override exists.
BRACKET_RE = re.compile(r"^\s*\[[^\]]+\]\s*")
CONV_RE = re.compile(
    r"^\s*(fix|feat|build|chore|docs|refactor|test|ci|perf|style)"
    r"(\([^)]+\))?!?:\s*",
    re.IGNORECASE,
)

JSON_FIELDS = "number,title,url,repository,createdAt"

# Ordered display sections. compress=True collapses still-in-review PRs to a
# single "+N in review" line; merged PRs are always listed individually.
# compress=False lists every PR as its own table row. Every section is currently
# compress=False so each PR — merged or in-review — is a discrete row; the
# compress mechanism is retained for any future campaign that grows large
# enough to warrant collapsing.
# A PR's section comes from its override "category"; if absent, the three
# largest campaigns are inferred from the summary, else it lands in "misc".
DEFAULT_CATEGORY = "misc"
# Cap how many still-in-review PR links a compressed section lists, so the line
# stays bounded no matter how large a campaign grows.
IN_REVIEW_PREVIEW = 10
CATEGORIES: list[tuple[str, str, bool]] = [
    ("httproute", "Gateway API HTTPRoute support · Helm charts", False),
    ("misc", "Standalone contributions", False),
    ("action-version-file", "GitHub Action `version-file` inputs", False),
    ("ngf-gocyclo", "nginx-gateway-fabric cyclomatic-complexity refactors · #5253", False),
    ("schema-json", "helm-values-schema-json features", False),
    ("moto-aws", "moto AWS API mocks", False),
]


def gh_search(*extra: str) -> list[dict]:
    """Run `gh search prs` for the author and return the parsed JSON array."""
    cmd = [
        "gh", "search", "prs",
        "--author", AUTHOR,
        "--limit", str(SEARCH_LIMIT),
        "--json", JSON_FIELDS,
        *extra,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    prs = json.loads(result.stdout or "[]")
    if len(prs) >= SEARCH_LIMIT:
        raise RuntimeError(
            f"`gh search prs {' '.join(extra)}` hit the {SEARCH_LIMIT}-result cap, "
            "so the list is truncated; split the query by creation date"
        )
    return prs


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


def key_of(pr: dict) -> str:
    return f"{pr['repository']['nameWithOwner']}#{pr['number']}"


def forced_merged(overrides: dict) -> list[dict]:
    """Synthesize PR records for entries flagged `"merged": true` in overrides.

    Some maintainers land a PR by squashing it onto their default branch under a
    fresh commit SHA. GitHub then can't auto-close the original PR, so it stays
    in the CLOSED (not MERGED) state and `gh search prs --merged` never returns
    it — the contribution would silently vanish. Flagging the PR `"merged": true`
    re-includes it; its metadata is pulled with `gh pr view`, since the search
    queries won't surface a closed PR.
    """
    records: list[dict] = []
    for key, meta in overrides.items():
        if not meta.get("merged"):
            continue
        repo, num = key.rsplit("#", 1)
        result = subprocess.run(
            ["gh", "pr", "view", num, "--repo", repo,
             "--json", "number,title,url,createdAt"],
            capture_output=True, text=True, check=True,
        )
        pr = json.loads(result.stdout)
        pr["repository"] = {"nameWithOwner": repo}
        records.append(pr)
    return records


def summary_of(pr: dict, overrides: dict) -> str:
    return overrides.get(key_of(pr), {}).get("summary") or clean_title(pr["title"])


def category_of(pr: dict, summary: str, overrides: dict) -> str:
    """Resolve a PR's section: explicit override "category" wins, else infer the
    big repeating campaigns from the summary, else fall back to the catch-all."""
    explicit = overrides.get(key_of(pr), {}).get("category")
    if explicit:
        return explicit
    s = summary.lower()
    repo = pr["repository"]["nameWithOwner"]
    if "httproute" in s:
        return "httproute"
    if repo == "nginx/nginx-gateway-fabric" and ("gocyclo" in s or "cyclomatic" in s):
        return "ngf-gocyclo"
    if "version-file" in s or "version_file" in s or ".tool-versions" in s:
        return "action-version-file"
    return DEFAULT_CATEGORY


def table_row(pr: dict, summary: str, status: str, overrides: dict) -> str:
    project = overrides.get(key_of(pr), {}).get("project", pr["repository"]["nameWithOwner"])
    return f"| {project} | [#{pr['number']}]({pr['url']}) | {summary} | {status} |"


def group_by_category(
    merged: list[dict], review: list[dict], overrides: dict
) -> dict[str, dict[str, list]]:
    """Bucket each PR into its section, preserving the (date-sorted) input order
    and the merged/review split."""
    groups: dict[str, dict[str, list]] = {
        key: {"merged": [], "review": []} for key, _, _ in CATEGORIES
    }
    for bucket, prs in (("merged", merged), ("review", review)):
        for pr in prs:
            summary = summary_of(pr, overrides)
            cat = category_of(pr, summary, overrides)
            groups[cat if cat in groups else DEFAULT_CATEGORY][bucket].append((pr, summary))
    return groups


def anchor(heading: str) -> str:
    """GitHub's heading slug: lowercase, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def static_badge(label: str, n: int, color: str) -> str:
    return f"https://img.shields.io/badge/{label}-{n}-{color}?style=for-the-badge"


def dynamic_badge(label: str, key: str, color: str) -> str:
    """A shields.io badge whose value is read from data/oss-stats.json at view time."""
    query = urlencode({
        "url": STATS_RAW_URL,
        "query": f"$.{key}",
        "label": label,
        "color": color,
        "style": "for-the-badge",
    })
    return f"https://img.shields.io/badge/dynamic/json?{query}"


def section_lines(
    heading: str, compress: bool, group: dict[str, list], overrides: dict
) -> list[str]:
    merged, review = group["merged"], group["review"]
    lines = [f"## {heading}", ""]

    listed = [(pr, s, "✅ Merged") for pr, s in merged]
    if not compress:
        listed += [(pr, s, "🔵 Review") for pr, s in review]
    if listed:
        lines += ["| Project | PR | Contribution | Status |", "|---|---|---|---|"]
        lines += [table_row(pr, s, status, overrides) for pr, s, status in listed]
        lines.append("")

    if compress and review:
        shown = ", ".join(f"[#{pr['number']}]({pr['url']})" for pr, _ in review[:IN_REVIEW_PREVIEW])
        if len(review) > IN_REVIEW_PREVIEW:
            shown += ", …"
        lines += [f"_In review ({len(review)}): {shown}_", ""]
    return lines


def render_catalog(merged: list[dict], review: list[dict], overrides: dict) -> str:
    groups = group_by_category(merged, review, overrides)
    present = [
        (key, heading, compress)
        for key, heading, compress in CATEGORIES
        if groups[key]["merged"] or groups[key]["review"]
    ]
    lines = [
        "<!-- Generated by scripts/oss_contributions.py — do not edit by hand. "
        "Curate summaries in scripts/oss_contributions_overrides.json. -->",
        "",
        "# Open Source Contributions",
        "",
        "Pull requests opened against external open-source projects, merged and "
        "still in review. Closed-unmerged pull requests are not listed.",
        "",
        f"![Merged]({static_badge('Merged', len(merged), MERGED_COLOR)}) "
        f"![Review]({static_badge('Review', len(review), REVIEW_COLOR)})",
        "",
        "| Area | Merged | Review | Total |",
        "|---|---|---|---|",
    ]
    for key, heading, _ in present:
        n_merged, n_review = len(groups[key]["merged"]), len(groups[key]["review"])
        lines.append(
            f"| [{heading}](#{anchor(heading)}) | {n_merged} | {n_review} | {n_merged + n_review} |"
        )
    lines.append("")
    for key, heading, compress in present:
        lines += ["<br/>", ""]
        lines += section_lines(heading, compress, groups[key], overrides)
    return "\n".join(lines).rstrip("\n") + "\n"


def render_stats(merged: list[dict], review: list[dict]) -> str:
    stats = {"merged": len(merged), "review": len(review), "total": len(merged) + len(review)}
    return json.dumps(stats, indent=2) + "\n"


def render_readme_block() -> str:
    return "\n".join([
        MARKER_START,
        "",
        f"![Merged]({dynamic_badge('Merged', 'merged', MERGED_COLOR)}) "
        f"![Review]({dynamic_badge('Review', 'review', REVIEW_COLOR)})",
        "",
        f"**[View all contributions →]({CATALOG_URL})**",
        "",
        MARKER_END,
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any output is out of date instead of rewriting it",
    )
    args = parser.parse_args(argv)

    overrides = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}

    merged = external(gh_search("--merged"))
    review = external(gh_search("--state", "open"))

    # Re-include PRs a maintainer squash-merged under a new SHA: GitHub leaves
    # them CLOSED so `--merged` misses them. They are flagged "merged": true in
    # the overrides and must not also appear in the in-review list.
    merged_keys = {key_of(p) for p in merged}
    for pr in forced_merged(overrides):
        if key_of(pr) not in merged_keys:
            merged.append(pr)
            merged_keys.add(key_of(pr))
    review = [p for p in review if key_of(p) not in merged_keys]

    merged.sort(key=lambda p: p["createdAt"], reverse=True)
    review.sort(key=lambda p: p["createdAt"], reverse=True)

    readme_text = README.read_text()
    pattern = re.compile(
        re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
        re.DOTALL,
    )
    if not pattern.search(readme_text):
        print(
            f"error: markers {MARKER_START} / {MARKER_END} not found in {README}",
            file=sys.stderr,
        )
        return 2

    outputs = {
        README: pattern.sub(lambda _: render_readme_block(), readme_text),
        CATALOG: render_catalog(merged, review, overrides),
        STATS: render_stats(merged, review),
    }
    stale = [p for p, text in outputs.items() if not p.exists() or p.read_text() != text]
    counts = f"{len(merged)} merged, {len(review)} in review"

    if not stale:
        print(f"OSS outputs already up to date ({counts}).")
        return 0
    names = ", ".join(str(p.relative_to(REPO_ROOT)) for p in stale)
    if args.check:
        print(f"OUT OF DATE: {names} (run without --check to update).", file=sys.stderr)
        return 1

    for path in stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(outputs[path])
    print(f"Updated {names} ({counts}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
