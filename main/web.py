"""Веб-страница отфильтрованных торговых возможностей."""

from __future__ import annotations

import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from config import BUY_PRICE_MUST_BE_LOWER, CALCULATOR_SPREAD_LEVELS
from database import connect_db, initialize_database, read_config

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo("Europe/Kyiv")
TRANSFER_EPISODE_GAP_SECONDS = 60


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    result = format(value, "f") if hasattr(value, "as_tuple") else str(value)
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return html.escape(result)


def _fmt_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    utc_value = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return html.escape(utc_value.astimezone(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S "))


def _spot_market_url(exchange: Any, market_pair: Any) -> str | None:
    """Build a direct spot-trading URL for supported exchanges."""
    pair = str(market_pair or "").strip().upper()
    if "/" not in pair:
        return None
    base, quote_symbol = pair.split("/", 1)
    if not base or not quote_symbol:
        return None
    safe_pair = f"{quote(base, safe='')}_{quote(quote_symbol, safe='')}"
    exchange_name = str(exchange or "").strip().casefold()
    if exchange_name == "mexc":
        return f"https://www.mexc.com/exchange/{safe_pair}?_from=search_spot_trade"
    if exchange_name == "binance":
        return f"https://www.binance.com/ru-UA/trade/{safe_pair}?type=spot"
    if exchange_name == "okx":
        okx_pair = f"{quote(base.lower(), safe='')}-{quote(quote_symbol.lower(), safe='')}"
        return f"https://www.okx.com/ru/trade-spot/{okx_pair}#workspaceId=1031710357440172032"
    if exchange_name == "bybit":
        return f"https://www.bybit.com/uk-UA/trade/spot/{quote(base, safe='')}/{quote(quote_symbol, safe='')}"
    return None


def _market_cell(exchange: Any, market_pair: Any, price: Any) -> str:
    exchange_text = html.escape(str(exchange))
    pair_text = html.escape(str(market_pair))
    url = _spot_market_url(exchange, market_pair)
    content = f"{exchange_text}<small>{pair_text} · {_fmt(price)}</small>"
    if url is None:
        return content
    return f'<a class="market-link" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{content}</a>'


def _market_link(exchange: Any, market_pair: Any, label: Any) -> str:
    text = html.escape(str(label))
    url = _spot_market_url(exchange, market_pair)
    if url is None:
        return text
    return f'<a class="market-link" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{text}</a>'


def _elapsed_seconds_since(value: datetime) -> int:
    """Посчитать длительность по UTC, независимо от часового пояса процесса."""
    utc_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - utc_value).total_seconds()))


def _load_opportunities() -> tuple[list[dict[str, Any]], int]:
    connection = connect_db()
    try:
        config = read_config(connection)
        price_direction = "<" if BUY_PRICE_MUST_BE_LOWER else ">"
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT source.market_id AS source_market_id, source.market_pair AS source_pair, "
                "source.base_symbol, source.quote_symbol AS source_quote, "
                "life.spread_percent AS price_difference_percent, "
                "life.buy_exchange AS exchange_name, life.buy_market_pair AS venue_pair, "
                "life.buy_price AS price, "
                "life.sell_exchange, life.sell_market_pair, life.sell_price, "
                "life.last_confirmed_at AS last_updated, "
                "life.started_at "
                "FROM opportunity_lifetimes AS life "
                "JOIN market_pairs AS source ON source.market_id = life.source_market_id "
                "WHERE life.is_active = 1 AND life.threshold_percent = %s "
                f"AND life.buy_price {price_direction} life.sell_price "
                "ORDER BY life.spread_percent DESC, source.market_id",
                (CALCULATOR_SPREAD_LEVELS[0],),
            )
            rows = cursor.fetchall()
        return rows, int(config["interval_minutes"])
    finally:
        connection.close()


def _load_worker_status() -> dict[str, str]:
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT config_key, config_value FROM config "
                "WHERE config_key IN ('worker_status', 'worker_phase', 'worker_next_run_at', 'worker_updated_at')"
            )
            values = dict(cursor.fetchall())
            return {
                "status": values.get("worker_status", "unknown"),
                "phase": values.get("worker_phase", ""),
                "next_run_at": values.get("worker_next_run_at", ""),
                "updated_at": values.get("worker_updated_at", ""),
            }
    finally:
        connection.close()


def _load_trader_dashboard() -> tuple[dict[str, Any] | None, dict[str, Any], list[dict[str, Any]]]:
    connection = connect_db()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT balance_usd, training_mode, created_at, updated_at "
                "FROM trader_account WHERE account_id = 1"
            )
            account = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) AS total_count, "
                "SUM(status = 'settled') AS settled_count, "
                "SUM(status IN ('pending', 'settling', 'partial')) AS pending_count, "
                "SUM(status = 'open') AS open_count, "
                "SUM(route_status = 'confirmed') AS confirmed_routes, "
                "SUM(route_status = 'unconfirmed') AS assumed_routes, "
                "COALESCE(SUM(CASE WHEN status = 'settled' THEN realized_profit_usd ELSE 0 END), 0) AS net_profit, "
                "COALESCE(SUM(CASE WHEN status = 'settled' THEN buy_fee_usd + COALESCE(sell_fee_usd, 0) ELSE 0 END), 0) AS total_fees "
                "FROM trader_training_settlements"
            )
            stats = cursor.fetchone() or {}
            cursor.execute(
                "SELECT settlement_id, source_pair, buy_exchange, buy_market_pair, buy_price, "
                "sell_exchange, sell_market_pair, expected_sell_price, final_sell_price, quantity, "
                "buy_cost_usd, realized_sell_proceeds_usd, buy_fee_usd, sell_fee_usd, "
                "expected_profit_usd, realized_profit_usd, status, created_at, settle_after_utc, settled_at, "
                "planned_sell_at, actual_sell_at, "
                "route_status, remaining_quantity, transfer_fee_amount, transfer_fee_currency, "
                "expected_profit_before_reserve_usd, expected_profit_after_reserve_usd, price_reserve_usd "
                "FROM trader_training_settlements ORDER BY created_at DESC, settlement_id DESC LIMIT 500"
            )
            trades = cursor.fetchall()
        return account, stats, trades
    finally:
        connection.close()


