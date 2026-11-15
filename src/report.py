#!/usr/bin/env python3
"""Build an as-of-date contractor payment allocation report."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value: str, field: str) -> Decimal:
    try:
        raw = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"Invalid money value in {field}: {value!r}")
    if not raw.is_finite():
        raise ValueError(f"Non-finite money value in {field}: {value!r}")
    try:
        result = raw.quantize(CENT)
    except InvalidOperation:
        raise ValueError(f"Money value is outside supported precision in {field}: {value!r}")
    if raw != result:
        raise ValueError(f"Money value has fractions smaller than a kopeck in {field}: {value!r}")
    return result


def day(value: str, field: str, required: bool = False) -> date | None:
    value = (value or "").strip()
    if not value:
        if required:
            raise ValueError(f"Missing date in {field}")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid date in {field}: {value!r}")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    source_status: str
    completed_at: date | None
    cancelled_at: date | None
    status_as_of: str
    direct_paid: Decimal = ZERO
    allocated_contract_paid: Decimal = ZERO

    @property
    def paid(self) -> Decimal:
        return self.direct_paid + self.allocated_contract_paid

    @property
    def remaining(self) -> Decimal:
        if self.status_as_of == "cancelled":
            return ZERO
        return max(ZERO, self.price - self.paid)

    @property
    def overpaid(self) -> Decimal:
        if self.status_as_of == "cancelled":
            return self.paid
        return max(ZERO, self.paid - self.price)


def load_inputs(data_dir: Path, as_of: date):
    contracts_rows = read_csv(data_dir / "contracts.csv")
    works_rows = read_csv(data_dir / "works.csv")
    payments_rows = read_csv(data_dir / "payments.csv")

    contracts: dict[str, dict] = {}
    for row in contracts_rows:
        cid = row["contract_id"].strip()
        if not cid or cid in contracts:
            raise ValueError(f"Empty or duplicate contract_id: {cid!r}")
        amount = money(row["amount"], f"contracts.amount ({cid})")
        if amount < ZERO:
            raise ValueError(f"Negative contract amount: {cid}")
        contracts[cid] = {
            "contract_id": cid,
            "contractor": row["contractor"].strip(),
            "amount": amount,
            "signed_at": day(row["signed_at"], f"contracts.signed_at ({cid})", True),
        }

    works: dict[str, Work] = {}
    for row in works_rows:
        wid, cid = row["work_id"].strip(), row["contract_id"].strip()
        if not wid or wid in works:
            raise ValueError(f"Empty or duplicate work_id: {wid!r}")
        if cid not in contracts:
            raise ValueError(f"Work {wid} references unknown contract {cid}")
        status = row["status"].strip()
        if status not in {"planned", "done", "cancelled"}:
            raise ValueError(f"Unknown status {status!r} for work {wid}")
        completed = day(row["completed_at"], f"works.completed_at ({wid})")
        cancelled = day(row["cancelled_at"], f"works.cancelled_at ({wid})")
        if status == "done" and (completed is None or cancelled is not None):
            raise ValueError(f"Work {wid}: status 'done' requires completed_at and forbids cancelled_at")
        if status == "cancelled" and (cancelled is None or completed is not None):
            raise ValueError(f"Work {wid}: status 'cancelled' requires cancelled_at and forbids completed_at")
        if status == "planned" and (completed is not None or cancelled is not None):
            raise ValueError(f"Work {wid}: status 'planned' cannot have completion/cancellation dates")
        # The export is a current snapshot. Reconstruct status at the requested date
        # from dated completion/cancellation events rather than applying today's status
        # retrospectively.
        if cancelled and cancelled <= as_of:
            status_as_of = "cancelled"
        elif completed and completed <= as_of:
            status_as_of = "done"
        else:
            status_as_of = "planned"
        price = money(row["price"], f"works.price ({wid})")
        if price < ZERO:
            raise ValueError(f"Negative work price: {wid}")
        works[wid] = Work(
            wid, cid, row["title"].strip(), price, status,
            completed, cancelled, status_as_of
        )

    payments: list[dict] = []
    seen_payment_ids: set[str] = set()
    for row in payments_rows:
        pid, cid = row["payment_id"].strip(), row["contract_id"].strip()
        wid = row["work_id"].strip()
        if not pid or pid in seen_payment_ids:
            raise ValueError(f"Empty or duplicate payment_id: {pid!r}")
        seen_payment_ids.add(pid)
        if cid not in contracts:
            raise ValueError(f"Payment {pid} references unknown contract {cid}")
        if wid:
            if wid not in works:
                raise ValueError(f"Payment {pid} references unknown work {wid}")
            if works[wid].contract_id != cid:
                raise ValueError(f"Payment {pid}: work {wid} belongs to another contract")
        status = row["status"].strip()
        if status not in {"paid", "queued", "refunded"}:
            raise ValueError(f"Unknown payment status {status!r} for {pid}")
        amount = money(row["amount"], f"payments.amount ({pid})")
        if amount < ZERO:
            raise ValueError(f"Negative payment amount: {pid}")
        created = day(row["created_at"], f"payments.created_at ({pid})", True)
        paid_at = day(row["paid_at"], f"payments.paid_at ({pid})")
        refunded_at = day(row["refunded_at"], f"payments.refunded_at ({pid})")
        if status in {"paid", "refunded"} and paid_at is None:
            raise ValueError(f"Payment {pid} has status {status!r} but no paid_at")
        if status == "refunded" and refunded_at is None:
            raise ValueError(f"Payment {pid} has status 'refunded' but no refunded_at")
        if paid_at and paid_at < created:
            raise ValueError(f"Payment {pid}: paid_at is earlier than created_at")
        if refunded_at and paid_at and refunded_at < paid_at:
            raise ValueError(f"Payment {pid}: refunded_at is earlier than paid_at")
        if status == "paid" and refunded_at is not None:
            raise ValueError(f"Payment {pid}: status 'paid' cannot have refunded_at")
        if status == "queued" and paid_at is not None:
            raise ValueError(f"Payment {pid}: status 'queued' cannot have paid_at")

        net_paid = ZERO
        queued = ZERO
        # Reconstruct payment state on the report date using event dates. A payment
        # marked paid in today's export may still have been queued on the historical date.
        if created <= as_of:
            if paid_at is None or paid_at > as_of:
                queued = amount
            elif refunded_at is None or refunded_at > as_of:
                net_paid = amount

        payments.append({
            "payment_id": pid, "contract_id": cid, "work_id": wid,
            "purpose": row["purpose"].strip(), "amount": amount,
            "status": status, "created_at": created, "paid_at": paid_at,
            "refunded_at": refunded_at, "net_paid": net_paid, "queued": queued,
            "refunded_by_as_of": bool(status == "refunded" and refunded_at and refunded_at <= as_of and paid_at and paid_at <= as_of),
        })

    return contracts, works, payments


def allocate(contracts, works, payments):
    """Direct payments first; contract-level payments then fill work balances FIFO."""
    direct_by_contract: dict[str, list[dict]] = {cid: [] for cid in contracts}
    pooled_by_contract: dict[str, Decimal] = {cid: ZERO for cid in contracts}
    paid_by_contract: dict[str, Decimal] = {cid: ZERO for cid in contracts}
    queued_by_contract: dict[str, Decimal] = {cid: ZERO for cid in contracts}
    refunded_by_contract: dict[str, Decimal] = {cid: ZERO for cid in contracts}

    for p in payments:
        cid = p["contract_id"]
        paid_by_contract[cid] += p["net_paid"]
        queued_by_contract[cid] += p["queued"]
        if p["refunded_by_as_of"]:
            refunded_by_contract[cid] += p["amount"]
        if p["net_paid"] == ZERO:
            continue
        if p["work_id"]:
            works[p["work_id"]].direct_paid += p["net_paid"]
        else:
            pooled_by_contract[cid] += p["net_paid"]

    unallocated: dict[str, Decimal] = {}
    for cid in contracts:
        contract_works = [w for w in works.values() if w.contract_id == cid]
        # Work with a direct payment keeps that payment. The common contract pool
        # is applied to remaining balances: completed works first, then planned works.
        def order(w: Work):
            if w.status_as_of == "done":
                return (0, w.completed_at or date.max, w.work_id)
            if w.status_as_of == "planned":
                return (1, date.max, w.work_id)
            return (2, date.max, w.work_id)

        pool = pooled_by_contract[cid]
        for w in sorted(contract_works, key=order):
            if w.status_as_of == "cancelled":
                continue
            capacity = max(ZERO, w.price - w.direct_paid)
            applied = min(pool, capacity)
            w.allocated_contract_paid += applied
            pool -= applied
            if pool == ZERO:
                break
        unallocated[cid] = pool

    return {
        "paid": paid_by_contract,
        "queued": queued_by_contract,
        "refunded": refunded_by_contract,
        "unallocated": unallocated,
    }


def write_reports(output_dir: Path, contracts, works, totals, as_of: date):
    output_dir.mkdir(parents=True, exist_ok=True)
    works_path = output_dir / "works_report.csv"
    with works_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = [
            "as_of", "contract_id", "work_id", "title", "status_as_of",
            "price", "paid_direct", "paid_from_contract", "paid_total",
            "remaining", "overpaid",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for w in sorted(works.values(), key=lambda x: (x.contract_id, x.work_id)):
            writer.writerow({
                "as_of": as_of.isoformat(), "contract_id": w.contract_id,
                "work_id": w.work_id, "title": w.title,
                "status_as_of": w.status_as_of, "price": f"{w.price:.2f}",
                "paid_direct": f"{w.direct_paid:.2f}",
                "paid_from_contract": f"{w.allocated_contract_paid:.2f}",
                "paid_total": f"{w.paid:.2f}", "remaining": f"{w.remaining:.2f}",
                "overpaid": f"{w.overpaid:.2f}",
            })

    contracts_path = output_dir / "contracts_report.csv"
    with contracts_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = [
            "as_of", "contract_id", "contractor", "contract_amount",
            "paid_net", "queued_not_paid", "remaining_contract",
            "work_prices_total", "cancelled_work_prices_total",
            "active_work_prices_total", "work_remaining_total",
            "unallocated_paid", "refunded_by_as_of",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for cid, c in sorted(contracts.items()):
            work_list = [w for w in works.values() if w.contract_id == cid]
            work_prices = sum((w.price for w in work_list), ZERO)
            cancelled_work_prices = sum(
                (w.price for w in work_list if w.status_as_of == "cancelled"), ZERO
            )
            active_work_prices = work_prices - cancelled_work_prices
            work_remaining = sum((w.remaining for w in work_list), ZERO)
            remaining_contract = c["amount"] - totals["paid"][cid]
            writer.writerow({
                "as_of": as_of.isoformat(), "contract_id": cid,
                "contractor": c["contractor"],
                "contract_amount": f'{c["amount"]:.2f}',
                "paid_net": f'{totals["paid"][cid]:.2f}',
                "queued_not_paid": f'{totals["queued"][cid]:.2f}',
                "remaining_contract": f"{remaining_contract:.2f}",
                "work_prices_total": f"{work_prices:.2f}",
                "cancelled_work_prices_total": f"{cancelled_work_prices:.2f}",
                "active_work_prices_total": f"{active_work_prices:.2f}",
                "work_remaining_total": f"{work_remaining:.2f}",
                "unallocated_paid": f'{totals["unallocated"][cid]:.2f}',
                "refunded_by_as_of": f'{totals["refunded"][cid]:.2f}',
            })
    return works_path, contracts_path


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Распределение платежей по работам и договорам на заданную дату."
    )
    parser.add_argument("--data", type=Path, default=Path("data"),
                        help="Каталог с contracts.csv, works.csv, payments.csv")
    parser.add_argument("--as-of", required=True, help="Отчётная дата YYYY-MM-DD")
    parser.add_argument("--out", type=Path, default=Path("out"),
                        help="Каталог для CSV-отчётов")
    args = parser.parse_args(argv)
    report_date = day(args.as_of, "--as-of", True)
    contracts, works, payments = load_inputs(args.data, report_date)
    totals = allocate(contracts, works, payments)
    works_path, contracts_path = write_reports(
        args.out, contracts, works, totals, report_date
    )
    print(f"Отчёт на {report_date:%d.%m.%Y} сформирован.")
    print(f"Работы: {works_path}")
    print(f"Договоры: {contracts_path}")
    for cid, c in sorted(contracts.items()):
        print(
            f'{cid} — {c["contractor"]}: договор {c["amount"]:.2f} ₽; '
            f'оплачено {totals["paid"][cid]:.2f} ₽; '
            f'остаток {c["amount"] - totals["paid"][cid]:.2f} ₽; '
            f'не распределено {totals["unallocated"][cid]:.2f} ₽'
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
