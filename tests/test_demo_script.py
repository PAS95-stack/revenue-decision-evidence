import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence.engine import EvidenceEngine  # noqa: E402
from revenue_evidence.narrative import validate_narrative  # noqa: E402

DATA = ROOT / "data" / "synthetic"


@unittest.skipIf(shutil.which("bash") is None, "bash is not available")
class DemoScriptTests(unittest.TestCase):
    def test_w04_walkthrough_script_runs_and_every_step_gets_its_expected_result(self):
        # --no-tests stops the script re-running this suite from inside it.
        env = {**os.environ, "PYTHON": sys.executable}
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "demo.sh"), "--no-pause", "--no-tests"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertIn("Demo complete.", result.stdout)
        self.assertIn("REJECTED: Accepted revenue is AED 65,500 [EV-REV-001].", result.stdout)


class KnownGapTests(unittest.TestCase):
    def test_k01_mislabelled_metric_with_matching_number_still_passes(self):
        # Documented in docs/limitations.md. If the gate learns to bind metric
        # wording, this test fails: update the limitation and invert the test.
        report = EvidenceEngine().run(DATA / "ads.csv", DATA / "crm.csv", DATA / "revenue.csv")
        valid, _ = validate_narrative("Accepted revenue is AED 15,000 [EV-SPEND-001].", report)
        self.assertTrue(valid)


if __name__ == "__main__":
    unittest.main()
