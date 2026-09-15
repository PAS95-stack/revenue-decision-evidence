#!/usr/bin/env bash
# Five-minute walkthrough. Every step states the result it expects and the script
# stops if that result does not happen, so the demo cannot quietly show a failure
# as a success. Nothing in the repository is modified; all runs go to a temp folder.
#
#   scripts/demo.sh              pause before each step (for recording)
#   scripts/demo.sh --no-pause   run straight through
#   scripts/demo.sh --no-tests   skip the unit-test step
set -u

PAUSE=1
TESTS=1
for arg in "$@"; do
  case "$arg" in
    --no-pause) PAUSE=0 ;;
    --no-tests) TESTS=0 ;;
    *) echo "unknown option: $arg" >&2; exit 64 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
DATA="$ROOT/data/synthetic"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$ROOT" || exit 1

step() {
  printf '\n\033[1m== %s\033[0m\n' "$1"
  if [ "$PAUSE" = 1 ]; then read -r -p "(press Enter)" _; fi
}

# expect CODE COMMAND... : run the command and stop the demo if its exit code differs.
expect() {
  local want="$1"; shift
  "$@"
  local got=$?
  if [ "$got" != "$want" ]; then
    printf '\nDEMO STOPPED: expected exit code %s, got %s\n' "$want" "$got" >&2
    exit 1
  fi
  printf '\033[2m(exit code %s, as expected)\033[0m\n' "$got"
}

engine() {  # engine ADS CRM REVENUE OUTPUT
  PYTHONPATH="$ROOT/src" "$PY" -m revenue_evidence.cli --ads "$1" --crm "$2" --revenue "$3" --output "$4"
}

checker() {  # checker ADS CRM REVENUE OUTPUT
  "$PY" -I src/revenue_evidence/reconstruct.py --ads "$1" --crm "$2" --revenue "$3" --output "$4" \
    | "$PY" -c 'import json,sys; r=json.load(sys.stdin); print("checker:", r["status"]); [print("  ", f) for f in r["failures"][:3]]; sys.exit(r["status"] != "PASS")'
}

if [ "$TESTS" = 1 ]; then
  step "1. The test suite"
  expect 0 "$PY" -m unittest discover -s tests
fi

step "2. Run from the original CSVs"
expect 0 engine "$DATA/ads.csv" "$DATA/crm.csv" "$DATA/revenue.csv" "$WORK/run"
echo "Comparing with the committed outputs byte for byte:"
expect 0 diff -r outputs/synthetic-case "$WORK/run"

step "3. Every input row gets one status"
expect 0 "$PY" scripts/demo_tools.py statuses "$WORK/run"

step "4. Rebuild published figures from their raw source rows"
expect 0 "$PY" scripts/demo_tools.py trace "$WORK/run" EV-REV-001
expect 0 "$PY" scripts/demo_tools.py trace "$WORK/run" EV-CONFLICT-001
echo "Independent checker (standard library only, cannot import the engine):"
expect 0 checker "$DATA/ads.csv" "$DATA/crm.csv" "$DATA/revenue.csv" "$WORK/run"

step "5. Make the narrative say something false"
T="scripts/demo_tools.py"
expect 0 "$PY" "$T" narrative "Accepted revenue is AED 56,500 [EV-REV-001]."
expect 1 "$PY" "$T" narrative "Accepted revenue is AED 65,500 [EV-REV-001]."
expect 1 "$PY" "$T" narrative "Accepted revenue is AED 15,000 [EV-REV-001]."
expect 1 "$PY" "$T" narrative "Accepted revenue is AED 15,000 [EV-SPEND-001]."
expect 1 "$PY" "$T" narrative "Accepted revenue is AED 56,500 [EV-REV-999]."
expect 1 "$PY" "$T" narrative "Accepted revenue is recorded [EV-REV-001]. Paid Search returns 9.9x ROAS."
expect 1 "$PY" "$T" narrative "Paid Search drove the revenue [EV-ATTR-001]."

step "6. Edit a published report after the fact"
cp -R "$WORK/run" "$WORK/tampered"
sed 's/"56500\.00"/"65500.00"/g' "$WORK/run/report.json" > "$WORK/tampered/report.json"
echo "report.json now says accepted revenue is 65500.00."
expect 1 checker "$DATA/ads.csv" "$DATA/crm.csv" "$DATA/revenue.csv" "$WORK/tampered"

step "7. Swap the two contradictory CRM rows for LEAD-002"
mkdir -p "$WORK/swapped-input"
"$PY" - "$DATA/crm.csv" "$WORK/swapped-input/crm.csv" <<'SWAP'
import sys
lines = open(sys.argv[1], encoding="utf-8", newline="").read().splitlines(keepends=True)
a = next(i for i, l in enumerate(lines) if l.startswith("LEAD-002,CMP-SEARCH-01"))
b = next(i for i, l in enumerate(lines) if l.startswith("LEAD-002,CMP-SOCIAL-01"))
lines[a], lines[b] = lines[b], lines[a]
open(sys.argv[2], "w", encoding="utf-8", newline="").write("".join(lines))
print(f"Swapped physical lines {a + 1} and {b + 1}.")
SWAP
expect 0 engine "$DATA/ads.csv" "$WORK/swapped-input/crm.csv" "$DATA/revenue.csv" "$WORK/swapped"
expect 0 "$PY" scripts/demo_tools.py compare "$WORK/run" "$WORK/swapped"

step "8. Missing evidence: a revenue export with no rows"
head -1 "$DATA/revenue.csv" > "$WORK/empty-revenue.csv"
expect 2 engine "$DATA/ads.csv" "$DATA/crm.csv" "$WORK/empty-revenue.csv" "$WORK/empty"
echo "Files written:"; ls "$WORK/empty"

step "9. What this does not show"
echo "See docs/limitations.md. In short: synthetic data only; attribution is not causation;"
echo "the narrative gate uses word lists and has only seen a mocked model; the engine and"
echo "checker share one author's reading of the data contract."
printf '\nDemo complete.\n'
