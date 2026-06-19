#!/usr/bin/env python3
"""Regenerate the Open Source Contributions section of the profile README.

Queries GitHub for every PR the profile owner has opened against EXTERNAL
repositories (merged + still-open), renders a summary-count badge line plus a
collapsible table, and replaces whatever sits between the OSS markers in
README.md. Closed-unmerged PRs are intentionally excluded — the public profile
shows positive signal only, and a closed PR simply drops off the table.

One exception: a PR a maintainer squash-merges under a fresh commit SHA stays
CLOSED (not MERGED) on GitHub, so `gh search prs --merged` can't see it and it
would silently vanish despite being a real merge. Flag such a PR `"merged": true`
in oss_contributions_overrides.json to re-include it as merged.

Data source: `gh search prs` (two queries: --merged and --state open), plus
`gh pr view` for any forced-merged override. Curated one-line summaries come
from oss_contributions_overrides.json; any PR without an override falls back to
a cleaned-up PR title.

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

# Ordered display sections. compress=True collapses still-in-review PRs to a
# single "+N in review" line; merged PRs are always listed individually.
# compress=False lists every PR as its own table row. Every section is currently
# compress=False so each PR — merged or in-review — is a discrete row, which
# keeps the table machine-reconcilable; the compress mechanism is retained for
# any future campaign that grows large enough to warrant collapsing.
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


def section_lines(
    heading: str, compress: bool, group: dict[str, list], overrides: dict
) -> list[str]:
    merged, review = group["merged"], group["review"]
    if not merged and not review:
        return []

    total = len(merged) + len(review)
    suffix = f"{len(merged)} merged" if merged else "in review"
    lines = [f"#### {heading} ({total} · {suffix})", ""]

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


def render(merged: list[dict], review: list[dict], overrides: dict) -> str:
    groups = group_by_category(merged, review, overrides)
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
    ]
    rendered = [
        sec
        for key, heading, compress in CATEGORIES
        if (sec := section_lines(heading, compress, groups[key], overrides))
    ]
    for i, sec in enumerate(rendered):
        if i > 0:  # space each heading section from the previous one
            lines += ["<br/>", ""]
        lines += sec
    lines += ["</details>", "", MARKER_END]
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
