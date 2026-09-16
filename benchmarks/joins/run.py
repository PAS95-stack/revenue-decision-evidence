#!/usr/bin/env python3
"""Measure join discovery against the benchmark's ground truth.

    python3 benchmarks/joins/run.py --data ~/benchmarks/joins

The development datasets (olist, tpch, chinook) and the held-out ones (northwind, sakila,
instacart) were fixed before any result was seen and are reported separately, so a later
change to the rules can be judged on data that did not shape it. The negative control runs
discovery over every file of every dataset at once: any join between files of two different
datasets is false by construction.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence.joins import MIN_DISTINCT, PROPOSE, Table, discover, lookup_verdict  # noqa: E402

DEVELOPMENT = ("olist", "tpch", "chinook")
HELD_OUT = ("northwind", "sakila", "instacart")


def _key(child: str, child_columns, parent: str, parent_columns) -> tuple:
    return (child, tuple(child_columns), parent, tuple(parent_columns))


def _share(values: set, keys: set) -> float:
    return len(values & keys) / len(values) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--low-cardinality", action="store_true",
                        help="measure the variant that also surfaces low-cardinality joins whose names agree")
    args = parser.parse_args()
    variant = "low-cardinality variant (not adopted)" if args.low_cardinality else "rules as specified"
    datasets = args.data / "datasets"
    cache: dict = {}
    results: dict = {"variant": variant, "rules": {"min_distinct": MIN_DISTINCT, "propose_containment": PROPOSE},
                     "datasets": {}}
    started = time.time()

    for name in DEVELOPMENT + HELD_OUT:
        folder = datasets / name
        tables = [Table(f"{name}/{path.name}", path) for path in sorted(folder.glob("*.csv"))]
        truth = json.loads((folder / "truth.json").read_text(encoding="utf-8"))
        joins = discover(tables, cache, low_cardinality=args.low_cardinality)
        profiles = {(p.table, p.column): p for table in tables for p in cache[table.name]}

        def qualified(item: dict) -> tuple:
            return _key(f"{name}/{item['child']}", item["child_columns"], f"{name}/{item['parent']}", item["parent_columns"])

        truth_keys = [qualified(item) for item in truth["joins"]]
        valid = set(truth_keys) | {qualified(item) for item in truth.get("also_valid", [])}
        for child, child_columns, parent, parent_columns in truth_keys:
            if len(child_columns) == 1:
                a, b = profiles.get((child, child_columns[0])), profiles.get((parent, parent_columns[0]))
                if a and b and a.unique and b.unique:
                    valid.add(_key(parent, parent_columns, child, child_columns))
        proposed = {_key(j.child, j.child_columns, j.parent, j.parent_columns): j for j in joins}

        reachable, misses, fan = [], [], {"cases": 0, "detected": 0}
        for key in truth_keys:
            child, child_columns, parent, parent_columns = key
            if len(child_columns) == 2:
                reachable.append(key)
            else:
                a, b = profiles[(child, child_columns[0])], profiles[(parent, parent_columns[0])]
                if len(a.distinct) >= MIN_DISTINCT and _share(a.distinct, b.distinct) >= PROPOSE:
                    reachable.append(key)
                if a.repeats:
                    fan["cases"] += 1
                    fan["detected"] += lookup_verdict(b, a) == "fan-out"
            if key not in proposed:
                a = profiles.get((child, child_columns[0]))
                b = profiles.get((parent, parent_columns[0]))
                if len(child_columns) == 2:
                    why = "two-column key not found"
                elif a.rows == a.blanks:
                    why = "the column has no values"
                elif not a.keylike:
                    why = f"not treated as a key ({a.kind}{', measure name' if a.measure else ''})"
                elif len(a.distinct) < MIN_DISTINCT:
                    why = f"only {len(a.distinct)} distinct values"
                elif _share(a.distinct, b.distinct) < PROPOSE:
                    why = f"only {_share(a.distinct, b.distinct):.0%} contained"
                else:
                    why = "rejected by a guard"
                misses.append(f"{child}.{'+'.join(child_columns)} -> {parent}: {why}")

        check = [key for key, join in proposed.items() if join.confidence == "check"]
        entry = {
            "truth": len(truth_keys), "reachable": len(reachable),
            "proposed_check": len(check), "correct_check": sum(1 for key in check if key in valid),
            "proposed_all": len(proposed), "correct_all": sum(1 for key in proposed if key in valid),
            "found_check": sum(1 for key in truth_keys if key in proposed and proposed[key].confidence == "check"),
            "found_all": sum(1 for key in truth_keys if key in proposed),
            "found_reachable": sum(1 for key in reachable if key in proposed),
            "fan_out": fan,
            "false_joins": [f"{j.child}.{'+'.join(j.child_columns)} -> {j.parent}.{'+'.join(j.parent_columns)} "
                            f"[{j.confidence}]" for key, j in proposed.items() if key not in valid],
            "misses": misses,
        }
        results["datasets"][name] = entry

    everything = [Table(f"{name}/{path.name}", path)
                  for name in DEVELOPMENT + HELD_OUT for path in sorted((datasets / name).glob("*.csv"))]
    crossing = [j for j in discover(everything, cache, low_cardinality=args.low_cardinality)
                if j.child.split("/")[0] != j.parent.split("/")[0]]
    results["negative_control"] = {
        "files": len(everything),
        "false_joins_check": sum(1 for j in crossing if j.confidence == "check"),
        "false_joins_all": len(crossing),
        "examples": [f"{j.child}.{'+'.join(j.child_columns)} -> {j.parent}.{'+'.join(j.parent_columns)} "
                     f"[{j.confidence}]" for j in crossing[:8]],
    }
    results["seconds"] = round(time.time() - started, 1)

    def total(names: tuple[str, ...]) -> dict:
        keys = ("truth", "reachable", "proposed_check", "correct_check", "proposed_all", "correct_all",
                "found_check", "found_all", "found_reachable")
        summed = {key: sum(results["datasets"][n][key] for n in names) for key in keys}
        summed["fan_cases"] = sum(results["datasets"][n]["fan_out"]["cases"] for n in names)
        summed["fan_detected"] = sum(results["datasets"][n]["fan_out"]["detected"] for n in names)
        return summed

    def ratio(a: int, b: int) -> str:
        return f"{a / b:.1%} ({a}/{b})" if b else "n/a (0/0)"

    print(f"== {variant} ==")
    print(f"{'':12} {'check precision':>20} {'all precision':>18} {'recall check':>18} {'recall all':>18} {'recall reachable':>20} {'fan-out':>10}")
    rows = [(n, results["datasets"][n]) for n in DEVELOPMENT + HELD_OUT]
    rows += [("DEVELOPMENT", total(DEVELOPMENT)), ("HELD OUT", total(HELD_OUT)), ("ALL", total(DEVELOPMENT + HELD_OUT))]
    for name, e in rows:
        fan_cases = e["fan_out"]["cases"] if "fan_out" in e else e["fan_cases"]
        fan_found = e["fan_out"]["detected"] if "fan_out" in e else e["fan_detected"]
        print(f"{name:12} {ratio(e['correct_check'], e['proposed_check']):>20} {ratio(e['correct_all'], e['proposed_all']):>18} "
              f"{ratio(e['found_check'], e['truth']):>18} {ratio(e['found_all'], e['truth']):>18} "
              f"{ratio(e['found_reachable'], e['reachable']):>20} {fan_found}/{fan_cases:<6}")
    results["totals"] = {"development": total(DEVELOPMENT), "held_out": total(HELD_OUT), "all": total(DEVELOPMENT + HELD_OUT)}
    control = results["negative_control"]
    print(f"\nnegative control over {control['files']} files: {control['false_joins_check']} false joins at check, "
          f"{control['false_joins_all']} at any confidence")
    for example in control["examples"]:
        print(f"   {example}")
    for name, e in results["datasets"].items():
        if e["false_joins"]:
            print(f"\n{name} false joins:")
            for item in e["false_joins"]:
                print(f"   {item}")
    for name, e in results["datasets"].items():
        if e["misses"]:
            print(f"\n{name} missed:")
            for item in e["misses"]:
                print(f"   {item}")
    target = args.data / ("results-low-cardinality.json" if args.low_cardinality else "results.json")
    target.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n{results['seconds']}s; results written to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
