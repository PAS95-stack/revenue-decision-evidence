#!/usr/bin/env python3
"""Convert one sheet of an .xlsx workbook to CSV, keeping the sheet's row numbers.

Clients send spreadsheets. This makes a CSV an engagement can read, and writes a
manifest recording the workbook's SHA-256, the sheet, the rows written and how many
cells were read as dates, so the conversion is itself evidence rather than an
untracked step.

    python3 scripts/xlsx_to_csv.py Orders.xlsx --out inputs/orders.csv
    python3 scripts/xlsx_to_csv.py Orders.xlsx --sheet "Invoices" --out inputs/invoices.csv

Standard library only. Row N of the CSV is row N of the sheet: empty sheet rows are
written as empty CSV lines, so a row reference such as revenue/orders.csv:14 points at
spreadsheet row 14. A cell shown as a date is written as YYYY-MM-DD, or
YYYY-MM-DD HH:MM:SS when it carries a time; every other cell is written as the
workbook stores it, so nothing is reformatted silently.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

NAMESPACE = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RELATIONS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
# Number formats Excel ships that mean a date or a time.
BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57}
DATE_CHARACTERS = re.compile(r"[dmyhs]", re.IGNORECASE)
CELL_REFERENCE = re.compile(r"([A-Z]+)([0-9]+)")


class WorkbookError(Exception):
    """The workbook cannot be read well enough to convert."""


def _column_index(reference: str) -> int:
    letters = CELL_REFERENCE.fullmatch(reference.upper())
    if letters is None:
        raise WorkbookError(f"cell reference {reference!r} is not understood")
    index = 0
    for character in letters.group(1):
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.itertext()) for node in root.findall(f"{NAMESPACE}si")]


def _date_styles(archive: zipfile.ZipFile) -> set[int]:
    """Style indexes whose number format displays a date or a time."""
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = ElementTree.fromstring(archive.read("xl/styles.xml"))
    custom = {
        int(node.attrib["numFmtId"]): node.attrib.get("formatCode", "")
        for node in root.iter(f"{NAMESPACE}numFmt")
    }
    styles = set()
    formats = root.find(f"{NAMESPACE}cellXfs")
    for index, node in enumerate(list(formats) if formats is not None else []):
        identifier = int(node.attrib.get("numFmtId", 0))
        code = custom.get(identifier)
        if identifier in BUILTIN_DATE_FORMATS or (code and DATE_CHARACTERS.search(code.replace("\\", ""))):
            styles.add(index)
    return styles


def _sheets(archive: zipfile.ZipFile) -> dict[str, str]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relations = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {node.attrib["Id"]: node.attrib["Target"] for node in relations}
    sheets = {}
    for node in workbook.iter(f"{NAMESPACE}sheet"):
        target = targets[node.attrib[f"{RELATIONS}id"]].lstrip("/")
        sheets[node.attrib["name"]] = target if target.startswith("xl/") else f"xl/{target}"
    if not sheets:
        raise WorkbookError("the workbook has no sheets")
    return sheets


def _epoch(archive: zipfile.ZipFile) -> date:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    properties = workbook.find(f"{NAMESPACE}workbookPr")
    if properties is not None and properties.attrib.get("date1904") in ("1", "true"):
        return date(1904, 1, 1)
    # Excel's 1900 system counts an imaginary 29 February 1900, so day 1 is 31 December 1899.
    return date(1899, 12, 30)


def _as_date(serial: str, epoch: date) -> str:
    number = float(serial)
    days = int(number)
    seconds = round((number - days) * 86400)
    moment = epoch + timedelta(days=days)
    if seconds:
        hours, remainder = divmod(seconds, 3600)
        minutes, second = divmod(remainder, 60)
        return f"{moment.isoformat()} {hours:02d}:{minutes:02d}:{second:02d}"
    return moment.isoformat()


def convert(workbook: Path, target: Path, sheet: str | None = None) -> dict:
    try:
        archive = zipfile.ZipFile(workbook)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WorkbookError(f"{workbook.name} is not a readable .xlsx workbook") from exc
    with archive:
        sheets = _sheets(archive)
        name = sheet or next(iter(sheets))
        if name not in sheets:
            raise WorkbookError(f"the workbook has no sheet named {name!r}; it has {', '.join(sheets)}")
        strings = _shared_strings(archive)
        date_styles = _date_styles(archive)
        epoch = _epoch(archive)
        root = ElementTree.fromstring(archive.read(sheets[name]))

        rows: dict[int, dict[int, str]] = {}
        widest = 0
        dates = 0
        for row in root.iter(f"{NAMESPACE}row"):
            number = int(row.attrib["r"])
            cells: dict[int, str] = {}
            for cell in row.iter(f"{NAMESPACE}c"):
                column = _column_index(cell.attrib["r"])
                kind = cell.attrib.get("t", "n")
                value_node = cell.find(f"{NAMESPACE}v")
                if kind == "inlineStr":
                    inline = cell.find(f"{NAMESPACE}is")
                    value = "".join((inline if inline is not None else cell).itertext())
                elif value_node is None or value_node.text is None:
                    value = ""
                elif kind == "s":
                    value = strings[int(value_node.text)]
                elif kind == "b":
                    value = "TRUE" if value_node.text == "1" else "FALSE"
                elif kind in ("str", "e"):
                    value = value_node.text
                else:
                    style = int(cell.attrib.get("s", -1))
                    if style in date_styles and value_node.text not in (None, ""):
                        value = _as_date(value_node.text, epoch)
                        dates += 1
                    else:
                        value = value_node.text
                cells[column] = value
                widest = max(widest, column + 1)
            if cells:
                rows[number] = cells

    target.parent.mkdir(parents=True, exist_ok=True)
    last = max(rows) if rows else 0
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for number in range(1, last + 1):
            cells = rows.get(number, {})
            writer.writerow([cells.get(column, "") for column in range(widest)] if cells else [])
    return {
        "workbook": workbook.name,
        "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
        "sheet": name,
        "sheets_available": list(sheets),
        "csv": target.name,
        "csv_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "rows_written": last,
        "columns": widest,
        "cells_read_as_dates": dates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--sheet", help="sheet name; the first sheet by default")
    parser.add_argument("--out", type=Path, required=True, help="CSV to write")
    parser.add_argument("--manifest", type=Path, help="where to write the conversion record (default: beside the CSV)")
    args = parser.parse_args(argv)
    try:
        manifest = convert(args.workbook, args.out, args.sheet)
    except WorkbookError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    path = args.manifest or args.out.with_suffix(args.out.suffix + ".conversion.json")
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
