"""Phase 5: planted engine defects must be caught before publication.

Each test re-introduces a realistic bug into the engine at runtime, including
the two that P1 originally found. If the independent checker were vacuous, or
shared the engine's code, these would publish. Two further planted defects
live in test_reconstruction (v07 dropped lineage row, v08 brief missing
evidence).
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
import tempfile
import unittest
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence import engine  # noqa: E402
from revenue_evidence.engine import EvidenceEngine, write_outputs  # noqa: E402

DATA = ROOT / "data" / "synthetic"
REAL_RESOLVE = engine._resolve_identity


def first_row_wins(rows, key, signature, dispositions, source, duplicate_reason, conflict_reason, amount_field=None):
    """The pre-P1 behaviour: the first row with an identifier wins, whatever the others say."""
    kept, seen = [], set()
    for row in sorted(rows, key=lambda item: item["source_row"]):
        amount = row[amount_field] if amount_field else None
        status = engine.DUPLICATE if row[key] in seen else engine.ACCEPTED
        engine._dispose(dispositions, source, row["source_row"], row[key], status, "", amount)
        if status == engine.ACCEPTED:
            seen.add(row[key])
            kept.append(row)
    return kept, {}


def keep_duplicates(rows, key, signature, dispositions, source, duplicate_reason, conflict_reason, amount_field=None):
    """Identical copies are all counted instead of once."""
    kept, conflicts = REAL_RESOLVE(rows, key, signature, dispositions, source, duplicate_reason, conflict_reason, amount_field)
    for row in rows:
        ref = f"{source}:{row['source_row']}"
        if dispositions[ref].status == engine.DUPLICATE:
            dispositions[ref] = dataclasses.replace(dispositions[ref], status=engine.ACCEPTED, reason="")
            kept.append(row)
    kept.sort(key=lambda item: item["source_row"])
    return kept, conflicts


def lenient_money(value, field):
    """The pre-P1 parser: anything Decimal accepts, including 1e3."""
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{field} is not a number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} must be a non-negative finite number")
    return parsed.quantize(engine.MONEY, rounding=engine.ROUND_HALF_UP)


class PlantedDefectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sources = {name: self.root / f"{name}.csv" for name in ("ads", "crm", "revenue")}
        for name, path in self.sources.items():
            shutil.copyfile(DATA / f"{name}.csv", path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def replace_in(self, name: str, before: str, after: str) -> None:
        text = self.sources[name].read_text(encoding="utf-8")
        self.assertEqual(text.count(before), 1)
        self.sources[name].write_text(text.replace(before, after), encoding="utf-8")

    def assert_blocked(self, *fragments: str) -> list[str]:
        output = self.root / "out"
        report = EvidenceEngine().run(self.sources["ads"], self.sources["crm"], self.sources["revenue"])
        evaluation = write_outputs(report, output)
        self.assertEqual(evaluation["status"], "FAIL", "a planted engine defect was published")
        self.assertFalse((output / "executive_brief.md").exists())
        joined = " ".join(evaluation["failures"])
        for fragment in fragments:
            self.assertIn(fragment, joined)
        return evaluation["failures"]

    def test_x01_control_the_unpatched_engine_publishes(self):
        report = EvidenceEngine().run(self.sources["ads"], self.sources["crm"], self.sources["revenue"])
        self.assertEqual(write_outputs(report, self.root / "out")["status"], "PASS")

    def test_x02_first_row_wins_conflict_resolution_is_blocked(self):
        with mock.patch.object(engine, "_resolve_identity", first_row_wins):
            self.assert_blocked("crm:3", "crm:8")

    def test_x03_counting_identical_duplicates_is_blocked(self):
        with mock.patch.object(engine, "_resolve_identity", keep_duplicates):
            self.assert_blocked("revenue:8")

    def test_x04_half_even_money_rounding_is_blocked(self):
        self.replace_in("ads", "2026-07-01,CMP-EMAIL-01,Email,1000.00", "2026-07-01,CMP-EMAIL-01,Email,1000.005")
        with mock.patch.object(engine, "ROUND_HALF_UP", ROUND_HALF_EVEN):
            self.assert_blocked("ads:4")

    def test_x05_lenient_amount_parsing_is_blocked_with_the_cause_first(self):
        self.replace_in("ads", "2026-07-01,CMP-EMAIL-01,Email,1000.00", "2026-07-01,CMP-EMAIL-01,Email,1e3")
        with mock.patch.object(engine, "_parse_money", lenient_money):
            failures = self.assert_blocked("ads:4")
        self.assertTrue(failures[0].startswith("row_dispositions: status disagrees"), failures[0])
        self.assertLessEqual(len(failures), 12, f"cascade not summarised: {len(failures)} lines")
        self.assertTrue(any("follow from the row status disagreements above" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
