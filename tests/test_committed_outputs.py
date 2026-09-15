"""The committed synthetic outputs must be exactly what the current code produces."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence.engine import OUTPUT_NAMES, EvidenceEngine, write_outputs  # noqa: E402

DATA = ROOT / "data" / "synthetic"
COMMITTED = ROOT / "outputs" / "synthetic-case"


class CommittedOutputTests(unittest.TestCase):
    def test_o01_committed_outputs_match_a_fresh_run_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as temp:
            report = EvidenceEngine().run(DATA / "ads.csv", DATA / "crm.csv", DATA / "revenue.csv")
            evaluation = write_outputs(report, temp)
            self.assertEqual(evaluation["status"], "PASS", evaluation["failures"])
            self.assertEqual(sorted(path.name for path in COMMITTED.iterdir()), sorted(OUTPUT_NAMES))
            for name in OUTPUT_NAMES:
                with self.subTest(output=name):
                    self.assertEqual(
                        (COMMITTED / name).read_bytes(),
                        (Path(temp) / name).read_bytes(),
                        f"{name} is stale; regenerate it with the command in README.md",
                    )


if __name__ == "__main__":
    unittest.main()
