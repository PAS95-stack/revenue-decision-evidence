"""Phase 3: repeatable identity and output, and no budget move ever recommended."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence import engine  # noqa: E402
from revenue_evidence.engine import EvidenceEngine, InputContractError, write_outputs  # noqa: E402

DATA = ROOT / "data" / "synthetic"
ADS, CRM, REVENUE = DATA / "ads.csv", DATA / "crm.csv", DATA / "revenue.csv"


def read_all(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


class RepeatabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_r01_one_fils_changes_run_id_and_only_the_affected_figures(self):
        changed = self.root / "revenue.csv"
        text = REVENUE.read_text(encoding="utf-8")
        self.assertIn("TX-001,LEAD-001,16000.00", text)
        changed.write_text(text.replace("TX-001,LEAD-001,16000.00", "TX-001,LEAD-001,16000.01"), encoding="utf-8")
        base = EvidenceEngine().run(ADS, CRM, REVENUE)
        nudged = EvidenceEngine().run(ADS, CRM, changed)
        self.assertNotEqual(base.run_id, nudged.run_id)
        base_claims = {claim.evidence_id: claim.value for claim in base.claims}
        nudged_claims = {claim.evidence_id: claim.value for claim in nudged.claims}
        self.assertEqual(nudged_claims["EV-REV-001"], "56500.01")
        self.assertEqual(nudged_claims["EV-SPEND-001"], base_claims["EV-SPEND-001"])
        self.assertEqual(nudged_claims["EV-CONFLICT-001"], base_claims["EV-CONFLICT-001"])

    def test_r02_engine_version_is_part_of_run_id(self):
        base = EvidenceEngine().run(ADS, CRM, REVENUE)
        with mock.patch.object(engine, "ENGINE_VERSION", "0.0.0-test"):
            other = EvidenceEngine().run(ADS, CRM, REVENUE)
        self.assertNotEqual(base.run_id, other.run_id)
        self.assertEqual(other.engine_version, "0.0.0-test")

    def test_r03_ruleset_content_changes_run_id_without_a_version_bump(self):
        base = EvidenceEngine().run(ADS, CRM, REVENUE)
        with mock.patch.dict(engine.RULESET, {"coverage_threshold_percent": "75"}):
            other = EvidenceEngine().run(ADS, CRM, REVENUE)
        self.assertEqual(base.ruleset["version"], other.ruleset["version"])
        self.assertNotEqual(base.ruleset["fingerprint"], other.ruleset["fingerprint"])
        self.assertNotEqual(base.run_id, other.run_id)

    def test_r04_as_of_is_optional_validated_recorded_and_repeatable(self):
        undated = EvidenceEngine().run(ADS, CRM, REVENUE)
        self.assertIsNone(undated.as_of)
        first = EvidenceEngine().run(ADS, CRM, REVENUE, as_of="2026-09-15")
        second = EvidenceEngine().run(ADS, CRM, REVENUE, as_of="2026-09-15")
        self.assertEqual(first.as_of, "2026-09-15")
        self.assertNotEqual(undated.run_id, first.run_id)
        write_outputs(first, self.root / "first")
        write_outputs(second, self.root / "second")
        self.assertEqual(read_all(self.root / "first"), read_all(self.root / "second"))
        with self.assertRaises(InputContractError):
            EvidenceEngine().run(ADS, CRM, REVENUE, as_of="15/09/2026")

    def test_r05_pyproject_version_matches_engine_version(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], engine.ENGINE_VERSION)

    def test_r06_json_outputs_are_written_with_sorted_keys(self):
        write_outputs(EvidenceEngine().run(ADS, CRM, REVENUE), self.root / "out")
        for name in ("report.json", "evaluation_results.json"):
            with self.subTest(output=name):
                text = (self.root / "out" / name).read_text(encoding="utf-8")
                canonical = json.dumps(json.loads(text), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
                self.assertEqual(text, canonical)


class RecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clean = {
            "ads": self.root / "ads.csv",
            "crm": self.root / "crm.csv",
            "revenue": self.root / "revenue.csv",
        }
        self.clean["ads"].write_text(
            "date,campaign_id,channel,spend_aed\n2026-07-01,C1,Search,100\n2026-07-01,C2,Social,50\n",
            encoding="utf-8",
        )
        self.clean["crm"].write_text(
            "lead_id,campaign_id,created_at,status\nL1,C1,2026-07-02,won\nL2,C2,2026-07-03,won\n",
            encoding="utf-8",
        )
        self.clean["revenue"].write_text(
            "transaction_id,lead_id,value_aed,date\nT1,L1,300,2026-07-10\nT2,L2,100,2026-08-20\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def clean_report(self):
        return EvidenceEngine().run(self.clean["ads"], self.clean["crm"], self.clean["revenue"])

    def test_c01_no_output_recommends_a_budget_move_even_at_full_coverage(self):
        for label, report in (("synthetic", EvidenceEngine().run(ADS, CRM, REVENUE)), ("clean", self.clean_report())):
            with self.subTest(case=label):
                self.assertIn(report.recommendation["action"], {"request-more-evidence", "review-evidence"})
                self.assertEqual(
                    report.recommendation["decision"], "Do not reallocate budget from this evidence set alone."
                )
                target = self.root / f"out-{label}"
                write_outputs(report, target)
                for name, content in read_all(target).items():
                    self.assertIsNone(
                        re.search(r"reallocation toward|bounded-test|test at most", content.decode("utf-8")),
                        f"{name} proposes a budget move",
                    )

    def test_c02_clean_full_coverage_evidence_is_for_review_not_action(self):
        report = self.clean_report()
        self.assertEqual(report.summary["attribution_coverage_percent"], "100.0")
        self.assertEqual(report.recommendation["action"], "review-evidence")
        self.assertEqual(report.recommendation["evidence_grade"], "assumption-dependent")
        self.assertFalse(any("EV-COVER-001" in item for item in report.recommendation["findings"]))
        self.assertIn("controlled test", report.recommendation["questions"][-1])

    def test_c03_synthetic_findings_and_questions_are_specific(self):
        recommendation = EvidenceEngine().run(ADS, CRM, REVENUE).recommendation
        self.assertEqual(recommendation["action"], "request-more-evidence")
        self.assertEqual(recommendation["evidence_grade"], "unsupported")
        findings = " ".join(recommendation["findings"])
        for citation in ("[EV-COVER-001]", "[EV-UNMATCH-001]", "[EV-CONFLICT-001]", "[EV-WINEXCL-030]"):
            self.assertIn(citation, findings)
        # ads:8, crm:9 and revenue:9 rejected; ads:7 and revenue:8 duplicate; crm:3 and crm:8 conflict.
        self.assertIn("3 rejected, 2 duplicate and 2 conflicting source rows", findings)
        questions = " ".join(recommendation["questions"])
        for fragment in ("contradictory CRM records", "CRM export", "advertising export", "dated before"):
            self.assertIn(fragment, questions)

    def test_c04_threshold_is_published_as_a_planning_choice(self):
        report = EvidenceEngine().run(ADS, CRM, REVENUE)
        self.assertIn("planning choice", report.ruleset["coverage_threshold_basis"])
        write_outputs(report, self.root / "out")
        self.assertIn(report.ruleset["coverage_threshold_basis"], (self.root / "out" / "executive_brief.md").read_text(encoding="utf-8"))

    def test_c05_html_does_not_trust_source_labels_as_markup(self):
        self.clean["ads"].write_text(
            "date,campaign_id,channel,spend_aed\n2026-07-01,C1,<script>alert(1)</script>,100\n", encoding="utf-8"
        )
        report = self.clean_report()
        write_outputs(report, self.root / "out")
        page = (self.root / "out" / "report.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)


if __name__ == "__main__":
    unittest.main()
