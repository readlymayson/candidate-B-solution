import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.report import load_inputs, allocate, write_reports, money


DATA = Path(__file__).resolve().parents[1] / "data"


class ReportTests(unittest.TestCase):
    def test_case_data_as_of_november_10(self):
        contracts, works, payments = load_inputs(DATA, date(2026, 11, 10))
        totals = allocate(contracts, works, payments)

        # D-1: refunded P-2 and queued P-4 are not paid; P-1 + P-3 are paid.
        self.assertEqual(totals["paid"]["D-1"], Decimal("315000.00"))
        self.assertEqual(totals["queued"]["D-1"], Decimal("150000.00"))
        self.assertEqual(totals["unallocated"]["D-1"], Decimal("0.00"))
        self.assertEqual(works["W-11"].paid, Decimal("100000.00"))
        self.assertEqual(works["W-12"].paid, Decimal("100000.00"))
        self.assertEqual(works["W-14"].paid, Decimal("100000.00"))
        self.assertEqual(works["W-15"].paid, Decimal("15000.00"))
        self.assertEqual(works["W-15"].remaining, Decimal("85000.00"))
        self.assertEqual(works["W-16"].remaining, Decimal("100000.00"))
        self.assertEqual(works["W-13"].status_as_of, "cancelled")
        self.assertEqual(works["W-13"].remaining, Decimal("0.00"))

        # W-23 is done in the current export, but was not yet completed on Nov 10.
        self.assertEqual(works["W-23"].status_as_of, "planned")
        self.assertEqual(totals["paid"]["D-2"], Decimal("75000.00"))
        self.assertEqual(works["W-23"].paid, Decimal("25000.00"))
        self.assertEqual(works["W-23"].remaining, Decimal("15000.00"))

    def test_money_rejects_fraction_smaller_than_kopeck(self):
        with self.assertRaises(ValueError):
            money("1.001", "test.amount")
        self.assertEqual(money("1.01", "test.amount"), Decimal("1.01"))

    def test_payment_after_report_date_is_excluded(self):
        contracts, works, payments = load_inputs(DATA, date(2026, 11, 10))
        totals = allocate(contracts, works, payments)
        self.assertEqual(totals["paid"]["D-2"], Decimal("75000.00"))
        self.assertNotEqual(totals["paid"]["D-2"], Decimal("95000.00"))

    def test_payment_paid_after_as_of_is_queued_as_of(self):
        contracts, works, payments = load_inputs(DATA, date(2026, 11, 10))
        payments.append({
            "payment_id": "P-LATE", "contract_id": "D-2", "work_id": "",
            "purpose": "Late settlement", "amount": Decimal("1200.00"),
            "status": "paid", "created_at": date(2026, 11, 5),
            "paid_at": date(2026, 11, 12), "refunded_at": None,
            "net_paid": Decimal("0.00"), "queued": Decimal("1200.00"),
            "refunded_by_as_of": False,
        })
        totals = allocate(contracts, works, payments)
        self.assertEqual(totals["paid"]["D-2"], Decimal("75000.00"))
        self.assertEqual(totals["queued"]["D-2"], Decimal("1200.00"))

    def test_reports_are_written(self):
        contracts, works, payments = load_inputs(DATA, date(2026, 11, 10))
        totals = allocate(contracts, works, payments)
        with tempfile.TemporaryDirectory() as tmp:
            wp, cp = write_reports(Path(tmp), contracts, works, totals, date(2026, 11, 10))
            self.assertTrue(wp.exists())
            self.assertTrue(cp.exists())
            with cp.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            d1 = next(r for r in rows if r["contract_id"] == "D-1")
            self.assertEqual(d1["work_prices_total"], "600000.00")
            self.assertEqual(d1["cancelled_work_prices_total"], "100000.00")
            self.assertEqual(d1["active_work_prices_total"], "500000.00")
            self.assertEqual(d1["work_remaining_total"], "185000.00")
            self.assertEqual(d1["remaining_contract"], "285000.00")

            d2 = next(r for r in rows if r["contract_id"] == "D-2")
            self.assertEqual(d2["paid_net"], "75000.00")
            self.assertEqual(d2["remaining_contract"], "15000.00")
            self.assertEqual(d2["cancelled_work_prices_total"], "0.00")


if __name__ == "__main__":
    unittest.main()