def _render_trader_page(
    account: dict[str, Any] | None, stats: dict[str, Any], trades: list[dict[str, Any]]
) -> str:
    trade_rows = []
    status_labels = {
        "pending": "Ожидает перевода",
        "settling": "Проверяется продажа",
        "settled": "Завершена",
        "partial": "Продана частично",
        "open": "Остаток сохранён",
        "expired": "Истёк срок оценки",
    }
    for trade in trades:
        settled = trade["status"] == "settled"
        realized = trade["status"] in {"settled", "partial", "open"}
        profit = trade["realized_profit_usd"] if realized else trade["expected_profit_usd"]
        profit_class = "positive" if profit is not None and Decimal(str(profit)) >= 0 else "negative"
        sale_price = trade["final_sell_price"] if realized else trade["expected_sell_price"]
        route_label = {"confirmed": "Подтверждён", "unconfirmed": "Предполагаемый", "legacy": "Старая запись"}.get(
            trade["route_status"], html.escape(str(trade["route_status"]))
        )
        trade_rows.append(
            "<tr>"
            f"<td>{html.escape(str(trade['source_pair']))}</td>"
            f"<td>{_market_cell(trade['buy_exchange'], trade['buy_market_pair'], trade['buy_price'])}</td>"
            f"<td>{_market_cell(trade['sell_exchange'], trade['sell_market_pair'], sale_price)}</td>"
            f"<td>{_fmt(trade['quantity'])}</td>"
            f"<td>{_fmt(trade['buy_cost_usd'])}</td>"
            f"<td>{_fmt(trade['realized_sell_proceeds_usd'] if realized else None)}</td>"
            f"<td>{_fmt(trade['buy_fee_usd'])} + {_fmt(trade['sell_fee_usd'] if realized else None)}</td>"
            f'<td class="{profit_class}">{_fmt(profit)} USD'
            f'{"<small>накопленная, позиция открыта</small>" if realized and not settled else ""}'
            f'{"<small>ожидаемая до резерва; после резерва " + _fmt(trade["expected_profit_after_reserve_usd"]) + " USDT</small>" if not realized else ""}</td>'
            f"<td>{route_label}<small>Перевод: {_fmt(trade['transfer_fee_amount'])} {html.escape(str(trade['transfer_fee_currency']))}</small></td>"
            f"<td>{_fmt(trade['remaining_quantity'])}</td>"
            f"<td><span class=\"status status-{html.escape(str(trade['status']))}\">"
            f"{status_labels.get(trade['status'], html.escape(str(trade['status'])))}</span></td>"
            f"<td>{_fmt_time(trade['created_at'])}"
            f"<small>План продажи: {_fmt_time(trade['planned_sell_at'])}</small>"
            f"<small>Последняя продажа: {_fmt_time(trade['actual_sell_at'])}</small>"
            f"<small>{'Завершена: ' + _fmt_time(trade['settled_at']) if settled else 'Следующая проверка: ' + _fmt_time(trade['settle_after_utc'])}</small></td>"
            "</tr>"
        )
    if not trade_rows:
        trade_rows.append('<tr><td colspan="12">Трейдер ещё не входил в сделки.</td></tr>')

    balance = _fmt(account["balance_usd"]) if account else "—"
    mode = "Тренировка" if account and account["training_mode"] else "Рабочий режим"
    last_update = _fmt_time(account["updated_at"]) if account else "Счёт ещё не создан"
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Трейдер</title>
<style>
:root {{ color-scheme: dark; font-family: system-ui, sans-serif; background:#0b1220; color:#e5e7eb; }}
body {{ margin:0; }} header {{ padding:24px 4vw; background:#111b2e; border-bottom:1px solid #26334a; }}
main {{ padding:24px 4vw; }} h1 {{ margin:0 0 8px; }} nav {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:14px; }}
nav a {{ padding:8px 12px; border:1px solid #334155; border-radius:8px; color:#7dd3fc; text-decoration:none; }} nav a.active {{ background:#1e3a5f; }}
.muted, small {{ color:#9ca3af; }} small {{ display:block; margin-top:4px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:24px; }}
.card {{ padding:16px; border:1px solid #26334a; border-radius:10px; background:#111b2e; }} .card span {{ display:block; color:#9ca3af; font-size:.85rem; }} .card strong {{ display:block; margin-top:8px; font-size:1.3rem; }}
.table-wrap {{ overflow-x:auto; border:1px solid #26334a; border-radius:10px; }} table {{ width:100%; min-width:1200px; border-collapse:collapse; background:#111b2e; }}
th,td {{ padding:11px 13px; text-align:left; border-bottom:1px solid #26334a; white-space:nowrap; }} th {{ color:#9fb4d0; font-size:.8rem; text-transform:uppercase; }}
.positive {{ color:#67e8a5; font-weight:700; }} .negative {{ color:#f87171; font-weight:700; }} .status {{ color:#fbbf24; }} .status-settled {{ color:#67e8a5; }}
a.market-link {{ color:inherit; text-decoration:none; }} a.market-link:hover {{ color:#7dd3fc; text-decoration:underline; }}
</style></head><body>
<header><h1>Трейдер</h1><div class="muted">Состояние счёта и сделки · страница обновляется каждые 30 секунд · баланс на {last_update}</div>
<nav><a href="/">Возможности</a><a href="/situations">Активные ситуации</a><a href="/history">История</a><a class="active" href="/trader">Трейдер</a></nav></header>
<main><section class="cards">
<div class="card"><span>Баланс</span><strong>{balance} USD</strong></div>
<div class="card"><span>Режим</span><strong>{mode}</strong></div>
<div class="card"><span>Завершено сделок</span><strong>{_fmt(stats.get('settled_count') or 0)}</strong></div>
<div class="card"><span>В ожидании</span><strong>{_fmt(stats.get('pending_count') or 0)}</strong></div>
<div class="card"><span>Подтверждённые маршруты</span><strong>{_fmt(stats.get('confirmed_routes') or 0)}</strong></div>
<div class="card"><span>Предполагаемые маршруты</span><strong>{_fmt(stats.get('assumed_routes') or 0)}</strong></div>
<div class="card"><span>Остались открыты</span><strong>{_fmt(stats.get('open_count') or 0)}</strong></div>
<div class="card"><span>Реализованный результат</span><strong class="{'positive' if Decimal(str(stats.get('net_profit') or 0)) >= 0 else 'negative'}">{_fmt(stats.get('net_profit') or 0)} USD</strong></div>
<div class="card"><span>Уплаченные комиссии</span><strong>{_fmt(stats.get('total_fees') or 0)} USD</strong></div>
</section><h2>Сделки трейдера</h2><div class="table-wrap"><table><thead><tr>
<th>Пара</th><th>Купил</th><th>Продал / цена оценки</th><th>Количество</th><th>Затраты на покупку</th><th>Выручка</th><th>Комиссии buy + sell</th><th>Результат</th><th>Маршрут и комиссия перевода</th><th>Остаток монет</th><th>Статус</th><th>Время (Киев)</th>
</tr></thead><tbody>{''.join(trade_rows)}</tbody></table></div></main></body></html>"""


def _classify_transfer_groups(
    lifetime_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate short interruptions for the same route and threshold.

    Confirmed spread seconds accumulate across episodes separated by at most
    TRANSFER_EPISODE_GAP_SECONDS. The first episode's sampled transfer estimate applies to that
    transfer attempt. Gaps themselves do not add to the elapsed opportunity time.
    """
    now = datetime.utcnow()
    route_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in lifetime_rows:
        key = (
            row.get("source_market_id"), row["source_pair"], row["threshold_percent"],
            row["buy_exchange"], row["buy_market_pair"],
            row["sell_exchange"], row["sell_market_pair"],
        )
        route_groups.setdefault(key, []).append(row)

    transfer_groups: list[dict[str, Any]] = []
    for key, episodes in route_groups.items():
        episodes.sort(key=lambda item: item["started_at"])
        clusters: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        previous_end: datetime | None = None
        for row in episodes:
            start = row["started_at"]
            if row["is_active"]:
                end = now
                duration = max(0, int((end - start).total_seconds()))
            else:
                duration = int(row.get("duration_seconds") or 0)
                end = start + timedelta(seconds=duration)
            row["_confirmed_seconds"] = duration
            gap = (start - previous_end).total_seconds() if previous_end else None
            if current and gap is not None and gap > TRANSFER_EPISODE_GAP_SECONDS:
                clusters.append(current)
                current = []
            current.append(row)
            previous_end = max(previous_end, end) if previous_end else end
        if current:
            clusters.append(current)

        for cluster in clusters:
            total_seconds = sum(row["_confirmed_seconds"] for row in cluster)
            active = any(bool(row["is_active"]) for row in cluster)
            expected = next(
                (float(row["transfer_seconds"]) for row in cluster
                 if row.get("transfer_seconds") is not None),
                None,
            )
            if expected is None:
                state = "unknown"
            elif total_seconds >= expected:
                state = "passed"
            elif active:
                state = "pending"
            else:
                state = "failed"
            group = {
                "source_market_id": key[0],
                "threshold_percent": key[2],
                "started_at": cluster[0]["started_at"],
                "total_seconds": total_seconds,
                "expected_seconds": expected,
                "state": state,
                "episodes": cluster,
            }
            transfer_groups.append(group)
            for row in cluster:
                row["_transfer_state"] = state
                row["_transfer_total_seconds"] = total_seconds
                row["_transfer_expected_seconds"] = expected
                row["_transfer_episode_count"] = len(cluster)
                row["_transfer_group_started_at"] = cluster[0]["started_at"]
    return transfer_groups


def _load_history() -> tuple[list[dict[str, Any]], dict[int, dict[str, int]]]:
    connection = connect_db()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT history_id, source_pair, spread_percent, low_exchange, low_market_pair, low_price, "
                "high_exchange, high_market_pair, high_price, observed_at "
                "FROM opportunity_history ORDER BY observed_at DESC, history_id DESC LIMIT 500"
            )
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT lifetime_id, source_market_id, source_pair, threshold_percent, "
                "buy_exchange, buy_market_pair, sell_exchange, sell_market_pair, "
                "started_at, ended_at, last_confirmed_at, duration_seconds, is_active, transfer_seconds "
                "FROM opportunity_lifetimes "
                "WHERE started_at >= TIMESTAMPADD(MINUTE, -1440, UTC_TIMESTAMP()) "
                "ORDER BY source_market_id, threshold_percent, started_at, lifetime_id"
            )
            transfer_rows = cursor.fetchall()
            transfer_groups = _classify_transfer_groups(transfer_rows)
            counters: dict[int, dict[str, int]] = {}
            low, middle, high = CALCULATOR_SPREAD_LEVELS
            for minutes in (60, 720, 1440):
                cutoff = datetime.utcnow() - timedelta(minutes=minutes)
                counters[minutes] = {
                    band: sum(
                        group["state"] == "passed"
                        and group["started_at"] >= cutoff
                        and group["threshold_percent"] == threshold
                        for group in transfer_groups
                    )
                    for band, threshold in (
                        ("low_band", low), ("middle_band", middle), ("high_band", high)
                    )
                }
        return rows, counters
    finally:
        connection.close()


def _load_lifetimes(is_active: bool = False) -> list[dict[str, Any]]:
    connection = connect_db()
    try:
        price_direction = "<" if BUY_PRICE_MUST_BE_LOWER else ">"
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT lifetime_id, source_market_id, source_pair, threshold_percent, spread_percent, max_spread_percent, "
                "buy_exchange, buy_market_pair, buy_price, sell_exchange, sell_market_pair, sell_price, "
                "started_at, last_confirmed_at, ended_at, duration_seconds, is_active, transfer_seconds "
                "FROM opportunity_lifetimes WHERE is_active = %s "
                f"AND buy_price {price_direction} sell_price "
                "ORDER BY started_at DESC, lifetime_id DESC LIMIT 500",
                (int(is_active),),
            )
            return cursor.fetchall()
    finally:
        connection.close()


def _render_history(
    rows: list[dict[str, Any]],
    counters: dict[int, dict[str, int]],
    rows_lifetimes: list[dict[str, Any]],
    active_view: bool = False,
) -> str:
    low, middle, high = CALCULATOR_SPREAD_LEVELS
    bands = (
        ("low_band", f"от {_fmt(low)}%"),
        ("middle_band", f"от {_fmt(middle)}%"),
        ("high_band", f"от {_fmt(high)}%"),
    )
    counter_rows = "".join(
        "<tr><td>" + label + "</td>"
        + "".join(f"<td>{counters[minutes][key]}</td>" for minutes in (60, 720, 1440))
        + "</tr>"
        for key, label in bands
    )
    history_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row['source_pair']))}</td>"
        f"<td class=spread>+{_fmt(row['spread_percent'])}%</td>"
        f"<td>{html.escape(str(row['low_exchange']))}<small>{html.escape(str(row['low_market_pair']))}</small></td>"
        f"<td>{_fmt(row['low_price'])}</td>"
        f"<td>{html.escape(str(row['high_exchange']))}<small>{html.escape(str(row['high_market_pair']))}</small></td>"
        f"<td>{_fmt(row['high_price'])}</td>"
        f"<td>{_fmt_time(row['observed_at'])}</td>"
        "</tr>"
        for row in rows
    )
    if not history_rows:
        history_rows = '<tr><td colspan="7">История появится после первого пересчёта.</td></tr>'
    _classify_transfer_groups(rows_lifetimes)
    quote_groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows_lifetimes:
        transfer_state = row.get("_transfer_state", "unknown")
        quote_key = (
            row["source_market_id"], row["source_pair"], row["threshold_percent"],
            row["buy_exchange"], row["buy_market_pair"], row["sell_exchange"],
            row["sell_market_pair"], transfer_state, row.get("_transfer_group_started_at"),
        )
        group = quote_groups.setdefault(
            quote_key, {"row": row, "episodes": {}, "transfer_state": transfer_state}
        )
        episode_key = (row["threshold_percent"], row["started_at"], bool(row["is_active"]), row["ended_at"])
        existing = group["episodes"].get(episode_key)
        if existing is None:
            group["episodes"][episode_key] = row
        elif row["max_spread_percent"] > existing["max_spread_percent"]:
            existing["max_spread_percent"] = row["max_spread_percent"]

    lifetime_rows_list = []
    for group in quote_groups.values():
        row = group["row"]
        episodes = list(group["episodes"].values())
        thresholds = _fmt(episodes[0]["threshold_percent"]) + "%"
        spreads = "<br>".join(
            f"+{_fmt(item['spread_percent'])}%<small>Максимум: +{_fmt(item['max_spread_percent'])}%</small>"
            for item in episodes
        )
        starts = "<br>".join(_fmt_time(item["started_at"]) for item in episodes)
        states = "<br>".join(
            (
                f"Активна · последняя проверка {_fmt_time(item['last_confirmed_at'])}"
                if item["is_active"]
                else _fmt_time(item["ended_at"])
            )
            for item in episodes
        )
        duration_cells = []
        for item in episodes:
            is_active = bool(item["is_active"])
            seconds = (
                _elapsed_seconds_since(item["started_at"])
                if is_active
                else int(item["duration_seconds"] or 0)
            )
            live_attrs = f' class="live-duration" data-elapsed-seconds="{seconds}"' if is_active else ""
            duration_cells.append(f'<span{live_attrs}>{seconds} сек.</span>')
        durations = "<br>".join(duration_cells)
        buys = "<br>".join(
            _market_cell(item["buy_exchange"], item["buy_market_pair"], item["buy_price"])
            for item in episodes
        )
        sells = "<br>".join(
            _market_cell(item["sell_exchange"], item["sell_market_pair"], item["sell_price"])
            for item in episodes
        )
        transfer_state = group["transfer_state"]
        episode_count = row.get("_transfer_episode_count", 1)
        episode_word = "эпизод" if episode_count == 1 else "эпизода" if episode_count < 5 else "эпизодов"
        total_seconds = row.get("_transfer_total_seconds", 0)
        total_minutes = total_seconds / 60
        total_duration = f"{total_seconds} сек. ({total_minutes:.1f} мин.)"
        transfer_labels = {
            "passed": f"Успела: {total_duration} из {row['_transfer_expected_seconds']:.0f} сек. ({episode_count} {episode_word})",
            "failed": f"Не успела: {total_duration} из {row['_transfer_expected_seconds']:.0f} сек. ({episode_count} {episode_word})",
            "pending": f"Проверяется: {total_duration} из {row['_transfer_expected_seconds']:.0f} сек. ({episode_count} {episode_word})",
            "unknown": "Нет данных",
        }
        lifetime_rows_list.append(
            f'<tr data-transfer-state="{transfer_state}">'
            f"<td>{html.escape(str(row['source_pair']))}</td>"
            f"<td>{thresholds}</td><td>{spreads}</td>"
            f"<td>{buys}</td>"
            f"<td>{sells}</td>"
            f"<td>{starts}</td><td>{states}</td><td>{durations}</td>"
            f"<td>{transfer_labels[transfer_state]}</td></tr>"
        )
    lifetime_rows = "".join(lifetime_rows_list)
    if not lifetime_rows:
        lifetime_rows = '<tr><td colspan="9">Ожидаются подтверждения от WebSocket-стаканов.</td></tr>'
    refreshed_at = datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S")
    page_title = "Активные ситуации" if active_view else "История возможностей"
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<style>
  :root {{ color-scheme: dark; font-family: system-ui, sans-serif; background: #0b1220; color: #e5e7eb; }}
  body {{ margin: 0; }} header {{ padding: 24px 4vw; background: #111b2e; border-bottom: 1px solid #26334a; }}
  main {{ padding: 24px 4vw; }} h1 {{ margin-top: 0; }} nav {{ display: flex; gap: 10px; margin-top: 14px; }}
  nav a {{ padding: 8px 12px; border: 1px solid #334155; border-radius: 8px; color: #7dd3fc; text-decoration: none; }}
  nav a.active {{ background: #1e3a5f; }} .muted, small {{ color: #9ca3af; }} small {{ display: block; margin-top: 4px; }}
  .active-view .counter-section {{ display: none; }}
  .table-wrap {{ overflow-x: auto; margin-bottom: 24px; border: 1px solid #26334a; border-radius: 10px; }}
  table {{ width: 100%; border-collapse: collapse; min-width: 760px; background: #111b2e; }}
  th, td {{ padding: 11px 14px; text-align: left; border-bottom: 1px solid #26334a; white-space: nowrap; }}
  th {{ color: #9fb4d0; font-size: .82rem; text-transform: uppercase; }} .spread {{ color: #67e8a5; font-weight: 700; }}
  a.market-link {{ color: inherit; text-decoration: none; }} a.market-link:hover {{ color: #7dd3fc; text-decoration: underline; }}
  tr[hidden] {{ display: none; }} .filters {{ margin: 0 0 14px; padding: 12px 14px; border: 1px solid #26334a; border-radius: 10px; background: #111b2e; }}
  .filters input {{ accent-color: #38bdf8; vertical-align: middle; }} .filters label {{ cursor: pointer; }}
</style></head><body>
<header><h1>{page_title}</h1><div class="muted">{('Текущие подтверждённые ситуации · список обновляется автоматически' if active_view else f'Последние 500 наблюдений · данные хранятся 30 дней · обновлено {refreshed_at}')}</div>
<nav><a href="/">Возможности</a><a class="{'active' if active_view else ''}" href="/situations">Активные ситуации</a><a class="{'active' if not active_view else ''}" href="/history">История</a><a href="/trader">Трейдер</a></nav></header>
<main class="{'active-view' if active_view else ''}"><section class="counter-section"><h2>Подтверждённые возможности за период</h2><div class="muted counter-note">Суммируется подтверждённое время эпизодов одного маршрута с паузами до 60 секунд; паузы не учитываются.</div><div class="table-wrap"><table><thead><tr><th>Порог спреда</th><th>1 час</th><th>12 часов</th><th>24 часа</th></tr></thead>
<tbody>{counter_rows}</tbody></table></div>
</section><h2>{'Активные ситуации и время жизни' if active_view else 'Время жизни подтверждённых возможностей'}</h2><div class="filters"><label><input id="hide-transfer-failed" type="checkbox" checked> Скрывать возможности, которые не пережили время перевода</label></div><div class="table-wrap"><table><thead><tr><th>Пара</th><th>Порог</th><th>Текущий спред</th><th>Купить</th><th>Продать</th><th>Подтверждена с (Киев)</th><th>Состояние / завершена (Киев)</th><th>Длительность эпизода</th><th>Суммарное время для перевода</th></tr></thead>
<tbody id="lifetimes-body">{lifetime_rows}</tbody></table></div>
</main><script>
function updateLiveDurations() {{
  const now = performance.now();
  document.querySelectorAll('.live-duration[data-elapsed-seconds]').forEach(cell => {{
    const elapsed = Number(cell.dataset.elapsedSeconds);
    if (!Number.isFinite(elapsed)) return;
    if (!Number.isFinite(Number(cell.dataset.timerBaseMs))) {{
      cell.dataset.timerBaseMs = String(now);
      return;
    }}
    let seconds = elapsed + Math.floor((now - Number(cell.dataset.timerBaseMs)) / 1000);
    const hours = Math.floor(seconds / 3600); seconds %= 3600;
    const minutes = Math.floor(seconds / 60); seconds %= 60;
    cell.textContent = (hours ? hours + ' ч ' : '') + (minutes ? minutes + ' мин ' : '') + seconds + ' сек.';
  }});
}}
updateLiveDurations(); setInterval(updateLiveDurations, 1000);
async function refreshLifetimeHistory() {{
  try {{
    const response = await fetch('{"/api/active-situations" if active_view else "/api/history-lifetimes"}', {{cache: 'no-store'}});
    if (!response.ok) return;
    const documentCopy = new DOMParser().parseFromString(await response.text(), 'text/html');
    const updatedBody = documentCopy.getElementById('lifetimes-body');
    const currentBody = document.getElementById('lifetimes-body');
    if (updatedBody && currentBody) currentBody.replaceChildren(...updatedBody.cloneNode(true).childNodes);
    renderTransferFilter();
  }} catch (_) {{}}
}}
const transferFilter = document.getElementById('hide-transfer-failed');
if (transferFilter) {{
  const savedTransferFilter = localStorage.getItem('hide-transfer-failed');
  if (savedTransferFilter !== null) transferFilter.checked = savedTransferFilter === 'true';
  transferFilter.addEventListener('change', () => {{
    localStorage.setItem('hide-transfer-failed', String(transferFilter.checked));
    renderTransferFilter();
  }});
}}
function renderTransferFilter() {{
  if (!transferFilter) return;
  document.querySelectorAll('#lifetimes-body tr[data-transfer-state]').forEach(row => {{
    row.hidden = transferFilter.checked && row.dataset.transferState === 'failed';
  }});
}}
renderTransferFilter();
setInterval(refreshLifetimeHistory, 5000);
</script></body></html>"""


def _render_page(rows: list[dict[str, Any]], interval_minutes: int) -> str:
    now = datetime.utcnow()
    stale_after = timedelta(minutes=max(1, interval_minutes * 2))
    distinct_positions = len({row["source_market_id"] for row in rows})
    table_rows = []
    for row in rows:
        last_updated = row["last_updated"]
        is_fresh = bool(last_updated and now - last_updated <= stale_after)
        freshness = "Актуальна" if is_fresh else "Устарела"
        freshness_class = "fresh" if is_fresh else "stale"
        source_pair = row["source_pair"] or f'{row["base_symbol"]}/{row["source_quote"]}'
        spread = Decimal(str(row["price_difference_percent"]))
        band = "low" if spread < CALCULATOR_SPREAD_LEVELS[1] else (
            "medium" if spread < CALCULATOR_SPREAD_LEVELS[2] else "high"
        )
        elapsed_seconds = _elapsed_seconds_since(row["started_at"])
        table_rows.append(
            f'<tr data-source-id="{html.escape(str(row["source_market_id"]))}" data-band="{band}">'
            f"<td>{html.escape(str(source_pair))}<small>ID {html.escape(str(row['source_market_id']))}</small></td>"
            f"<td class=spread>+{_fmt(row['price_difference_percent'])}%</td>"
            f"<td>{_market_link(row['exchange_name'], row['venue_pair'], row['exchange_name'])}</td>"
            f"<td>{_market_link(row['exchange_name'], row['venue_pair'], row['venue_pair'] or '—')}</td>"
            f"<td class=price>{_fmt(row['price'])}</td>"
            f"<td>{_market_link(row['sell_exchange'], row['sell_market_pair'], row['sell_exchange'])}</td>"
            f"<td>{_market_link(row['sell_exchange'], row['sell_market_pair'], row['sell_market_pair'] or '—')}</td>"
            f"<td class=price>{_fmt(row['sell_price'])}</td>"
            f'<td><span class="{freshness_class}">{freshness}</span>'
            f"<small>Последняя котировка: {_fmt_time(last_updated)}</small></td>"
            f"<td>{_fmt_time(row['started_at'])}</td>"
        f'<td><span class="live-duration" data-elapsed-seconds="{elapsed_seconds}">{elapsed_seconds} сек.</span></td>'
            "</tr>"
        )

    filter_controls = (
        f'<section class="filters"><strong>Показывать спред:</strong>'
        f'<label><input type="checkbox" data-band-filter="low" checked> {_fmt(CALCULATOR_SPREAD_LEVELS[0])}–{_fmt(CALCULATOR_SPREAD_LEVELS[1])}%</label>'
        f'<label><input type="checkbox" data-band-filter="medium" checked> {_fmt(CALCULATOR_SPREAD_LEVELS[1])}–{_fmt(CALCULATOR_SPREAD_LEVELS[2])}%</label>'
        f'<label><input type="checkbox" data-band-filter="high" checked> от {_fmt(CALCULATOR_SPREAD_LEVELS[2])}%</label>'
        '</section>'
    )
    if not table_rows:
        body = '<div class="empty">Пока нет возможностей, подтверждённых WebSocket-стаканами.</div>'
    else:
        body = (
            '<div class="table-wrap"><table><thead><tr>'
            '<th>Исходная пара</th><th>Подтверждённый спред</th>'
            '<th>Купить на</th><th>Пара</th><th>Ask</th><th>Продать на</th><th>Пара</th><th>Bid</th>'
            '<th>Котировки</th><th>Возможность с (Киев)</th><th>Длительность</th>'
            '</tr></thead><tbody>' + "".join(table_rows) + "</tbody></table></div>"
        )

    body = filter_controls + '<div id="filter-summary" class="filter-summary"></div>' + body
    if table_rows:
        body += '<div id="no-filtered-rows" class="empty" hidden>Нет позиций в выбранных диапазонах.</div>'

    refreshed_at = datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S ")
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Арбитражный скринер</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, sans-serif; background: #0b1220; color: #e5e7eb; }}
    body {{ margin: 0; }}
    header {{ position: relative; padding: 24px 4vw; padding-right: 290px; background: #111b2e; border-bottom: 1px solid #26334a; }}
    h1 {{ margin: 0 0 8px; font-size: 1.5rem; }}
    .muted, small {{ color: #9ca3af; }}
    main {{ padding: 24px 4vw; }}
    .summary {{ margin-bottom: 16px; color: #cbd5e1; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid #26334a; border-radius: 10px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 980px; background: #111b2e; }}
    th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid #26334a; white-space: nowrap; }}
    th {{ color: #9fb4d0; font-size: .82rem; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    small {{ display: block; margin-top: 4px; font-size: .75rem; }}
    .spread, .price {{ font-variant-numeric: tabular-nums; }}
    .spread {{ color: #67e8a5; font-weight: 700; }}
    .price {{ font-weight: 600; }}
    .fresh {{ color: #67e8a5; }} .stale {{ color: #fbbf24; }}
    a {{ color: #7dd3fc; }}
    .empty {{ padding: 28px; border: 1px solid #26334a; border-radius: 10px; background: #111b2e; }}
    .worker-status {{ position: absolute; top: 20px; right: 4vw; min-width: 210px; padding: 10px 14px; border: 1px solid #334155; border-radius: 10px; background: #0b1220; text-align: right; }}
    .worker-status strong {{ display: block; font-size: .9rem; }} .worker-status small {{ margin-top: 3px; }}
    .worker-running {{ color: #fbbf24; }} .worker-waiting {{ color: #67e8a5; }} .worker-error {{ color: #f87171; }}
    .filters {{ display: flex; flex-wrap: wrap; gap: 16px; align-items: center; margin: 0 0 16px; padding: 12px 14px; border: 1px solid #26334a; border-radius: 10px; background: #111b2e; }}
    .filters label {{ cursor: pointer; white-space: nowrap; }} .filters input {{ accent-color: #38bdf8; vertical-align: middle; }}
    .filter-summary {{ margin: -8px 0 12px; color: #9ca3af; font-size: .85rem; }}
    tr[hidden] {{ display: none; }}
    nav {{ display: flex; gap: 10px; margin-top: 14px; }}
    nav a {{ padding: 8px 12px; border: 1px solid #334155; border-radius: 8px; color: #7dd3fc; text-decoration: none; }}
    nav a.active {{ background: #1e3a5f; }}
    @media (max-width: 700px) {{ header {{ padding-right: 4vw; padding-bottom: 95px; }} .worker-status {{ top: auto; bottom: 16px; left: 4vw; right: auto; text-align: left; }} }}
  </style>
</head>
<body>
  <nav><a class="active" href="/">Возможности</a><a href="/situations">Активные ситуации</a><a href="/history">История</a><a href="/trader">Трейдер</a></nav>
  <header><h1>Арбитражный скринер</h1>
  <div id="worker-status" class="worker-status"><strong>Проверяю обновление…</strong><small></small></div>
  <div class="muted">Длительность активных возможностей обновляется каждую секунду · данные пересчитываются по расписанию</div></header>
  <main><div class="summary">Позиций: {distinct_positions} · Биржевых строк: {len(rows)} · Страница обновлена: {refreshed_at}</div>
  {body}</main>
<script>
function updateLiveDurations() {{
  const now = performance.now();
  document.querySelectorAll('.live-duration[data-elapsed-seconds]').forEach(cell => {{
    const elapsed = Number(cell.dataset.elapsedSeconds);
    if (!Number.isFinite(elapsed)) return;
    if (!Number.isFinite(Number(cell.dataset.timerBaseMs))) {{
      cell.dataset.timerBaseMs = String(now);
      return;
    }}
    let seconds = elapsed + Math.floor((now - Number(cell.dataset.timerBaseMs)) / 1000);
    const hours = Math.floor(seconds / 3600);
    seconds %= 3600;
    const minutes = Math.floor(seconds / 60);
    seconds %= 60;
    cell.textContent = (hours ? hours + ' ч ' : '') + (minutes ? minutes + ' мин ' : '') + seconds + ' сек.';
  }});
}}
updateLiveDurations();
setInterval(updateLiveDurations, 1000);
const spreadFilters = [...document.querySelectorAll('[data-band-filter]')];
spreadFilters.forEach(filter => {{
  const saved = localStorage.getItem('spread-filter-' + filter.dataset.bandFilter);
  if (saved !== null) filter.checked = saved === 'true';
}});
function renderSpreadFilters() {{
  const enabled = new Set(spreadFilters.filter(filter => filter.checked).map(filter => filter.dataset.bandFilter));
  const rows = [...document.querySelectorAll('tbody tr[data-band]')];
  rows.forEach(row => {{ row.hidden = !enabled.has(row.dataset.band); }});
  const visibleRows = rows.filter(row => !row.hidden);
  const positions = new Set(visibleRows.map(row => row.dataset.sourceId));
  document.getElementById('filter-summary').textContent = 'Показано позиций: ' + positions.size + ' · биржевых строк: ' + visibleRows.length;
  const emptyState = document.getElementById('no-filtered-rows');
  if (emptyState) emptyState.hidden = rows.some(row => !row.hidden);
}}
spreadFilters.forEach(filter => filter.addEventListener('change', () => {{
  localStorage.setItem('spread-filter-' + filter.dataset.bandFilter, String(filter.checked));
  renderSpreadFilters();
}}));
renderSpreadFilters();
async function refreshWorkerStatus() {{
  const panel = document.getElementById('worker-status');
  try {{
    const response = await fetch('/api/status', {{cache: 'no-store'}});
    const state = await response.json();
    panel.className = 'worker-status worker-' + (state.status || 'waiting');
    let title = 'Нет данных о процессе';
    let detail = '';
    if (state.status === 'running') {{ title = 'Идёт обновление'; detail = state.phase || ''; }}
    else if (state.status === 'waiting' || state.status === 'error') {{
      title = state.status === 'error' ? 'Последний цикл завершился с ошибкой' : 'Следующее обновление через';
      if (state.next_run_at) {{
        const seconds = Math.max(0, Math.floor((new Date(state.next_run_at.replace(' ', 'T') + 'Z') - Date.now()) / 1000));
        detail = Math.floor(seconds / 60) + ' мин ' + (seconds % 60) + ' сек';
      }} else {{ detail = state.phase || ''; }}
    }}
    panel.querySelector('strong').textContent = title;
    panel.querySelector('small').textContent = detail;
  }} catch (_) {{
    panel.className = 'worker-status worker-error';
    panel.querySelector('strong').textContent = 'Статус недоступен';
    panel.querySelector('small').textContent = 'Проверка повторится автоматически';
  }}
}}
refreshWorkerStatus();
setInterval(refreshWorkerStatus, 1000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/trader":
            try:
                account, stats, trades = _load_trader_dashboard()
                body = _render_trader_page(account, stats, trades).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                logger.exception("Не удалось загрузить страницу трейдера")
                self.send_error(500, "Не удалось получить данные трейдера из БД")
            return
        if path in ("/api/history-lifetimes", "/api/active-situations"):
            try:
                active_view = path == "/api/active-situations"
                empty_counters = {minutes: {"low_band": 0, "middle_band": 0, "high_band": 0} for minutes in (60, 720, 1440)}
                body = _render_history([], empty_counters, _load_lifetimes(active_view), active_view).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                logger.exception("Не удалось обновить завершённые возможности")
                self.send_error(500, "Не удалось обновить историю")
            return
        if path in ("/history", "/situations"):
            try:
                active_view = path == "/situations"
                if active_view:
                    rows, counters = [], {minutes: {"low_band": 0, "middle_band": 0, "high_band": 0} for minutes in (60, 720, 1440)}
                else:
                    rows, counters = _load_history()
                rows_lifetimes = _load_lifetimes(active_view)
                body = _render_history(rows, counters, rows_lifetimes, active_view).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                logger.exception("Не удалось загрузить историю возможностей")
                self.send_error(500, "Не удалось получить историю из БД")
            return
        if path == "/api/status":
            try:
                body = json.dumps(_load_worker_status()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                logger.exception("Не удалось получить состояние worker")
                self.send_error(500, "Не удалось получить состояние процесса")
            return
        if path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if path != "/":
            self.send_error(404)
            return
        try:
            rows, interval_minutes = _load_opportunities()
            body = _render_page(rows, interval_minutes).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            logger.exception("Не удалось загрузить данные для веб-страницы")
            self.send_error(500, "Не удалось получить данные из БД")

    def log_message(self, format_string: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format_string % args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    connection = connect_db()
    try:
        initialize_database(connection)
    finally:
        connection.close()
    port = int(os.getenv("WEB_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    logger.info("Веб-страница доступна на порту %s", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
