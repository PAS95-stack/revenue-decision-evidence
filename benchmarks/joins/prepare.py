#!/usr/bin/env python3
"""Build the join-discovery benchmark: public multi-table datasets with known relationships.

    python3 benchmarks/joins/prepare.py --out ~/benchmarks/joins --tpch ~/benchmarks/tpch-sf0.05

Nothing here is part of the product or its tests. Datasets are downloaded to the folder
given, never into this repository, and every file's SHA-256 is recorded. Ground truth
comes from each schema's declared foreign keys where the source declares them; where it
does not, from the published specification or schema, and each relationship records
which. TPC-H data is generated beforehand with the official dbgen (for example DuckDB's
tpch extension) and passed with --tpch, since generating it needs a tool this
standard-library repository does not carry.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import urllib.request
from pathlib import Path

GITHUB = "https://github.com"
SQLITE_SETS = {
    "chinook": {"chinook.sqlite": f"{GITHUB}/lerocha/chinook-database/raw/HEAD/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite"},
    "northwind": {"northwind.db": f"{GITHUB}/jpwhite3/northwind-SQLite3/raw/HEAD/dist/northwind.db"},
    "sakila": {
        "sakila-schema.sql": f"{GITHUB}/jOOQ/sakila/raw/HEAD/sqlite-sakila-db/sqlite-sakila-schema.sql",
        "sakila-data.sql": f"{GITHUB}/jOOQ/sakila/raw/HEAD/sqlite-sakila-db/sqlite-sakila-insert-data.sql",
    },
}
OLIST = "https://raw.githubusercontent.com/Ganesh7699/Brazilian-E-Commerce-OList/main/"
INSTACART = "https://raw.githubusercontent.com/Frisellarm/InstaCart_MBA/HEAD/Data/"
CSV_SETS = {
    "olist": {
        "source": "published Olist schema (Kaggle dataset documentation)",
        "files": {f"{name}.csv": f"{OLIST}{name}.csv" for name in (
            "olist_orders_dataset", "olist_order_items_dataset", "olist_order_payments_dataset",
            "olist_order_reviews_dataset", "olist_customers_dataset", "olist_sellers_dataset",
            "olist_products_dataset", "olist_marketing_qualified_leads_dataset", "olist_closed_deals_dataset")},
        "joins": [
            ("olist_order_items_dataset.csv", ["order_id"], "olist_orders_dataset.csv", ["order_id"]),
            ("olist_order_items_dataset.csv", ["product_id"], "olist_products_dataset.csv", ["product_id"]),
            ("olist_order_items_dataset.csv", ["seller_id"], "olist_sellers_dataset.csv", ["seller_id"]),
            ("olist_orders_dataset.csv", ["customer_id"], "olist_customers_dataset.csv", ["customer_id"]),
            ("olist_order_payments_dataset.csv", ["order_id"], "olist_orders_dataset.csv", ["order_id"]),
            ("olist_order_reviews_dataset.csv", ["order_id"], "olist_orders_dataset.csv", ["order_id"]),
            ("olist_closed_deals_dataset.csv", ["mql_id"], "olist_marketing_qualified_leads_dataset.csv", ["mql_id"]),
            ("olist_closed_deals_dataset.csv", ["seller_id"], "olist_sellers_dataset.csv", ["seller_id"]),
        ],
        "excluded": [],
    },
    "instacart": {
        "source": "published Instacart schema; the public mirror used carries four of the seven files",
        "files": {name: f"{INSTACART}{name}" for name in (
            "aisles.csv", "departments.csv", "products.csv", "order_products__train.csv")},
        "joins": [
            ("products.csv", ["aisle_id"], "aisles.csv", ["aisle_id"]),
            ("products.csv", ["department_id"], "departments.csv", ["department_id"]),
            ("order_products__train.csv", ["product_id"], "products.csv", ["product_id"]),
        ],
        "excluded": [{"child": "order_products__train.csv", "child_columns": ["order_id"], "parent": "orders.csv",
                      "why": "orders.csv is not in the public mirror"}],
    },
}
TPCH = {
    "source": "TPC-H specification, clause 1.4 (declared primary and foreign keys)",
    "joins": [
        ("nation.csv", ["n_regionkey"], "region.csv", ["r_regionkey"]),
        ("supplier.csv", ["s_nationkey"], "nation.csv", ["n_nationkey"]),
        ("customer.csv", ["c_nationkey"], "nation.csv", ["n_nationkey"]),
        ("partsupp.csv", ["ps_partkey"], "part.csv", ["p_partkey"]),
        ("partsupp.csv", ["ps_suppkey"], "supplier.csv", ["s_suppkey"]),
        ("orders.csv", ["o_custkey"], "customer.csv", ["c_custkey"]),
        ("lineitem.csv", ["l_orderkey"], "orders.csv", ["o_orderkey"]),
        ("lineitem.csv", ["l_partkey", "l_suppkey"], "partsupp.csv", ["ps_partkey", "ps_suppkey"]),
    ],
    # Correct relationships the specification states only through the composite key.
    "also_valid": [
        ("lineitem.csv", ["l_partkey"], "part.csv", ["p_partkey"]),
        ("lineitem.csv", ["l_suppkey"], "supplier.csv", ["s_suppkey"]),
    ],
}


def _download(url: str, target: Path) -> None:
    if target.exists() and target.stat().st_size:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as response, target.open("wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _join(child: str, child_columns: list[str], parent: str, parent_columns: list[str]) -> dict:
    return {"child": child, "child_columns": child_columns, "parent": parent, "parent_columns": parent_columns}


def _export_sqlite(database: Path, out: Path) -> dict:
    """Every table as a CSV export, and the foreign keys the schema declares."""
    out.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    tables = [row[0] for row in connection.execute(
        "select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by name")]
    by_folded = {table.casefold(): table for table in tables}
    counts = {}
    for table in tables:
        cursor = connection.execute(f'select * from "{table}"')
        columns = [item[0] for item in cursor.description]
        count = 0
        with (out / f"{table}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in cursor:
                writer.writerow(["" if value is None or isinstance(value, bytes) else value for value in row])
                count += 1
        counts[table] = count
    joins, excluded = [], []
    for table in tables:
        groups: dict[int, list] = {}
        for fk in connection.execute(f'pragma foreign_key_list("{table}")'):
            groups.setdefault(fk[0], []).append(fk)
        for parts in groups.values():
            parts.sort(key=lambda part: part[1])
            parent = by_folded.get(parts[0][2].casefold())
            child_columns = [part[3] for part in parts]
            parent_columns = [part[4] for part in parts]
            if parent and any(column is None for column in parent_columns):
                keys = [row[1] for row in sorted(connection.execute(f'pragma table_info("{parent}")'), key=lambda r: r[5]) if row[5]]
                parent_columns = keys[: len(child_columns)]
            entry = _join(f"{table}.csv", child_columns, f"{parent or parts[0][2]}.csv", parent_columns)
            if parent is None:
                excluded.append({**entry, "why": "the referenced table is not in the database"})
            elif parent == table:
                excluded.append({**entry, "why": "a reference within one table is not a join between two exports"})
            elif not counts[table] or not counts[parent]:
                excluded.append({**entry, "why": "one of the two tables has no rows"})
            else:
                joins.append(entry)
    connection.close()
    return {"joins": joins, "excluded": excluded, "rows": counts}


def _column_values(path: Path, columns: list[str]) -> list[tuple[str, ...]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [tuple((row.get(column) or "").strip() for column in columns) for row in csv.DictReader(handle)]


def _describe(folder: Path, join: dict) -> str:
    child = [key for key in _column_values(folder / join["child"], join["child_columns"]) if all(key)]
    parent = [key for key in _column_values(folder / join["parent"], join["parent_columns"]) if all(key)]
    distinct, parent_keys = set(child), set(parent)
    contained = len(distinct & parent_keys) / len(distinct) if distinct else 0.0
    unique = len(parent_keys) == len(parent)
    return (f"{join['child']}.{'+'.join(join['child_columns'])} -> {join['parent']}.{'+'.join(join['parent_columns'])}"
            f"  distinct {len(distinct)}, contained {contained:.1%}, parent key {'unique' if unique else 'REPEATS'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tpch", type=Path, help="folder of TPC-H CSVs generated with dbgen")
    args = parser.parse_args()
    raw, datasets = args.out / "raw", args.out / "datasets"
    manifest: dict[str, dict] = {}

    for name, files in SQLITE_SETS.items():
        for filename, url in files.items():
            _download(url, raw / filename)
            manifest[f"raw/{filename}"] = {"url": url, "sha256": _sha256(raw / filename)}
    sakila = raw / "sakila.db"
    if not sakila.exists():
        connection = sqlite3.connect(sakila)
        connection.executescript((raw / "sakila-schema.sql").read_text(encoding="utf-8", errors="replace"))
        connection.executescript((raw / "sakila-data.sql").read_text(encoding="utf-8", errors="replace"))
        connection.commit()
        connection.close()
    for name, database in (("chinook", raw / "chinook.sqlite"), ("northwind", raw / "northwind.db"), ("sakila", sakila)):
        found = _export_sqlite(database, datasets / name)
        truth = {"source": "declared foreign keys (sqlite PRAGMA foreign_key_list)", "joins": found["joins"],
                 "also_valid": [], "excluded": found["excluded"]}
        (datasets / name / "truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")

    for name, spec in CSV_SETS.items():
        for filename, url in spec["files"].items():
            _download(url, datasets / name / filename)
            manifest[f"datasets/{name}/{filename}"] = {"url": url, "sha256": _sha256(datasets / name / filename)}
        truth = {"source": spec["source"], "joins": [_join(*join) for join in spec["joins"]], "also_valid": [],
                 "excluded": spec["excluded"]}
        (datasets / name / "truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")

    if args.tpch:
        target = datasets / "tpch"
        target.mkdir(parents=True, exist_ok=True)
        for source in sorted(args.tpch.glob("*.csv")):
            (target / source.name).write_bytes(source.read_bytes())
            manifest[f"datasets/tpch/{source.name}"] = {"url": "generated with dbgen", "sha256": _sha256(target / source.name)}
        truth = {"source": TPCH["source"], "joins": [_join(*join) for join in TPCH["joins"]],
                 "also_valid": [_join(*join) for join in TPCH["also_valid"]], "excluded": []}
        (target / "truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")

    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    for folder in sorted(path for path in datasets.iterdir() if (path / "truth.json").exists()):
        truth = json.loads((folder / "truth.json").read_text(encoding="utf-8"))
        print(f"== {folder.name}: {len(truth['joins'])} relationships ({truth['source']}), "
              f"{len(truth['excluded'])} excluded")
        for join in truth["joins"]:
            print(f"   {_describe(folder, join)}")
        for item in truth["excluded"]:
            print(f"   excluded {item['child']}.{'+'.join(item['child_columns'])} -> {item['parent']}: {item['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
