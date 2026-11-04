"""Reference-data store: one ingest per snapshot, indexed reads afterwards."""
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from rfq_agent.infrastructure import db
from rfq_agent.application.cases import COST_TABLE, MATERIAL, PRICE_TABLE
from rfq_agent.ingestion.sources import load_cost_table, load_price_table

ROOT = Path(__file__).parents[1]


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = ROOT / "runtime" / "tests" / f"db{uuid4().hex[:12]}"
        cls.tmp.mkdir(parents=True)
        cls.conn = db.connect(cls.tmp / "rfq.db")
        cls.first = db.sync(cls.conn, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, True)

    def test_snapshot_matches_the_workbooks(self):
        price = load_price_table(ROOT / MATERIAL / PRICE_TABLE)
        cost = load_cost_table(ROOT / MATERIAL / COST_TABLE)
        self.assertEqual(self.first["price_rows"], len(price))
        self.assertEqual(self.first["cost_rows"], len(cost))
        self.assertTrue(self.first["snapshot_id"].startswith("snap-"))
        self.assertEqual(db.counts(self.conn, self.first["snapshot_id"]),
                         {"price_rows": len(price), "cost_rows": len(cost)})

    def test_second_sync_does_no_work(self):
        again = db.sync(self.conn, ROOT)
        self.assertFalse(again["changed"])
        self.assertEqual(again["snapshot_id"], self.first["snapshot_id"])

    def test_force_resync_is_idempotent(self):
        forced = db.sync(self.conn, ROOT, force=True)
        self.assertEqual(forced["snapshot_id"], self.first["snapshot_id"])
        # rows are replaced, not duplicated
        self.assertEqual(db.counts(self.conn, forced["snapshot_id"])["price_rows"],
                         self.first["price_rows"])

    def test_indexed_read_returns_only_requested_parts(self):
        price, cost = db.rows_for(self.conn, self.first["snapshot_id"], {"PN-0001"})
        self.assertTrue(price and cost)
        for row in price:
            self.assertEqual(row["Part Number"], "PN-0001")
        for row in cost:
            self.assertEqual(row["Part Number"], "PN-0001")
        self.assertLess(len(price), self.first["price_rows"])

    def test_unknown_part_reads_nothing(self):
        price, cost = db.rows_for(self.conn, self.first["snapshot_id"], {"NO-SUCH-PART"})
        self.assertEqual((price, cost), ([], []))

    def test_dates_survive_the_round_trip(self):
        _, cost = db.rows_for(self.conn, self.first["snapshot_id"], {"PN-0001"})
        original = [row for row in load_cost_table(ROOT / MATERIAL / COST_TABLE)
                    if row.get("Part Number") == "PN-0001"]
        self.assertTrue(original)
        self.assertEqual(type(cost[0].get("成本更新日期")), type(original[0].get("成本更新日期")))

    def test_row_payload_keeps_the_engine_contract(self):
        price, _ = db.rows_for(self.conn, self.first["snapshot_id"], {"PN-0001"})
        for field in ("Part Number", "最小起订量", "价格状态", "_source_row"):
            self.assertIn(field, price[0])


if __name__ == "__main__":
    unittest.main()