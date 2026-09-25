import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.helm_dependencies import build_locked, repository_alias, selected_charts


class HelmDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = "https://charts.example.test/reviewed"
        self.entry = {"name": "fixture", "version": "1.2.3", "repository": self.repo}
        self.chart = self.write_chart("chart")
        self.calls = []
        self.existing = []

    def write_chart(self, name):
        directory = self.root / name
        directory.mkdir()
        (directory / "Chart.yaml").write_text(
            yaml.safe_dump({"apiVersion": "v2", "dependencies": [self.entry]})
        )
        (directory / "Chart.lock").write_text(
            yaml.safe_dump({"dependencies": [self.entry], "digest": "sha256:" + "a" * 64})
        )
        return directory

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["helm", "repo", "list"]:
            if not self.existing:
                return subprocess.CompletedProcess(args, 1, "", "Error: no repositories to show")
            return subprocess.CompletedProcess(args, 0, json.dumps(self.existing), "")
        return subprocess.CompletedProcess(args, 0)

    def test_fresh_run_registers_each_repo_once_then_builds_only_locked_versions(self):
        other = self.write_chart("other")
        build_locked([self.chart, other, self.chart], self.runner)
        self.assertEqual(
            self.calls,
            [
                ["helm", "repo", "list", "--output", "json"],
                ["helm", "repo", "add", repository_alias(self.repo), self.repo],
                ["helm", "dependency", "build", str(self.chart), "--skip-refresh"],
                ["helm", "dependency", "build", str(other), "--skip-refresh"],
            ],
        )

    def test_existing_url_reuses_alias_without_touching_foreign_repositories(self):
        self.existing = [
            {"name": "team-chart", "url": self.repo},
            {"name": "private", "url": "https://foreign.example.test"},
        ]
        build_locked([self.chart], self.runner)
        self.assertEqual(self.calls[1], ["helm", "repo", "update", "team-chart"])
        self.assertFalse(any("private" in call for call in self.calls))
        self.assertFalse(any("--force-update" in call for call in self.calls))

    def test_foreign_alias_collision_fails_before_mutation(self):
        self.existing = [{"name": repository_alias(self.repo), "url": "https://foreign.test"}]
        with self.assertRaisesRegex(ValueError, "collision"):
            build_locked([self.chart], self.runner)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_or_mismatched_pins_fail_before_helm(self):
        original = (self.chart / "Chart.yaml").read_text()
        for changes in (
            {"repository": "http://charts.example.test"},
            {"repository": "https://user:password@charts.example.test"},
            {"repository": "https://charts.example.test?token=secret"},
            {"repository": "@team"},
            {"version": "^1.2.3"},
            {"version": "1.2.4"},
            {"name": "different"},
        ):
            with self.subTest(changes=changes):
                entry = copy.deepcopy(self.entry)
                entry.update(changes)
                (self.chart / "Chart.yaml").write_text(yaml.safe_dump({"dependencies": [entry]}))
                with self.assertRaises(ValueError):
                    build_locked([self.chart], self.runner)
                self.assertEqual(self.calls, [])
        (self.chart / "Chart.yaml").write_text(original)
        other = self.write_chart("missing-lock")
        (other / "Chart.lock").unlink()
        with self.assertRaisesRegex(ValueError, "Missing required chart file"):
            build_locked([self.chart, other], self.runner)
        self.assertEqual(self.calls, [])

    def test_unexpected_helm_errors_or_lock_mutation_fail(self):
        def failed_list(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "broken configuration")

        with self.assertRaisesRegex(ValueError, "Unable to read"):
            build_locked([self.chart], failed_list)

        def mutation(args, **kwargs):
            response = self.runner(args, **kwargs)
            if args[:3] == ["helm", "dependency", "build"]:
                (self.chart / "Chart.lock").write_text("unexpected")
            return response

        with self.assertRaisesRegex(ValueError, "unexpected mutation"):
            build_locked([self.chart], mutation)

    def test_committed_charts_have_consistent_pinned_https_locks(self):
        root = Path(__file__).resolve().parents[1]
        selected = [
            "charts/operators",
            "charts/platform",
            "charts/flink-operator",
            "integrations/cilium",
        ]
        charts, repositories = selected_charts([root / chart for chart in selected])
        self.assertEqual(len(charts), 4)
        self.assertIn("https://grafana.github.io/helm-charts", repositories)


if __name__ == "__main__":
    unittest.main()
