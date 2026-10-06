"""Tests for oss_contributions.py. Run: python3 -m unittest discover -s scripts"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oss_contributions as oc  # noqa: E402


def pr(repo: str, number: int, title: str = "Some change", created: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/{repo}/pull/{number}",
        "repository": {"nameWithOwner": repo},
        "createdAt": created,
    }


def completed(payload) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


class CleanTitleTest(unittest.TestCase):
    def test_strips_conventional_prefix_and_capitalizes(self):
        self.assertEqual(oc.clean_title("fix(chart): add a thing"), "Add a thing")

    def test_strips_stacked_bracket_and_type(self):
        self.assertEqual(oc.clean_title("[mesheryctl] feat: add export flag"), "Add export flag")

    def test_keeps_title_without_prefix(self):
        self.assertEqual(oc.clean_title("Already Clean"), "Already Clean")

    def test_falls_back_when_nothing_is_left(self):
        self.assertEqual(oc.clean_title("[scope]"), "[scope]")


class CategoryTest(unittest.TestCase):
    def test_explicit_override_wins(self):
        p = pr("getmoto/moto", 1)
        self.assertEqual(oc.category_of(p, "anything", {"getmoto/moto#1": {"category": "moto-aws"}}), "moto-aws")

    def test_inferred_campaigns(self):
        self.assertEqual(oc.category_of(pr("a/b", 1), "Add HTTPRoute support", {}), "httproute")
        ngf = pr("nginx/nginx-gateway-fabric", 2)
        self.assertEqual(oc.category_of(ngf, "Remove gocyclo nolint", {}), "ngf-gocyclo")
        self.assertEqual(oc.category_of(pr("a/b", 3), "Read a .tool-versions file", {}), "action-version-file")
        self.assertEqual(oc.category_of(pr("a/b", 4), "Fix a race", {}), oc.DEFAULT_CATEGORY)

    def test_unknown_category_falls_back_to_default_group(self):
        p = pr("a/b", 5)
        groups = oc.group_by_category([p], [], {"a/b#5": {"category": "nope"}})
        self.assertEqual(len(groups[oc.DEFAULT_CATEGORY]["merged"]), 1)


class HelpersTest(unittest.TestCase):
    def test_external_drops_own_repos(self):
        prs = [pr("somaz94/x", 1), pr("somaz-devops/y", 2), pr("apache/airflow", 3)]
        self.assertEqual([p["number"] for p in oc.external(prs)], [3])

    def test_summary_prefers_override(self):
        p = pr("a/b", 1, title="feat: raw title")
        self.assertEqual(oc.summary_of(p, {"a/b#1": {"summary": "Curated"}}), "Curated")
        self.assertEqual(oc.summary_of(p, {}), "Raw title")

    def test_table_row_uses_project_override(self):
        row = oc.table_row(pr("a/b", 7), "Sum", "✅ Merged", {"a/b#7": {"project": "A/B"}})
        self.assertEqual(row, "| A/B | [#7](https://github.com/a/b/pull/7) | Sum | ✅ Merged |")

    def test_anchor_matches_github_slugs(self):
        # Observed on github.com/somaz94 for the pre-split headings.
        cases = {
            "Gateway API HTTPRoute support · Helm charts (35 · 25 merged)":
                "gateway-api-httproute-support--helm-charts-35--25-merged",
            "GitHub Action `version-file` inputs (14 · 9 merged)":
                "github-action-version-file-inputs-14--9-merged",
            "nginx-gateway-fabric cyclomatic-complexity refactors · #5253 (11 · 11 merged)":
                "nginx-gateway-fabric-cyclomatic-complexity-refactors--5253-11--11-merged",
        }
        for heading, slug in cases.items():
            self.assertEqual(oc.anchor(heading), slug)

    def test_dynamic_badge_reads_stats_json(self):
        url = oc.dynamic_badge("Merged", "merged", oc.MERGED_COLOR)
        self.assertTrue(url.startswith("https://img.shields.io/badge/dynamic/json?"))
        self.assertIn("query=%24.merged", url)
        self.assertIn("data%2Foss-stats.json", url)


class SectionLinesTest(unittest.TestCase):
    def test_lists_merged_then_review(self):
        group = {"merged": [(pr("a/b", 1), "M")], "review": [(pr("a/b", 2), "R")]}
        lines = oc.section_lines("Heading", False, group, {})
        self.assertEqual(lines[0], "## Heading")
        self.assertIn("✅ Merged", lines[4])
        self.assertIn("🔵 Review", lines[5])

    def test_compress_collapses_review_with_preview_cap(self):
        review = [(pr("a/b", n), "R") for n in range(oc.IN_REVIEW_PREVIEW + 2)]
        lines = oc.section_lines("Heading", True, {"merged": [], "review": review}, {})
        self.assertNotIn("| Project | PR | Contribution | Status |", lines)
        self.assertTrue(lines[-2].startswith(f"_In review ({len(review)}): "))
        self.assertIn(", …_", lines[-2])


class RenderTest(unittest.TestCase):
    def test_catalog_summary_counts_and_spacing(self):
        merged = [pr("a/b", 1, title="Add HTTPRoute support"), pr("c/d", 2)]
        review = [pr("c/d", 3)]
        text = oc.render_catalog(merged, review, {})
        self.assertIn("Merged-2-", text)
        self.assertIn("Review-1-", text)
        self.assertIn("| [Standalone contributions](#standalone-contributions) | 1 | 1 | 2 |", text)
        self.assertNotIn("moto AWS API mocks", text)  # empty areas are omitted
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("## "):
                self.assertEqual(lines[i - 2], "<br/>")
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))

    def test_stats_json(self):
        self.assertEqual(
            json.loads(oc.render_stats([pr("a/b", 1)], [pr("a/b", 2), pr("a/b", 3)])),
            {"merged": 1, "review": 2, "total": 3},
        )

    def test_readme_block_is_constant(self):
        block = oc.render_readme_block()
        self.assertTrue(block.startswith(oc.MARKER_START) and block.endswith(oc.MARKER_END))
        self.assertIn(oc.CATALOG_URL, block)
        self.assertNotRegex(block, r"badge/Merged-\d")


class GhTest(unittest.TestCase):
    def test_gh_search_parses_results(self):
        with mock.patch.object(oc.subprocess, "run", return_value=completed([pr("a/b", 1)])) as run:
            self.assertEqual(len(oc.gh_search("--merged")), 1)
        self.assertIn(str(oc.SEARCH_LIMIT), run.call_args.args[0])

    def test_gh_search_fails_when_truncated(self):
        full = [pr("a/b", n) for n in range(oc.SEARCH_LIMIT)]
        with mock.patch.object(oc.subprocess, "run", return_value=completed(full)):
            with self.assertRaises(RuntimeError):
                oc.gh_search("--merged")

    def test_forced_merged_fetches_flagged_entries_only(self):
        overrides = {"a/b#9": {"merged": True}, "a/b#10": {"summary": "x"}}
        view = {"number": 9, "title": "t", "url": "u", "createdAt": "c"}
        with mock.patch.object(oc.subprocess, "run", return_value=completed(view)) as run:
            records = oc.forced_merged(overrides)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(records[0]["repository"], {"nameWithOwner": "a/b"})


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.readme = root / "README.md"
        self.readme.write_text(f"intro\n{oc.MARKER_START}\nold\n{oc.MARKER_END}\noutro\n")
        overrides = root / "overrides.json"
        overrides.write_text(json.dumps({"a/b#2": {"merged": True}}))
        for name, value in {
            "REPO_ROOT": root,
            "README": self.readme,
            "CATALOG": root / "OSS_CONTRIBUTIONS.md",
            "STATS": root / "data" / "oss-stats.json",
            "OVERRIDES": overrides,
        }.items():
            patcher = mock.patch.object(oc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        searches = {"--merged": [pr("a/b", 1)], "--state": [pr("a/b", 2), pr("a/b", 3)]}
        patcher = mock.patch.object(oc, "gh_search", side_effect=lambda *a: list(searches[a[0]]))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(oc, "forced_merged", return_value=[pr("a/b", 2), pr("a/b", 1)])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = root

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = oc.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_then_write_then_idempotent(self):
        code, _, err = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("OSS_CONTRIBUTIONS.md", err)

        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("2 merged, 1 in review", out)
        stats = json.loads((self.root / "data" / "oss-stats.json").read_text())
        self.assertEqual(stats, {"merged": 2, "review": 1, "total": 3})
        readme = self.readme.read_text()
        self.assertTrue(readme.startswith("intro\n") and readme.endswith("outro\n"))
        self.assertNotIn("\nold\n", readme)

        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 0)
        self.assertIn("already up to date", out)

    def test_missing_markers(self):
        self.readme.write_text("no markers here\n")
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()
