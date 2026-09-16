"""Join discovery: the guards that keep a wrong join from silently changing revenue.

A missed join is cheap, because a person adds it. A wrong join is expensive, because it
changes revenue without anyone seeing. Each case pins one guard with the smallest export
that would get past it.
"""

from __future__ import annotations

import csv
import itertools
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_evidence.joins import Table, discover, lookup_verdict, profile_table  # noqa: E402


class JoinDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def table(self, name: str, header: list[str], rows: list[list[str]]) -> Table:
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return Table(name, path)

    def joins(self, *tables: Table, **options) -> dict:
        return {(j.child, j.child_columns, j.parent, j.parent_columns): j for j in discover(list(tables), **options)}

    def orders(self) -> Table:
        return self.table("orders.csv", ["order_id", "placed"], [[f"INV-{n:04d}", "2026-07-01"] for n in range(1, 61)])

    def test_j01_a_contained_unique_key_whose_names_agree_is_a_check_join(self):
        lines = self.table("lines.csv", ["order_id", "amount"], [[f"INV-{n % 50 + 1:04d}", "10.00"] for n in range(200)])
        found = self.joins(self.orders(), lines)
        join = found[("lines.csv", ("order_id",), "orders.csv", ("order_id",))]
        self.assertEqual(join.confidence, "check")
        self.assertEqual(join.containment, 1.0)
        self.assertTrue(any("never repeats" in reason for reason in join.reasons))
        self.assertNotIn(("orders.csv", ("order_id",), "lines.csv", ("order_id",)), found,
                         "a key that repeats is never a parent")

    def test_j02_names_that_differ_are_never_accepted_without_a_person(self):
        lines = self.table("lines.csv", ["invoice_ref", "amount"], [[f"INV-{n % 50 + 1:04d}", "10.00"] for n in range(200)])
        join = self.joins(self.orders(), lines)[("lines.csv", ("invoice_ref",), "orders.csv", ("order_id",))]
        self.assertEqual(join.confidence, "needs you")

    def test_j03_fewer_than_thirty_distinct_values_are_not_proposed(self):
        lines = self.table("lines.csv", ["order_id"], [[f"INV-{n % 20 + 1:04d}"] for n in range(200)])
        self.assertEqual(self.joins(self.orders(), lines), {}, "20 values could match by chance")
        variant = self.joins(self.orders(), lines, low_cardinality=True)
        self.assertEqual(variant[("lines.csv", ("order_id",), "orders.csv", ("order_id",))].confidence, "needs you")

    def test_j04_a_file_that_opens_with_rows_of_one_is_read_as_integers(self):
        rows = [["1"] for _ in range(2500)] + [[str(n)] for n in range(2, 60)]
        profile = profile_table(self.table("tracks.csv", ["playlist_id"], rows))[0]
        self.assertEqual(profile.kind, "integer", "a sorted key is not a flag")
        self.assertEqual(len(profile.distinct), 59)
        self.assertEqual(profile_table(self.table("flags.csv", ["reordered"], [["true"], ["false"]]))[0].kind, "boolean")

    def test_j05_two_auto_increment_sequences_are_not_a_join_unless_named_alike(self):
        actors = self.table("actors.csv", ["actor_id"], [[str(n)] for n in range(1, 101)])
        addresses = self.table("addresses.csv", ["address_id"], [[str(n)] for n in range(1, 151)])
        self.assertEqual(self.joins(actors, addresses), {})
        details = self.table("actor_details.csv", ["actor_id", "biography"], [[str(n), "an actor"] for n in range(1, 101)])
        self.assertTrue(self.joins(actors, details), "one record split across two files keeps its name")

    def test_j06_small_integers_sitting_at_the_start_of_a_range_are_not_a_join(self):
        items = self.table("items.csv", ["item_id"], [[str(n)] for n in range(1, 1001)])
        sales = self.table("sales.csv", ["item_id", "qty"], [[str(n % 40 + 1), "1"] for n in range(400)])
        self.assertEqual(self.joins(items, sales), {})

    def test_j07_a_repeating_key_fans_rows_out_and_is_never_a_lookup(self):
        orders = profile_table(self.table("orders.csv", ["order_id"], [[f"O{n}"] for n in range(50)]))[0]
        lines = profile_table(self.table("lines.csv", ["order_id"], [[f"O{n % 50}"] for n in range(200)]))[0]
        self.assertEqual(lookup_verdict(orders, lines), "fan-out")
        self.assertEqual(lookup_verdict(lines, orders), "lookup")

    def test_j08_blank_keys_match_nothing_and_a_key_with_a_blank_is_no_key(self):
        orders = self.table("orders.csv", ["customer_id"], [[f"C{n}"] for n in range(40)] + [[""] for _ in range(30)])
        with_blank = self.table("customers.csv", ["customer_id"], [[f"C{n}"] for n in range(60)] + [[""]])
        self.assertEqual(self.joins(with_blank, orders), {})
        clean = self.table("clients.csv", ["customer_id"], [[f"C{n}"] for n in range(60)])
        join = self.joins(clean, orders)[("orders.csv", ("customer_id",), "clients.csv", ("customer_id",))]
        self.assertEqual(join.containment, 1.0, "blank cells in the child are not counted against the join")
        self.assertNotIn("", profile_table(orders)[0].distinct)

    def test_j09_values_that_match_only_after_normalising_need_a_person(self):
        accounts = self.table("accounts.csv", ["account_id"], [[str(n)] for n in range(1, 61)])
        invoices = self.table("invoices.csv", ["account_id", "total"], [[f"{n % 40 + 1:05d}", "5.00"] for n in range(100)])
        join = self.joins(accounts, invoices)[("invoices.csv", ("account_id",), "accounts.csv", ("account_id",))]
        self.assertEqual(join.confidence, "needs you")
        self.assertTrue(any("normalising" in reason for reason in join.reasons))

    def test_j10_single_words_are_not_codes(self):
        words = ["".join(pair) for pair in itertools.product("abcdefg", repeat=2)][:40]
        staff = self.table("staff.csv", ["last_name"], [[word] for word in words])
        people = self.table("people.csv", ["last_name"], [[word] for word in words * 2])
        self.assertEqual(self.joins(staff, people), {}, "shared surnames are not a key")

    def test_j11_a_two_column_key_is_found_and_left_to_a_person(self):
        lines = self.table("order_lines.csv", ["order_id", "line_no", "amount"],
                           [[f"O{order}", str(line), "9.50"] for order in range(40) for line in range(1, 4)])
        shipments = self.table("line_shipments.csv", ["order_id", "line_no", "carrier"],
                               [[f"O{order}", str(line), "DHL"] for order in range(40) for line in range(1, 4)])
        join = self.joins(lines, shipments)[
            ("line_shipments.csv", ("order_id", "line_no"), "order_lines.csv", ("order_id", "line_no"))]
        self.assertEqual(join.confidence, "needs you")

    def test_j12_a_near_constant_column_does_not_make_a_two_column_key(self):
        playlist = self.table("playlist_track.csv", ["playlist_id", "track_id"],
                              [["1", str(track)] for track in range(1, 101)] + [["2", str(track)] for track in range(1, 51)])
        tracks = self.table("tracks.csv", ["track_id", "media_type_id", "title"],
                            [[str(track), str(track % 3 + 1), f"Song {track}"] for track in range(1, 101)])
        found = self.joins(playlist, tracks)
        self.assertFalse([key for key in found if len(key[1]) == 2], "three media types cannot carry a key")
        self.assertEqual(found[("playlist_track.csv", ("track_id",), "tracks.csv", ("track_id",))].confidence, "check")


if __name__ == "__main__":
    unittest.main()
