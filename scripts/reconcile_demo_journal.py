#!/usr/bin/env python3
"""Read-only reconciliation for a trader demo-operation CSV export.

Uses only Python's standard library. It does not connect to the application or
write files. Unknown fee valuations and opening balances remain explicitly null.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


NUMERIC_FIELDS = (
    "quantity", "price", "quote_amount", "fee_amount", "balance_before", "balance_after",
    "application_realized_profit_usd", "application_buy_debit_usd",
    "application_sell_proceeds_accumulated_usd", "application_sell_fees_accumulated_usd",
    "application_remaining_quantity",
)


def decimal_value(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def utc_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    for row in rows:
        for field in NUMERIC_FIELDS:
            row[field] = decimal_value(row.get(field))
        try:
            row["book_snapshot"] = json.loads(row.get("book_snapshot") or "null")
        except (json.JSONDecodeError, TypeError):
            row["book_snapshot"] = None
        row["event_id"] = str(row.get("event_id") or "")
        row["settlement_id"] = str(row.get("settlement_id") or "")
        row["run_id"] = str(row.get("run_id") or "")
    return rows


def deduplicate(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    seen_keys: dict[str, dict[str, Any]] = {}
    seen_ids: dict[str, str] = {}
    duplicates: list[dict[str, Any]] = []
    id_conflicts: list[dict[str, Any]] = []
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = row.get("event_key", "")
        event_id = row.get("event_id", "")
        if key and key in seen_keys:
            previous = seen_keys[key]
            duplicates.append({
                "event_key": key, "first_event_id": previous.get("event_id"),
                "duplicate_event_id": event_id, "identical": previous == row,
            })
            continue
        if event_id and event_id in seen_ids and seen_ids[event_id] != key:
            id_conflicts.append({"event_id": event_id, "event_keys": [seen_ids[event_id], key]})
            continue
        if key:
            seen_keys[key] = row
        if event_id:
            seen_ids[event_id] = key
        unique.append(row)
    unique.sort(key=lambda row: (utc_time(row["event_at_utc"]) if row.get("event_at_utc") else datetime.min.replace(tzinfo=timezone.utc),
                                 int(row["event_id"]) if row["event_id"].isdigit() else 0))
    return unique, duplicates, id_conflicts


def _amount(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _fee_quote_value(event: dict[str, Any]) -> Decimal | None:
    value = event.get("quote_amount")
    fee_amount = event.get("fee_amount")
    asset = str(event.get("fee_asset") or "").upper()
    if value == 0 and fee_amount not in (None, Decimal(0)) and asset not in {"USDT", "USD"}:
        return None
    return value


def _equal_balance(left: Decimal, right: Decimal, asset: str) -> bool:
    tolerance = Decimal("0.00000001") if asset.upper() in {"USDT", "USD"} else Decimal("0.000000000000000001")
    return abs(left - right) <= tolerance


def reconcile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    events, duplicate_keys, duplicate_ids = deduplicate(rows)
    gaps: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    runs = sorted({row["run_id"] for row in events if row["run_id"]})

    ids = sorted({int(row["event_id"]) for row in events if row["event_id"].isdigit()})
    id_gaps = []
    for left, right in zip(ids, ids[1:]):
        if right > left + 1:
            id_gaps.append({"from": left + 1, "to": right - 1, "count": right - left - 1,
                            "note": "ID gaps may also result from rolled-back inserts"})

    balances: dict[tuple[str, str, str], dict[str, Any]] = {}
    settlements: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {
        "buy": None, "buy_fees": [], "transfer_fees": [], "sells": [], "sell_fees": [],
        "coin_credit": Decimal(0), "coin_credit_known": True,
        "application": {},
    })
    transfer_events: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in events:
        sid = row["settlement_id"]
        run_id = row["run_id"]
        settlement_key = (run_id, sid)
        record = settlements[settlement_key]
        for name in ("application_realized_profit_usd", "application_buy_debit_usd",
                     "application_sell_proceeds_accumulated_usd", "application_sell_fees_accumulated_usd",
                     "application_remaining_quantity", "application_status"):
            value = row.get(name)
            if value not in (None, ""):
                record["application"][name] = value

        kind = row.get("event_type", "")
        key = row.get("event_key", "")
        if kind == "buy":
            record["buy"] = row
            snapshot = row.get("book_snapshot")
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("buy"), dict):
                gaps.append({"event_key": key, "issue": "buy event has no buy-side book snapshot"})
        elif kind == "sell":
            record["sells"].append(row)
            if not isinstance(row.get("book_snapshot"), dict) or not row["book_snapshot"].get("bids"):
                gaps.append({"event_key": key, "issue": "sell event has no bid snapshot"})
        elif kind == "commission":
            if ":commission:buy" in key:
                record["buy_fees"].append(row)
            elif ":commission:transfer" in key:
                record["transfer_fees"].append(row)
            elif ":commission:sell:" in key:
                record["sell_fees"].append(row)
            if row.get("fee_amount") is None:
                gaps.append({"event_key": key, "issue": "commission amount is missing"})
            if row.get("fee_amount") not in (None, Decimal(0)) and _fee_quote_value(row) is None:
                gaps.append({"event_key": key, "issue": f"fee value in {row.get('fee_asset') or 'unknown asset'} is unknown"})
        elif kind in {"transfer_sent", "transfer_received"}:
            transfer_events[settlement_key].add(kind)

        if kind == "balance_change":
            exchange = str(row.get("exchange_slug") or "").lower()
            asset = str(row.get("asset") or "").upper()
            balance_key = (run_id, exchange, asset)
            state = balances.setdefault(balance_key, {
                "opening": None, "net_change": Decimal(0), "current": None,
                "application_balance": None, "known": False,
            })
            before, after, quantity = row.get("balance_before"), row.get("balance_after"), row.get("quantity")
            if before is not None:
                if state["current"] is not None and not _equal_balance(state["current"], before, asset):
                    discrepancies.append({"event_key": key, "exchange": exchange, "asset": asset,
                                          "kind": "balance chain", "expected_before": _amount(state["current"]),
                                          "application_before": _amount(before)})
                if state["opening"] is None:
                    state["opening"] = before - state["net_change"]
                    state["known"] = True
                if quantity is not None and after is not None and not _equal_balance(before + quantity, after, asset):
                    discrepancies.append({"event_key": key, "exchange": exchange, "asset": asset,
                                          "kind": "balance delta", "before_plus_quantity": _amount(before + quantity),
                                          "application_after": _amount(after)})
            elif state["current"] is None:
                gaps.append({"event_key": key, "issue": f"opening {exchange} {asset} balance is unknown"})
            if quantity is None:
                gaps.append({"event_key": key, "issue": "balance change quantity is missing"})
            else:
                state["net_change"] += quantity
                state["current"] = (state["current"] + quantity) if state["current"] is not None else None
            if after is not None:
                state["current"] = after
            state["application_balance"] = after
            if ":balance:buy:" in key and asset != "USDT" and quantity is not None:
                record["coin_credit"] += quantity
            if ":balance:transfer:" in key:
                transfer_events[settlement_key].add("sent" if quantity is not None and quantity < 0 else "received")

    for (run_id, sid), record in settlements.items():
        if record["buy"] is None and (record["sells"] or record["buy_fees"]):
            gaps.append({"settlement_id": sid, "issue": "settlement has sell/fee events but no buy event"})
        if record["buy"] is not None:
            buy = record["buy"]
            if not any(row.get("run_id") == run_id and row.get("event_type") == "balance_change" and row.get("event_key", "").startswith(f"{sid}:balance:buy:")
                       and str(row.get("asset") or "").upper() != "USDT" for row in events):
                gaps.append({"settlement_id": sid, "issue": "buy has no matching coin balance change"})
            if not record["buy_fees"]:
                gaps.append({"settlement_id": sid, "issue": "buy commission event is missing"})
        for sale in record["sells"]:
            sale_key = str(sale.get("event_key", "")).split(":sell:", 1)[-1]
            if not any(fee.get("event_key") == f"{sid}:commission:sell:{sale_key}" for fee in record["sell_fees"]):
                gaps.append({"event_key": sale.get("event_key"), "issue": "matching sell commission event is missing"})
        if "transfer_sent" in transfer_events[(run_id, sid)] and "transfer_received" not in transfer_events[(run_id, sid)]:
            gaps.append({"settlement_id": sid, "issue": "transfer sent event has no receive event"})
        if "transfer_received" in transfer_events[(run_id, sid)] and "transfer_sent" not in transfer_events[(run_id, sid)]:
            gaps.append({"settlement_id": sid, "issue": "transfer receive event has no send event"})

    trade_reports = []
    for (run_id, sid), record in sorted(settlements.items(), key=lambda item: item[0]):
        buy = record["buy"]
        if buy is None:
            continue
        buy_cost = buy.get("quote_amount")
        buy_fee_values = [_fee_quote_value(fee) for fee in record["buy_fees"]]
        transfer_fee_values = [_fee_quote_value(fee) for fee in record["transfer_fees"]]
        sell_proceeds_values = [sale.get("quote_amount") for sale in record["sells"]]
        sell_fee_values = [_fee_quote_value(fee) for fee in record["sell_fees"]]
        all_cost_values = [buy_cost, *buy_fee_values, *transfer_fee_values]
        cost_known = all(value is not None for value in all_cost_values)
        total_cost = sum(all_cost_values, Decimal(0)) if cost_known else None
        sells_known = all(value is not None for value in (*sell_proceeds_values, *sell_fee_values))
        gross_sales = sum(sell_proceeds_values, Decimal(0)) if all(v is not None for v in sell_proceeds_values) else None
        sell_fees = sum(sell_fee_values, Decimal(0)) if all(v is not None for v in sell_fee_values) else None
        net_sales = gross_sales - sell_fees if gross_sales is not None and sell_fees is not None else None
        sold_quantity = sum((sale.get("quantity") or Decimal(0) for sale in record["sells"]), Decimal(0))
        bought_quantity = record["coin_credit"]
        reconstructed_remaining = bought_quantity - sold_quantity
        app_remaining = record["application"].get("application_remaining_quantity")
        if reconstructed_remaining < 0:
            gaps.append({"settlement_id": sid, "issue": "sold quantity exceeds journaled coin credit",
                         "excess": _amount(-reconstructed_remaining)})
        if app_remaining is not None and reconstructed_remaining >= 0 and not _equal_balance(
                reconstructed_remaining, app_remaining, str(buy.get("asset") or "")):
            discrepancies.append({"run_id": run_id, "settlement_id": sid, "kind": "remaining quantity",
                                  "journal": _amount(reconstructed_remaining), "application": _amount(app_remaining),
                                  "difference": _amount(reconstructed_remaining - app_remaining)})
        cost_sold = (total_cost * min(Decimal(1), sold_quantity / bought_quantity)
                     if total_cost is not None and bought_quantity > 0 else None)
        independent_realized = net_sales - cost_sold if net_sales is not None and cost_sold is not None else None
        app = record["application"]
        app_value = app.get("application_realized_profit_usd")
        app_buy_debit = app.get("application_buy_debit_usd")
        app_formula = (gross_sales - sell_fees - app_buy_debit
                       if gross_sales is not None and sell_fees is not None and app_buy_debit is not None else None)
        pnl_difference = (independent_realized - app_value
                          if independent_realized is not None and app_value is not None
                          and bought_quantity > 0 and sold_quantity >= bought_quantity else None)
        debit_difference = (total_cost - app_buy_debit
                            if total_cost is not None and app_buy_debit is not None else None)
        if debit_difference not in (None, Decimal(0)):
            discrepancies.append({"settlement_id": sid, "kind": "buy debit", "journal": _amount(total_cost),
                                  "application": _amount(app_buy_debit), "difference": _amount(debit_difference)})
        app_sales = app.get("application_sell_proceeds_accumulated_usd")
        journal_sales = gross_sales
        if app_sales is not None and journal_sales is not None and journal_sales != app_sales:
            discrepancies.append({"settlement_id": sid, "kind": "sell proceeds", "journal": _amount(journal_sales),
                                  "application": _amount(app_sales), "difference": _amount(journal_sales - app_sales)})
        app_sell_fees = app.get("application_sell_fees_accumulated_usd")
        if app_sell_fees is not None and sell_fees is not None and sell_fees != app_sell_fees:
            discrepancies.append({"settlement_id": sid, "kind": "sell fees", "journal": _amount(sell_fees),
                                  "application": _amount(app_sell_fees), "difference": _amount(sell_fees - app_sell_fees)})
        if pnl_difference not in (None, Decimal(0)):
            discrepancies.append({"settlement_id": sid, "kind": "realized PnL / cost basis",
                                  "journal": _amount(independent_realized), "application": _amount(app_value),
                                  "difference": _amount(pnl_difference)})
        trade_report = {
            "settlement_id": sid,
            "run_id": run_id,
            "asset": buy.get("asset"),
            "buy_cost_usd": _amount(buy_cost),
            "buy_and_transfer_fees_usd": _amount(sum((*buy_fee_values, *transfer_fee_values), Decimal(0)))
            if all(v is not None for v in (*buy_fee_values, *transfer_fee_values)) else None,
            "total_cost_basis_usd": _amount(total_cost),
            "quantity_acquired_from_balance_log": _amount(bought_quantity),
            "quantity_sold": _amount(sold_quantity),
            "quantity_remaining_from_journal": _amount(reconstructed_remaining) if reconstructed_remaining >= 0 else None,
            "cost_basis_sold_usd": _amount(cost_sold),
            "gross_sales_usd": _amount(gross_sales),
            "sell_fees_usd": _amount(sell_fees),
            "net_sales_usd": _amount(net_sales),
            "remaining_cost_basis_usd": _amount(total_cost - cost_sold) if total_cost is not None and cost_sold is not None else None,
            "independent_realized_pnl_usd": _amount(independent_realized),
            "application_realized_pnl_usd": _amount(app_value),
            "application_formula_from_journal_usd": _amount(app_formula),
            "independent_vs_application_pnl_difference_usd": _amount(pnl_difference),
            "application_buy_debit_usd": _amount(app_buy_debit),
            "journal_cost_vs_application_debit_difference_usd": _amount(debit_difference),
            "application_status": app.get("application_status"),
            "cost_complete": cost_known,
            "sales_complete": sells_known,
        }
        if record["transfer_fees"] and any(
            fee.get("fee_amount") not in (None, Decimal(0)) and fee.get("quote_amount") in (None, Decimal(0))
            and str(fee.get("fee_asset") or "").upper() != "USDT" for fee in record["transfer_fees"]
        ):
            trade_report["unknown_transfer_fee_value"] = True
        trade_reports.append(trade_report)

    balance_reports = []
    for (run_id, exchange, asset), state in sorted(balances.items()):
        if not state["known"]:
            final_value = None
        else:
            final_value = state["opening"] + state["net_change"]
        app_value = state["application_balance"]
        delta = final_value - app_value if final_value is not None and app_value is not None else None
        if delta is not None and not _equal_balance(final_value, app_value, asset):
            discrepancies.append({"run_id": run_id, "exchange": exchange, "asset": asset, "kind": "final balance",
                                  "reconstructed": _amount(final_value), "application": _amount(app_value),
                                  "difference": _amount(delta)})
        balance_reports.append({
            "run_id": run_id, "exchange": exchange, "asset": asset, "opening_balance": _amount(state["opening"]),
            "net_change": _amount(state["net_change"]), "reconstructed_final_balance": _amount(final_value),
            "application_last_balance_after": _amount(app_value), "difference": _amount(delta),
            "complete": state["known"],
        })

    fees_by_asset: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    fee_value_total: dict[str, Decimal] = defaultdict(Decimal)
    fee_value_known: dict[str, bool] = defaultdict(lambda: True)
    for row in events:
        if row.get("event_type") != "commission":
            continue
        fee_asset = str(row.get("fee_asset") or "UNKNOWN").upper()
        if row.get("fee_amount") is not None:
            fees_by_asset[(row["run_id"], fee_asset)] += row["fee_amount"]
        else:
            gaps.append({"event_key": row.get("event_key"), "issue": "fee amount unavailable"})
        if _fee_quote_value(row) is None:
            fee_value_known[row["run_id"]] = False
        else:
            fee_value_total[row["run_id"]] += _fee_quote_value(row) or Decimal(0)

    fees_by_run = {
        run_id: {
            "fees_by_asset": {
                asset: _amount(amount) for (fee_run, asset), amount in sorted(fees_by_asset.items())
                if fee_run == run_id
            },
            "fees_total_usd": _amount(fee_value_total[run_id]) if fee_value_known[run_id] else None,
            "fee_value_complete": fee_value_known[run_id],
        }
        for run_id in runs
    }

    return {
        "source": "exported demo operation journal; read-only",
        "valuation_policy": "No market value is inferred for unknown assets or fees; affected USD totals are null.",
        "runs": runs,
        "processed_events": len(events),
        "duplicates": duplicate_keys,
        "event_id_conflicts": duplicate_ids,
        "possible_event_id_gaps": id_gaps,
        "balances": balance_reports,
        "fees_by_run": fees_by_run,
        "trades": trade_reports,
        "discrepancies": discrepancies,
        "gaps": gaps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal_csv", type=Path, help="Экспортированный demo-operations CSV")
    parser.add_argument("--fail-on-findings", action="store_true", help="Код 2 при расхождениях или пропусках")
    args = parser.parse_args()
    try:
        result = reconcile(read_rows(args.journal_csv))
    except (OSError, csv.Error, UnicodeError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if args.fail_on_findings and (result["discrepancies"] or result["gaps"] or result["duplicates"]
                                            or result["event_id_conflicts"] or result["possible_event_id_gaps"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
