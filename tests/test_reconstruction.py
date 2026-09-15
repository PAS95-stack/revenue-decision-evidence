"""Phase 4: independent reconstruction gates publication."""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence import engine  # noqa: E402
from revenue_evidence.engine import OUTPUT_NAMES, EvidenceEngine, write_outputs  # noqa: E402

RECONSTRUCT = ROOT / "src" / "revenue_evidence" / "reconstruct.py"
DATA = ROOT / "data" / "synthetic"
STDLIB_ALLOWED = {"__future__", "argparse", "csv", "hashlib", "io", "json", "re", "sys", "datetime", "decimal", "pathlib"}


class ReconstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Copies, so tests may alter an export without touching the repository.
        self.sources = {name: self.root / f"{name}.csv" for name in ("ads", "crm", "revenue")}
        for name, path in self.sources.items():
            shutil.copyfile(DATA / f"{name}.csv", path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_engine(self):
        return EvidenceEngine().run(self.sources["ads"], self.sources["crm"], self.sources["revenue"])

    def write_garbage(self) -> None:
        self.sources["ads"].write_text("date,campaign_id,channel,spend_aed\nx,,,-1\n", encoding="utf-8")
        self.sources["crm"].write_text("lead_id,campaign_id,created_at,status\n,,,\n", encoding="utf-8")
        self.sources["revenue"].write_text("transaction_id,lead_id,value_aed,date\n,,,\n", encoding="utf-8")

    def standalone(self, output: Path) -> subprocess.CompletedProcess:
        """Run the checker in isolated mode from an unrelated directory: the package cannot be imported."""
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        return subprocess.run(
            [
                sys.executable, "-I", str(RECONSTRUCT),
                "--ads", str(self.sources["ads"]), "--crm", str(self.sources["crm"]),
                "--revenue", str(self.sources["revenue"]), "--output", str(output),
            ],
            cwd=self.root, env=env, capture_output=True, text=True, check=False,
        )

    def cli(self, output: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [
                sys.executable, "-m", "revenue_evidence.cli",
                "--ads", str(self.sources["ads"]), "--crm", str(self.sources["crm"]),
                "--revenue", str(self.sources["revenue"]), "--output", str(output),
            ],
            cwd=self.root, env=env, capture_output=True, text=True, check=False,
        )

    def test_v01_checker_imports_only_the_standard_library(self):
        tree = ast.parse(RECONSTRUCT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "relative import in the independent checker")
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, STDLIB_ALLOWED, f"unexpected imports: {sorted(imported - STDLIB_ALLOWED)}")

    def test_v02_standalone_checker_passes_published_synthetic_outputs(self):
        output = self.root / "out"
        evaluation = write_outputs(self.run_engine(), output)
        self.assertEqual(evaluation["status"], "PASS", evaluation["failures"])
        result = self.standalone(output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "PASS")

    def test_v03_passing_evaluation_lists_checks_and_hashes_every_published_file(self):
        output = self.root / "out"
        evaluation = write_outputs(self.run_engine(), output)
        self.assertEqual(evaluation["failures"], [])
        self.assertIn("claim_lineage", evaluation["checks"])
        published = sorted(path.name for path in output.iterdir())
        self.assertEqual(published, sorted(OUTPUT_NAMES), "staging must not be left behind")
        import hashlib

        for name, digest in evaluation["published_outputs"].items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest, name)
        self.assertEqual(set(evaluation["published_outputs"]), set(OUTPUT_NAMES) - {"evaluation_results.json"})

    def test_v04_unusable_input_publishes_only_a_failing_evaluation(self):
        self.write_garbage()
        output = self.root / "out"
        evaluation = write_outputs(self.run_engine(), output)
        self.assertEqual(evaluation["status"], "FAIL")
        self.assertEqual(sorted(path.name for path in output.iterdir()), ["evaluation_results.json"])
        failures = " ".join(evaluation["failures"])
        for name in ("ads", "crm", "revenue"):
            self.assertIn(f"the {name} export has no usable rows", failures)
        self.assertEqual(evaluation["published_outputs"], {})

    def test_v05_a_failed_run_removes_the_previous_brief(self):
        output = self.root / "out"
        self.assertEqual(write_outputs(self.run_engine(), output)["status"], "PASS")
        self.assertTrue((output / "executive_brief.md").exists())
        self.write_garbage()
        self.assertEqual(write_outputs(self.run_engine(), output)["status"], "FAIL")
        self.assertFalse((output / "executive_brief.md").exists())
        self.assertFalse((output / "report.json").exists())

    def test_v06_tampered_published_outputs_are_detected(self):
        output = self.root / "published"
        self.assertEqual(write_outputs(self.run_engine(), output)["status"], "PASS")
        pristine = {path.name: path.read_bytes() for path in output.iterdir()}

        def restore():
            for name, content in pristine.items():
                (output / name).write_bytes(content)

        # Bytes, not text: csv writes \r\n and text mode would silently normalise it,
        # turning a precise one-row tamper into a no-op.
        cases = {
            "false revenue value": (
                "report.json",
                b'"value": "56500.00"',
                b'"value": "65500.00"',
                "EV-REV-001",
            ),
            "row dropped from lineage": (
                "lineage.csv",
                b"EV-UNMATCH-001,unattributed_or_invalid_revenue,reconciled,summand,revenue:10\r\n",
                b"",
                "EV-UNMATCH-001",
            ),
            "status relabelled": (
                "row_dispositions.csv",
                b"revenue,10,TX-008,accepted-unattributed",
                b"revenue,10,TX-008,rejected",
                "revenue:10",
            ),
        }
        for label, (name, before, after, expected_fragment) in cases.items():
            with self.subTest(case=label):
                restore()
                original = (output / name).read_bytes()
                self.assertEqual(original.count(before), 1, "the tamper target must occur exactly once")
                (output / name).write_bytes(original.replace(before, after, 1))
                result = self.standalone(output)
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected_fragment, result.stdout)

        restore()
        with self.sources["revenue"].open("a", encoding="utf-8") as handle:
            handle.write("TX-900,LEAD-001,1.00,2026-07-21\n")
        result = self.standalone(output)
        self.assertEqual(result.returncode, 1)
        self.assertIn("the revenue export changed", result.stdout)

    def test_v07_an_engine_lineage_defect_blocks_publication(self):
        report = self.run_engine()
        report.claims = [
            dataclasses.replace(claim, lineage=claim.lineage[:-1]) if claim.evidence_id == "EV-UNMATCH-001" else claim
            for claim in report.claims
        ]
        output = self.root / "out"
        evaluation = write_outputs(report, output)
        self.assertEqual(evaluation["status"], "FAIL")
        self.assertTrue(any("EV-UNMATCH-001" in failure for failure in evaluation["failures"]))
        self.assertFalse((output / "executive_brief.md").exists())

    def test_v08_a_brief_that_omits_evidence_blocks_publication(self):
        with mock.patch.object(engine, "_brief", lambda report: "# Brief\n\nNo figures.\n"):
            evaluation = write_outputs(self.run_engine(), self.root / "out")
        self.assertEqual(evaluation["status"], "FAIL")
        self.assertTrue(any(failure.startswith("executive_brief:") for failure in evaluation["failures"]))

    def test_v09_cli_exits_non_zero_and_publishes_no_brief_on_failure(self):
        output = self.root / "cli"
        passed = self.cli(output)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(json.loads(passed.stdout)["status"], "PASS")
        self.write_garbage()
        failed = self.cli(output)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("no usable rows", failed.stderr)
        self.assertEqual(sorted(path.name for path in output.iterdir()), ["evaluation_results.json"])

    def test_v10_cli_schema_error_exits_non_zero_and_clears_stale_outputs(self):
        output = self.root / "cli"
        self.assertEqual(self.cli(output).returncode, 0)
        self.sources["ads"].write_text("date,campaign_id,channel\n2026-07-01,C1,Search\n", encoding="utf-8")
        result = self.cli(output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing required columns", result.stderr)
        self.assertEqual(list(output.iterdir()), [])

    def test_v11_a_report_without_source_paths_cannot_be_published(self):
        report = self.run_engine()
        report.source_paths = {}
        evaluation = write_outputs(report, self.root / "out")
        self.assertEqual(evaluation["status"], "FAIL")
        self.assertIn("source CSV paths were not supplied", " ".join(evaluation["failures"]))


if __name__ == "__main__":
    unittest.main()
