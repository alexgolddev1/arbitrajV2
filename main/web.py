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

from config import BUY_PRICE_MUST_BE_LOWER, CALCULATOR_SPREAD_LEVELS, SCREENER_QUOTE_SYMBOLS_CSV
from database import connect_db, finalize_stale_opportunity_lifetimes, initialize_database, read_config

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo("Europe/Kyiv")
LIFETIME_EPISODE_GAP_SECONDS = 60


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    result = format(value, "f") if hasattr(value, "as_tuple") else str(value)
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return html.escape(result)


def _fmt_fixed(value: Any, places: int) -> str:
    """Format dashboard numbers at a fixed display precision without changing stored values."""
    if value is None:
        return "—"
    return html.escape(f"{Decimal(str(value)):.{places}f}")


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
                "AND (%s = '' OR FIND_IN_SET(UPPER(source.quote_symbol), %s) > 0) "
                f"AND life.buy_price {price_direction} life.sell_price "
                "ORDER BY life.spread_percent DESC, source.market_id",
                (CALCULATOR_SPREAD_LEVELS[0], SCREENER_QUOTE_SYMBOLS_CSV, SCREENER_QUOTE_SYMBOLS_CSV),
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
                "sell_exchange, sell_market_pair, expected_sell_price, stop_loss_price, final_sell_price, quantity, "
                "buy_cost_usd, realized_sell_proceeds_usd, buy_fee_usd, sell_fee_usd, "
                "sold_quantity, sell_proceeds_accumulated_usd, "
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
        sold_quantity = Decimal(str(trade.get("sold_quantity") or 0))
        remaining_quantity = Decimal(str(trade.get("remaining_quantity") or 0))
        accumulated_proceeds = Decimal(str(trade.get("sell_proceeds_accumulated_usd") or 0))
        average_execution_price = (
            accumulated_proceeds / sold_quantity if sold_quantity > 0 else None
        )
        profit = trade["realized_profit_usd"] if realized else trade["expected_profit_usd"]
        profit_class = "positive" if profit is not None and Decimal(str(profit)) >= 0 else "negative"
        sale_price = trade["final_sell_price"] if realized else trade["expected_sell_price"]
        route_label = {"confirmed": "Подтверждён", "unconfirmed": "Предполагаемый", "legacy": "Старая запись"}.get(
            trade["route_status"], html.escape(str(trade["route_status"]))
        )
        trade_rows.append(
            f'<tr class="trade-row" tabindex="0" '
            f'data-pair="{html.escape(str(trade["source_pair"]), quote=True)}" '
            f'data-average="{_fmt_fixed(average_execution_price, 8)}" '
            f'data-sold="{_fmt_fixed(sold_quantity, 8)}" '
            f'data-remaining="{_fmt_fixed(remaining_quantity, 8)}">'
            f"<td>{html.escape(str(trade['source_pair']))}</td>"
            f"<td>{_market_cell(trade['buy_exchange'], trade['buy_market_pair'], trade['buy_price'])}</td>"
            f"<td>{_market_cell(trade['sell_exchange'], trade['sell_market_pair'], sale_price)}</td>"
            f"<td>TP {_fmt(trade['expected_sell_price'])}<small>SL {_fmt(trade['stop_loss_price'])}</small></td>"
            f"<td>{_fmt_fixed(trade['quantity'], 8)}</td>"
            f"<td>{_fmt_fixed(trade['buy_cost_usd'], 3)}</td>"
            f"<td>{_fmt_fixed(trade['realized_sell_proceeds_usd'] if realized else None, 3)}</td>"
            f"<td>{_fmt_fixed(trade['buy_fee_usd'], 3)} + {_fmt_fixed(trade['sell_fee_usd'] if realized else None, 3)}</td>"
            f'<td class="{profit_class}">{_fmt_fixed(profit, 3)} USD'
            f'{"<small>накопленная, позиция открыта</small>" if realized and not settled else ""}'
            f'{"<small>ожидаемая до резерва; после резерва " + _fmt_fixed(trade["expected_profit_after_reserve_usd"], 3) + " USDT</small>" if not realized else ""}</td>'
            f"<td>{route_label}<small>Перевод: {_fmt(trade['transfer_fee_amount'])} {html.escape(str(trade['transfer_fee_currency']))}</small></td>"
            f"<td>{_fmt_fixed(remaining_quantity, 8)}</td>"
            f"<td><span class=\"status status-{html.escape(str(trade['status']))}\">"
            f"{status_labels.get(trade['status'], html.escape(str(trade['status'])))}</span></td>"
            f"<td>{_fmt_time(trade['created_at'])}"
            f"<small>План продажи: {_fmt_time(trade['planned_sell_at'])}</small>"
            f"<small>Последняя продажа: {_fmt_time(trade['actual_sell_at'])}</small>"
            f"<small>{'Завершена: ' + _fmt_time(trade['settled_at']) if settled else 'Следующая проверка: ' + _fmt_time(trade['settle_after_utc'])}</small></td>"
            "</tr>"
        )
    if not trade_rows:
        trade_rows.append('<tr><td colspan="13">Трейдер ещё не входил в сделки.</td></tr>')

    balance = _fmt_fixed(account["balance_usd"], 3) if account else "—"
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
#trade-detail-modal {{ display:none; position:fixed; z-index:1000; max-width:320px; padding:14px 16px; border:1px solid #475569; border-radius:10px; background:#0f172a; box-shadow:0 12px 32px #0009; pointer-events:none; line-height:1.5; }}
#trade-detail-modal.visible {{ display:block; }} #trade-detail-modal strong {{ display:block; margin-bottom:7px; color:#7dd3fc; }}
.trade-row {{ cursor:help; }} .trade-row:focus {{ outline:2px solid #38bdf8; outline-offset:-2px; }}
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
<div class="card"><span>Реализованный результат</span><strong class="{'positive' if Decimal(str(stats.get('net_profit') or 0)) >= 0 else 'negative'}">{_fmt_fixed(stats.get('net_profit') or 0, 3)} USD</strong></div>
<div class="card"><span>Уплаченные комиссии</span><strong>{_fmt_fixed(stats.get('total_fees') or 0, 3)} USD</strong></div>
</section><h2>Сделки трейдера</h2><div class="table-wrap"><table><thead><tr>
<th>Пара</th><th>Купил</th><th>Продал / цена TP</th><th>Уровни TP / SL</th><th>Количество</th><th>Затраты на покупку</th><th>Выручка</th><th>Комиссии buy + sell</th><th>Результат</th><th>Маршрут и комиссия перевода</th><th>Остаток монет</th><th>Статус</th><th>Время (Киев)</th>
</tr></thead><tbody>{''.join(trade_rows)}</tbody></table></div>
<div id="trade-detail-modal" role="tooltip" aria-hidden="true"><strong data-detail="pair"></strong>
<div>Средняя цена исполнения: <span data-detail="average"></span></div>
<div>Продано монет: <span data-detail="sold"></span></div>
<div>Остаток монет: <span data-detail="remaining"></span></div></div></main>
<script>
const tradeDetail = document.getElementById('trade-detail-modal');
function positionTradeDetail(event) {{
  const offset = 14, margin = 8;
  const box = tradeDetail.getBoundingClientRect();
  const left = Math.min(event.clientX + offset, window.innerWidth - box.width - margin);
  const top = Math.min(event.clientY + offset, window.innerHeight - box.height - margin);
  tradeDetail.style.left = Math.max(margin, left) + 'px';
  tradeDetail.style.top = Math.max(margin, top) + 'px';
}}
function showTradeDetail(row, event) {{
  tradeDetail.querySelector('[data-detail="pair"]').textContent = row.dataset.pair;
  tradeDetail.querySelector('[data-detail="average"]').textContent = row.dataset.average;
  tradeDetail.querySelector('[data-detail="sold"]').textContent = row.dataset.sold;
  tradeDetail.querySelector('[data-detail="remaining"]').textContent = row.dataset.remaining;
  tradeDetail.classList.add('visible');
  tradeDetail.setAttribute('aria-hidden', 'false');
  positionTradeDetail(event);
}}
function hideTradeDetail() {{
  tradeDetail.classList.remove('visible');
  tradeDetail.setAttribute('aria-hidden', 'true');
}}
document.querySelectorAll('.trade-row').forEach(row => {{
  row.addEventListener('mouseenter', event => showTradeDetail(row, event));
  row.addEventListener('mousemove', positionTradeDetail);
  row.addEventListener('mouseleave', hideTradeDetail);
  row.addEventListener('focus', () => {{
    const rect = row.getBoundingClientRect();
    showTradeDetail(row, {{clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2}});
  }});
  row.addEventListener('blur', hideTradeDetail);
}});
</script></body></html>"""


def _aggregate_lifetime_groups(
    lifetime_rows: list[dict[str, Any]],
) -> None:
    """Attach summed confirmed time to episodes on the same route and threshold.

    Confirmed spread seconds accumulate across episodes separated by at most
    LIFETIME_EPISODE_GAP_SECONDS. Gaps themselves do not add to the elapsed opportunity time.
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

    for key, episodes in route_groups.items():
        episodes.sort(key=lambda item: item["started_at"])
        clusters: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        previous_end: datetime | None = None
        for row in episodes:
            start = row["started_at"]
            if row["is_active"]:
                end = now
                duration = max(0.0, (end - start).total_seconds())
            else:
                duration = float(row.get("duration_seconds") or 0)
                end = start + timedelta(seconds=duration)
            row["_confirmed_seconds"] = duration
            gap = (start - previous_end).total_seconds() if previous_end else None
            if current and gap is not None and gap > LIFETIME_EPISODE_GAP_SECONDS:
                clusters.append(current)
                current = []
            current.append(row)
            previous_end = max(previous_end, end) if previous_end else end
        if current:
            clusters.append(current)

        for cluster in clusters:
            total_seconds = round(sum(row["_confirmed_seconds"] for row in cluster), 3)
            for row in cluster:
                row["_lifetime_total_seconds"] = total_seconds
                row["_lifetime_episode_count"] = len(cluster)


def _count_trader_positions(
    positions: list[dict[str, Any]],
    spread_levels: tuple[Decimal, ...],
    now: datetime | None = None,
) -> dict[int, dict[str, int]]:
    """Count created trader positions by signal threshold and creation window."""
    current_time = now or datetime.utcnow()
    counters = {
        minutes: {f"threshold_{index}": 0 for index, _ in enumerate(spread_levels)}
        for minutes in (60, 720, 1440)
    }
    cutoffs = {
        minutes: current_time - timedelta(minutes=minutes)
        for minutes in counters
    }
    for position in positions:
        position_threshold = Decimal(str(position["threshold_percent"]))
        for index, threshold in enumerate(spread_levels):
            if position_threshold != threshold:
                continue
            key = f"threshold_{index}"
            for minutes, cutoff in cutoffs.items():
                if position["created_at"] >= cutoff:
                    counters[minutes][key] += 1
    return counters


def _history_live_button(row: dict[str, Any]) -> str:
    attrs = {
        "pair": row["source_pair"],
        "buy_exchange": row["buy_exchange"],
        "buy_pair": row["buy_market_pair"],
        "sell_exchange": row["sell_exchange"],
        "sell_pair": row["sell_market_pair"],
    }
    data = " ".join(
        f'data-{key.replace("_", "-")}="{html.escape(str(value or ""), quote=True)}"'
        for key, value in attrs.items()
    )
    label = html.escape(str(row["source_pair"]), quote=True)
    return f'<button class="live-pair-button" type="button" title="Открыть цены в реальном времени" aria-label="Открыть котировки {label}" {data}>⋮</button>'


def _load_history() -> tuple[list[dict[str, Any]], dict[int, dict[str, int]]]:
    connection = connect_db()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT history_id, source_pair, spread_percent, low_exchange, low_market_pair, low_price, "
                "high_exchange, high_market_pair, high_price, observed_at "
                "FROM opportunity_history AS history "
                "JOIN market_pairs AS source ON source.market_id = history.source_market_id "
                "WHERE (%s = '' OR FIND_IN_SET(UPPER(source.quote_symbol), %s) > 0) "
                "ORDER BY observed_at DESC, history_id DESC LIMIT 500",
                (SCREENER_QUOTE_SYMBOLS_CSV, SCREENER_QUOTE_SYMBOLS_CSV),
            )
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT CAST(JSON_UNQUOTE(JSON_EXTRACT(sig.payload, '$.threshold_percent')) "
                "AS DECIMAL(10, 4)) AS threshold_percent, settlement.created_at "
                "FROM trader_training_settlements AS settlement "
                "JOIN trader_signals AS sig ON sig.signal_id = settlement.signal_id "
                "WHERE settlement.created_at >= TIMESTAMPADD(MINUTE, -1440, UTC_TIMESTAMP())"
            )
            counters = _count_trader_positions(cursor.fetchall(), CALCULATOR_SPREAD_LEVELS)
        return rows, counters
    finally:
        connection.close()


def _load_lifetimes(is_active: bool = False) -> list[dict[str, Any]]:
    if is_active:
        finalize_stale_opportunity_lifetimes(180)
    connection = connect_db()
    try:
        price_direction = "<" if BUY_PRICE_MUST_BE_LOWER else ">"
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT lifetime_id, source_market_id, source_pair, threshold_percent, spread_percent, max_spread_percent, "
                "buy_exchange, buy_market_pair, buy_price, sell_exchange, sell_market_pair, sell_price, "
                "started_at, last_confirmed_at, ended_at, duration_seconds, is_active "
                "FROM opportunity_lifetimes AS life "
                "JOIN market_pairs AS source ON source.market_id = life.source_market_id "
                "WHERE life.is_active = %s AND (%s = '' OR FIND_IN_SET(UPPER(source.quote_symbol), %s) > 0) "
                f"AND (is_active = 0 OR buy_price {price_direction} sell_price) "
                "ORDER BY started_at DESC, lifetime_id DESC LIMIT 500",
                (int(is_active), SCREENER_QUOTE_SYMBOLS_CSV, SCREENER_QUOTE_SYMBOLS_CSV),
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
    bands = tuple(
        (f"threshold_{index}", f"от {_fmt(threshold)}%")
        for index, threshold in enumerate(CALCULATOR_SPREAD_LEVELS)
    )
    counter_rows = "".join(
        "<tr><td>" + label + "</td>"
        + "".join(f"<td>{counters.get(minutes, {}).get(key, 0)}</td>" for minutes in (60, 720, 1440))
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
    _aggregate_lifetime_groups(rows_lifetimes)
    # Одна строка соответствует одному эпизоду: котировки и время больше не смешиваются.
    lifetime_rows_list = []
    for row in rows_lifetimes:
        threshold = _fmt(row["threshold_percent"]) + "%"
        spread_value = Decimal(str(row["spread_percent"]))
        spread = (
            f"{'+' if spread_value >= 0 else ''}{_fmt(spread_value)}%"
            f"<small>Максимум: +{_fmt(row['max_spread_percent'])}%</small>"
        )
        started = _fmt_time(row["started_at"])
        state = (
            f"Активна · последняя проверка {_fmt_time(row['last_confirmed_at'])}"
            if row["is_active"]
            else _fmt_time(row["ended_at"])
        )
        is_active = bool(row["is_active"])
        seconds = (
            _elapsed_seconds_since(row["started_at"])
            if is_active
            else float(row["duration_seconds"] or 0)
        )
        live_attrs = f' class="live-duration" data-elapsed-seconds="{seconds:.3f}"' if is_active else ""
        seconds_label = f"{seconds:.1f}".rstrip("0").rstrip(".")
        duration = f'<span{live_attrs}>{seconds_label} сек.</span>'
        episode_count = row.get("_lifetime_episode_count", 1)
        episode_word = "эпизод" if episode_count == 1 else "эпизода" if episode_count < 5 else "эпизодов"
        total_seconds = row.get("_lifetime_total_seconds", 0)
        total_minutes = total_seconds / 60
        total_seconds_label = f"{total_seconds:.1f}".rstrip("0").rstrip(".")
        total_duration = f"{total_seconds_label} сек. ({total_minutes:.1f} мин.; {episode_count} {episode_word})"
        lifetime_rows_list.append(
            "<tr>"
            f"<td>{_history_live_button(row)} {html.escape(str(row['source_pair']))}</td>"
            f"<td>{threshold}</td><td class=spread>{spread}</td>"
            f"<td>{_market_cell(row['buy_exchange'], row['buy_market_pair'], row['buy_price'])}</td>"
            f"<td>{_market_cell(row['sell_exchange'], row['sell_market_pair'], row['sell_price'])}</td>"
            f"<td>{started}</td><td>{state}</td><td>{duration}</td>"
            f"<td>{total_duration}</td></tr>"
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
  .live-pair-button {{ margin-right: 8px; width: 28px; height: 30px; padding: 0; border: 1px solid #334155; border-radius: 6px; background: #0b1220; color: #9fb4d0; font-size: 1.25rem; cursor: pointer; }}
  .live-pair-button:hover {{ border-color: #38bdf8; color: #7dd3fc; }}
  .live-modal {{ position: fixed; inset: 0; z-index: 2000; display: grid; place-items: center; padding: 16px; background: #020617cc; }} .live-modal[hidden] {{ display: none; }}
  .live-dialog {{ width: min(760px, 100%); padding: 20px; border: 1px solid #334155; border-radius: 14px; background: #111b2e; }}
  .live-head {{ display: flex; justify-content: space-between; align-items: center; }} .live-head h2 {{ margin-top: 0; }}
  .live-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }} .live-card {{ padding: 14px; border: 1px solid #26334a; border-radius: 10px; background: #0b1220; }}
  .live-card p {{ color: #9fb4d0; }} .live-card strong {{ color: #e5e7eb; }} .live-status {{ color: #fbbf24; }} .live-status.connected {{ color: #67e8a5; }} .live-status.error {{ color: #f87171; }}
</style></head><body>
<header><h1>{page_title}</h1><div class="muted">{('Текущие подтверждённые ситуации · список обновляется автоматически' if active_view else f'Последние 500 наблюдений · данные хранятся 30 дней · обновлено {refreshed_at}')}</div>
<nav><a href="/">Возможности</a><a class="{'active' if active_view else ''}" href="/situations">Активные ситуации</a><a class="{'active' if not active_view else ''}" href="/history">История</a><a href="/trader">Трейдер</a></nav></header>
<main class="{'active-view' if active_view else ''}"><section class="counter-section"><h2>Подтверждённые возможности за период</h2><div class="muted counter-note">Считаются только позиции, созданные трейдером в тренировочном режиме; период определяется временем открытия позиции.</div><div class="table-wrap"><table><thead><tr><th>Порог спреда</th><th>1 час</th><th>12 часов</th><th>24 часа</th></tr></thead>
<tbody>{counter_rows}</tbody></table></div>
</section><h2>{'Активные ситуации и время жизни' if active_view else 'Время жизни подтверждённых возможностей'}</h2><div class="table-wrap"><table><thead><tr><th>Пара</th><th>Порог</th><th>Текущий спред</th><th>Купить</th><th>Продать</th><th>Подтверждена с (Киев)</th><th>Состояние / завершена (Киев)</th><th>Длительность эпизода</th><th>Суммарное подтверждённое время</th></tr></thead>
<tbody id="lifetimes-body">{lifetime_rows}</tbody></table></div>
</main>
<div id="history-live-modal" class="live-modal" hidden><section class="live-dialog" role="dialog" aria-modal="true">
  <div class="live-head"><h2 id="history-live-title"></h2><button id="history-live-close" type="button" aria-label="Закрыть">×</button></div>
  <div class="live-grid"><article class="live-card"><h3 id="history-buy-title"></h3><p>Bid: <strong id="history-buy-bid">—</strong></p><p>Ask: <strong id="history-buy-ask">—</strong></p><div id="history-buy-status" class="live-status">Подключение…</div></article>
  <article class="live-card"><h3 id="history-sell-title"></h3><p>Bid: <strong id="history-sell-bid">—</strong></p><p>Ask: <strong id="history-sell-ask">—</strong></p><div id="history-sell-status" class="live-status">Подключение…</div></article></div><h3 id="history-live-spread">Спред: ожидаем котировки</h3>
</section></div>
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
    const hours = Math.floor(seconds / 3600); seconds %= 3600;
    const minutes = Math.floor(seconds / 60); seconds %= 60;
    cell.textContent = (hours ? hours + ' ч ' : '') + (minutes ? minutes + ' мин ' : '') + seconds + ' сек.';
  }});
}}
updateLiveDurations(); setInterval(updateLiveDurations, 1000);
const historyLiveModal = document.getElementById('history-live-modal');
let historyLiveSockets = [], historyLiveTimers = [], historyLiveQuotes = {{}};
function historyLiveStatus(side, text, state) {{ const n = document.getElementById('history-' + side + '-status'); n.textContent = text; n.className = 'live-status' + (state ? ' ' + state : ''); }}
function historyStopLive() {{ historyLiveTimers.forEach(clearInterval); historyLiveTimers = []; historyLiveSockets.forEach(s => {{ try {{ s.close(); }} catch (_) {{}} }}); historyLiveSockets = []; historyLiveQuotes = {{}}; }}
function historyReceive(side, bid, ask) {{ bid = Number(bid); ask = Number(ask); if (!(bid > 0) || !(ask > 0)) return; historyLiveQuotes[side] = {{bid, ask}}; document.getElementById('history-' + side + '-bid').textContent = bid; document.getElementById('history-' + side + '-ask').textContent = ask; historyLiveStatus(side, 'Обновлено ' + new Date().toLocaleTimeString(), 'connected'); if (historyLiveQuotes.buy && historyLiveQuotes.sell) {{ const a = historyLiveQuotes.buy.ask, b = historyLiveQuotes.sell.bid, spread = (b-a)/a*100; document.getElementById('history-live-spread').textContent = 'Спред по ask покупки и bid продажи: ' + (spread >= 0 ? '+' : '') + spread.toFixed(8) + '%'; }} }}
function historyParseMexc(buffer) {{ const bytes = new Uint8Array(buffer), state = {{i: 0}}, values = {{}}; function read() {{ let v=0, shift=0, b; do {{ b=bytes[state.i++]; v+=(b&127)*2**shift; shift+=7; }} while ((b&128) && state.i<bytes.length && shift<56); return v; }} function fields(data, save) {{ let i=0; while(i<data.length) {{ const tagState={{i}}, tag=readAt(data,tagState); i=tagState.i; const f=Math.floor(tag/8), w=tag%8; if(w===2) {{ const lenState={{i}},len=readAt(data,lenState); i=lenState.i; const part=data.subarray(i,i+len); i+=len; if(save) values[f]=new TextDecoder().decode(part); else if(f>=1&&f<=4) values[f]=new TextDecoder().decode(part); }} else if(w===0) {{ const st={{i}}; readAt(data,st); i=st.i; }} else if(w===1) i+=8; else if(w===5) i+=4; else throw Error('Неверный protobuf MEXC'); }} }} function readAt(data,st) {{ let v=0,sh=0,b; do {{ b=data[st.i++]; v+=(b&127)*2**sh; sh+=7; }} while((b&128)&&st.i<data.length&&sh<56); return v; }}
  // Вложенное поле 315 содержит bid/ask; outer protobuf также содержит бинарные служебные поля.
  let i=0; while(i<bytes.length) {{ const tag=readAt(bytes,{{i}}); const st={{i: 0}}; st.i=i; const actual=readAt(bytes,st); i=st.i; const f=Math.floor(actual/8),w=actual%8; if(w===2) {{ const st2={{i}}; const len=readAt(bytes,st2); i=st2.i; const part=bytes.subarray(i,i+len); i+=len; if(f===315) fields(part,false); }} else if(w===0) {{ const st3={{i}}; readAt(bytes,st3); i=st3.i; }} else if(w===1) i+=8; else if(w===5) i+=4; else throw Error('Неверный protobuf MEXC'); }} return values; }}
function historyConnectLive(side, exchange, pair) {{ const p=String(pair).toUpperCase().split(/[\/_-]/); if(p.length!==2) {{ historyLiveStatus(side,'Некорректная пара','error'); return; }} const symbol=p.join(''), slug=String(exchange).toLowerCase(); let url,sub;
  if(slug==='binance') url='wss://stream.binance.com:9443/ws/'+symbol.toLowerCase()+'@bookTicker';
  else if(slug==='bybit') {{ url='wss://stream.bybit.com/v5/public/spot'; sub=s=>s.send(JSON.stringify({{op:'subscribe',args:['tickers.'+symbol]}})); }}
  else if(slug==='okx') {{ url='wss://ws.okx.com:8443/ws/v5/public'; sub=s=>s.send(JSON.stringify({{op:'subscribe',args:[{{channel:'tickers',instId:p[0]+'-'+p[1]}}]}})); }}
  else if(slug==='mexc') {{ url='wss://wbs-api.mexc.com/ws'; sub=s=>{{s.binaryType='arraybuffer';s.send(JSON.stringify({{method:'SUBSCRIPTION',params:['spot@public.aggre.bookTicker.v3.api.pb@100ms@'+symbol]}}));historyLiveTimers.push(setInterval(()=>{{if(s.readyState===WebSocket.OPEN)s.send(JSON.stringify({{method:'PING'}}));}},20000));}}; }}
  else {{ historyLiveStatus(side,'Биржа не поддерживается: '+exchange,'error'); return; }}
  historyLiveStatus(side,'Подключение…'); const s=new WebSocket(url); historyLiveSockets.push(s); s.onopen=()=>{{if(historyLiveModal.hidden){{s.close();return;}}if(sub)sub(s);if(slug==='bybit'||slug==='okx')historyLiveTimers.push(setInterval(()=>{{if(s.readyState===WebSocket.OPEN)s.send(slug==='bybit'?JSON.stringify({{op:'ping'}}):'ping');}},20000));}};
  s.onmessage=e=>{{try{{if(slug==='binance'){{const d=JSON.parse(e.data);historyReceive(side,d.b,d.a);}}else if(slug==='bybit'){{const d=JSON.parse(e.data);if(d.data)historyReceive(side,d.data.bid1Price,d.data.ask1Price);}}else if(slug==='okx'){{const d=JSON.parse(e.data);if(d.data&&d.data.length)historyReceive(side,d.data[0].bidPx,d.data[0].askPx);}}else if(slug==='mexc'&&e.data instanceof ArrayBuffer){{const d=historyParseMexc(e.data);historyReceive(side,d[1],d[3]);}}}}catch(err){{historyLiveStatus(side,'Ошибка данных: '+err.message,'error');}}}}; s.onerror=()=>historyLiveStatus(side,'Ошибка подключения','error'); }}
document.addEventListener('click',e=>{{const b=e.target.closest('.live-pair-button');if(b){{historyStopLive();const d=b.dataset;historyLiveModal.hidden=false;document.getElementById('history-live-title').textContent=d.pair+' · WebSocket';document.getElementById('history-buy-title').textContent=d.buyExchange+' · '+d.buyPair;document.getElementById('history-sell-title').textContent=d.sellExchange+' · '+d.sellPair;['buy','sell'].forEach(x=>{{document.getElementById('history-'+x+'-bid').textContent='—';document.getElementById('history-'+x+'-ask').textContent='—';historyLiveStatus(x,'Подключение…');}});document.getElementById('history-live-spread').textContent='Спред: ожидаем котировки';historyConnectLive('buy',d.buyExchange,d.buyPair);historyConnectLive('sell',d.sellExchange,d.sellPair);}}if(e.target===historyLiveModal)historyCloseLive();}});
function historyCloseLive(){{historyLiveModal.hidden=true;historyStopLive();}}
document.getElementById('history-live-close').addEventListener('click',historyCloseLive);
document.addEventListener('keydown',e=>{{if(e.key==='Escape'&&!historyLiveModal.hidden)historyCloseLive();}});
async function refreshLifetimeHistory() {{
  try {{
    const response = await fetch('{"/api/active-situations" if active_view else "/api/history-lifetimes"}', {{cache: 'no-store'}});
    if (!response.ok) return;
    const documentCopy = new DOMParser().parseFromString(await response.text(), 'text/html');
    const updatedBody = documentCopy.getElementById('lifetimes-body');
    const currentBody = document.getElementById('lifetimes-body');
    if (updatedBody && currentBody) currentBody.replaceChildren(...updatedBody.cloneNode(true).childNodes);
  }} catch (_) {{}}
}}
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
        band_index = 0
        for index, threshold in enumerate(CALCULATOR_SPREAD_LEVELS[1:], start=1):
            if spread < threshold:
                break
            band_index = index
        band = f"band-{band_index}"
        elapsed_seconds = _elapsed_seconds_since(row["started_at"])
        buy_exchange = str(row["exchange_name"] or "")
        buy_pair = str(row["venue_pair"] or "")
        sell_exchange = str(row["sell_exchange"] or "")
        sell_pair = str(row["sell_market_pair"] or "")
        table_rows.append(
            f'<tr data-source-id="{html.escape(str(row["source_market_id"]))}" data-band="{band}">'
            f'<td><button class="live-pair-button" type="button" title="Открыть цены в реальном времени" '
            f'aria-label="Открыть WebSocket-котировки пары {html.escape(str(source_pair), quote=True)}" '
            f'data-pair="{html.escape(str(source_pair), quote=True)}" '
            f'data-buy-exchange="{html.escape(buy_exchange, quote=True)}" '
            f'data-buy-pair="{html.escape(buy_pair, quote=True)}" '
            f'data-sell-exchange="{html.escape(sell_exchange, quote=True)}" '
            f'data-sell-pair="{html.escape(sell_pair, quote=True)}">⋮</button></td>'
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

    filter_labels = []
    for index, threshold in enumerate(CALCULATOR_SPREAD_LEVELS):
        label = (
            f"{_fmt(threshold)}–{_fmt(CALCULATOR_SPREAD_LEVELS[index + 1])}%"
            if index + 1 < len(CALCULATOR_SPREAD_LEVELS)
            else f"от {_fmt(threshold)}%"
        )
        filter_labels.append(
            f'<label><input type="checkbox" data-band-filter="band-{index}" checked> {label}</label>'
        )
    filter_controls = (
        '<section class="filters"><strong>Показывать спред:</strong>'
        + "".join(filter_labels)
        + "</section>"
    )
    if not table_rows:
        body = '<div class="empty">Пока нет возможностей, подтверждённых WebSocket-стаканами.</div>'
    else:
        body = (
            '<div class="table-wrap"><table><thead><tr>'
            '<th aria-label="Действия"></th><th>Исходная пара</th><th>Подтверждённый спред</th>'
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
    table {{ width: 100%; border-collapse: collapse; min-width: 1020px; background: #111b2e; }}
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
    .live-pair-button {{ width: 30px; height: 34px; padding: 0; border: 1px solid #334155; border-radius: 7px; background: #0b1220; color: #9fb4d0; font-size: 1.35rem; line-height: 1; cursor: pointer; }}
    .live-pair-button:hover, .live-pair-button:focus-visible {{ border-color: #38bdf8; color: #7dd3fc; outline: none; }}
    .live-modal {{ position: fixed; inset: 0; z-index: 2000; display: grid; place-items: center; padding: 16px; background: #020617cc; }}
    .live-modal[hidden] {{ display: none; }}
    .live-dialog {{ width: min(760px, 100%); padding: 20px; border: 1px solid #334155; border-radius: 14px; background: #111b2e; box-shadow: 0 24px 80px #000a; }}
    .live-dialog-header {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 14px; }}
    .live-dialog-header h2 {{ margin: 0; }} .live-close {{ border: 0; background: transparent; color: #cbd5e1; font-size: 1.8rem; cursor: pointer; }}
    .live-quote-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
    .live-quote-card {{ padding: 14px; border: 1px solid #26334a; border-radius: 10px; background: #0b1220; }}
    .live-quote-card h3 {{ margin: 0 0 10px; }} .live-quote-card p {{ margin: 6px 0; color: #9fb4d0; }}
    .live-quote-card strong {{ color: #e5e7eb; font-variant-numeric: tabular-nums; }}
    .live-connection {{ color: #fbbf24; }} .live-connection.connected {{ color: #67e8a5; }} .live-connection.error {{ color: #f87171; }}
    .live-current-spread {{ margin-top: 14px; font-size: 1.1rem; font-weight: 700; }}
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
<div id="live-modal" class="live-modal" hidden>
  <section class="live-dialog" role="dialog" aria-modal="true" aria-labelledby="live-pair-title">
    <div class="live-dialog-header"><h2 id="live-pair-title"></h2><button id="live-close" class="live-close" type="button" aria-label="Закрыть">×</button></div>
    <div class="live-quote-grid">
      <article class="live-quote-card"><h3 id="live-buy-title"></h3><p>Bid: <strong id="live-buy-bid">—</strong></p><p>Ask: <strong id="live-buy-ask">—</strong></p><div id="live-buy-status" class="live-connection">Подключение…</div></article>
      <article class="live-quote-card"><h3 id="live-sell-title"></h3><p>Bid: <strong id="live-sell-bid">—</strong></p><p>Ask: <strong id="live-sell-ask">—</strong></p><div id="live-sell-status" class="live-connection">Подключение…</div></article>
    </div>
    <div id="live-spread" class="live-current-spread">Спред: ожидаем котировки обеих бирж</div>
  </section>
</div>
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
const liveModal = document.getElementById('live-modal');
const liveSockets = [];
const liveHeartbeatTimers = [];
let liveQuotes = {{}};
function parseLivePair(pair) {{
  const parts = String(pair || '').toUpperCase().split(/[\/_-]/);
  if (parts.length !== 2 || !/^[A-Z0-9]+$/.test(parts[0]) || !/^[A-Z0-9]+$/.test(parts[1])) return null;
  const match = parts[0].match(/^(\d+)(?=[A-Z])/);
  return {{base: parts[0], quote: parts[1], unitCount: match ? Number(match[1]) : 1}};
}}
function setLiveStatus(side, message, state) {{
  const element = document.getElementById('live-' + side + '-status');
  element.textContent = message;
  element.className = 'live-connection' + (state ? ' ' + state : '');
}}
function formatLivePrice(value) {{
  return new Intl.NumberFormat('en-US', {{maximumSignificantDigits: 14}}).format(value);
}}
function updateLiveSpread() {{
  const buy = liveQuotes.buy, sell = liveQuotes.sell;
  const output = document.getElementById('live-spread');
  if (!buy || !sell) {{ output.textContent = 'Спред: ожидаем котировки обеих бирж'; return; }}
  const buyAsk = buy.ask / buy.unitCount;
  const sellBid = sell.bid / sell.unitCount;
  const spread = (sellBid - buyAsk) / buyAsk * 100;
  output.textContent = 'Спред по ask покупки и bid продажи: ' + (spread >= 0 ? '+' : '') + spread.toFixed(8) + '%';
  output.style.color = spread >= 0 ? '#67e8a5' : '#f87171';
}}
function receiveLiveQuote(side, bid, ask, unitCount) {{
  const bidPrice = Number(bid), askPrice = Number(ask);
  if (!Number.isFinite(bidPrice) || !Number.isFinite(askPrice) || bidPrice <= 0 || askPrice <= 0) return;
  liveQuotes[side] = {{bid: bidPrice, ask: askPrice, unitCount: unitCount}};
  document.getElementById('live-' + side + '-bid').textContent = formatLivePrice(bidPrice);
  document.getElementById('live-' + side + '-ask').textContent = formatLivePrice(askPrice);
  setLiveStatus(side, 'Котировка обновлена ' + new Date().toLocaleTimeString(), 'connected');
  updateLiveSpread();
}}
function readProtoVarint(bytes, state) {{
  let value = 0, shift = 0;
  while (state.offset < bytes.length && shift <= 49) {{
    const byte = bytes[state.offset++];
    value += (byte & 127) * Math.pow(2, shift);
    if ((byte & 128) === 0) return value;
    shift += 7;
  }}
  throw new Error('Повреждённое protobuf-сообщение MEXC');
}}
function readProtoFields(bytes, callback) {{
  const state = {{offset: 0}};
  while (state.offset < bytes.length) {{
    const tag = readProtoVarint(bytes, state);
    const field = Math.floor(tag / 8), wire = tag % 8;
    if (wire === 2) {{
      const length = readProtoVarint(bytes, state);
      const end = state.offset + length;
      if (end > bytes.length) throw new Error('Обрезанное protobuf-сообщение MEXC');
      callback(field, wire, bytes.subarray(state.offset, end));
      state.offset = end;
    }} else if (wire === 0) {{
      callback(field, wire, readProtoVarint(bytes, state));
    }} else if (wire === 1) {{ state.offset += 8;
    }} else if (wire === 5) {{ state.offset += 4;
    }} else {{ throw new Error('Неизвестный protobuf-формат MEXC'); }}
  }}
}}
function decodeMexcBookTicker(buffer) {{
  let symbol = '', ticker = {{}};
  const decoder = new TextDecoder();
  readProtoFields(new Uint8Array(buffer), (field, wire, value) => {{
    if (wire !== 2) return;
    if (field === 3) symbol = decoder.decode(value);
    if (field === 315) {{
      readProtoFields(value, (tickerField, tickerWire, tickerValue) => {{
        if (tickerWire === 2 && tickerField >= 1 && tickerField <= 4) {{
          ticker[tickerField] = decoder.decode(tickerValue);
        }}
      }});
    }}
  }});
  return {{symbol: symbol, bid: ticker[1], bidSize: ticker[2], ask: ticker[3], askSize: ticker[4]}};
}}
function stopLiveSockets() {{
  liveHeartbeatTimers.forEach(timer => clearInterval(timer));
  liveHeartbeatTimers.length = 0;
  liveSockets.forEach(socket => {{ try {{ socket.close(); }} catch (_) {{}} }});
  liveSockets.length = 0;
  liveQuotes = {{}};
}}
function connectLiveExchange(side, exchange, pair) {{
  const parsed = parseLivePair(pair);
  if (!parsed) {{ setLiveStatus(side, 'Некорректная торговая пара', 'error'); return; }}
  const symbol = parsed.base + parsed.quote;
  const slug = String(exchange || '').toLowerCase();
  let url = '', subscribe = null;
  if (slug === 'binance') url = 'wss://stream.binance.com:9443/ws/' + symbol.toLowerCase() + '@bookTicker';
  else if (slug === 'bybit') {{
    url = 'wss://stream.bybit.com/v5/public/spot';
    subscribe = socket => socket.send(JSON.stringify({{op: 'subscribe', args: ['tickers.' + symbol]}}));
  }} else if (slug === 'okx') {{
    url = 'wss://ws.okx.com:8443/ws/v5/public';
    subscribe = socket => socket.send(JSON.stringify({{op: 'subscribe', args: [{{channel: 'tickers', instId: parsed.base + '-' + parsed.quote}}]}}));
  }} else if (slug === 'mexc') {{
    url = 'wss://wbs-api.mexc.com/ws';
    subscribe = socket => {{
      socket.binaryType = 'arraybuffer';
      socket.send(JSON.stringify({{method: 'SUBSCRIPTION', params: ['spot@public.aggre.bookTicker.v3.api.pb@100ms@' + symbol]}}));
      liveHeartbeatTimers.push(setInterval(() => {{ if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({{method: 'PING'}})); }}, 20000));
    }};
  }} else {{ setLiveStatus(side, 'Биржа не поддерживается: ' + exchange, 'error'); return; }}
  setLiveStatus(side, 'Подключение к ' + exchange + '…', '');
  try {{
    const socket = new WebSocket(url);
    liveSockets.push(socket);
    socket.onopen = () => {{
      if (liveModal.hidden) {{ socket.close(); return; }}
      setLiveStatus(side, 'Соединение открыто, ждём котировку…', '');
      if (subscribe) subscribe(socket);
      if (slug === 'bybit' || slug === 'okx') liveHeartbeatTimers.push(setInterval(() => {{
        if (socket.readyState === WebSocket.OPEN) socket.send(slug === 'bybit' ? JSON.stringify({{op: 'ping'}}) : 'ping');
      }}, 20000));
    }};
    socket.onmessage = event => {{
      try {{
        if (slug === 'binance') {{
          const data = JSON.parse(event.data);
          receiveLiveQuote(side, data.b, data.a, parsed.unitCount);
        }} else if (slug === 'bybit') {{
          const data = JSON.parse(event.data);
          if (data.data) receiveLiveQuote(side, data.data.bid1Price, data.data.ask1Price, parsed.unitCount);
        }} else if (slug === 'okx') {{
          const data = JSON.parse(event.data);
          if (data.data && data.data.length) receiveLiveQuote(side, data.data[0].bidPx, data.data[0].askPx, parsed.unitCount);
        }} else if (slug === 'mexc' && event.data instanceof ArrayBuffer) {{
          const data = decodeMexcBookTicker(event.data);
          receiveLiveQuote(side, data.bid, data.ask, parsed.unitCount);
        }}
      }} catch (error) {{ setLiveStatus(side, 'Ошибка разбора котировки: ' + error.message, 'error'); }}
    }};
    socket.onerror = () => setLiveStatus(side, 'Ошибка подключения к ' + exchange, 'error');
    socket.onclose = () => {{ if (!liveModal.hidden) setLiveStatus(side, 'Соединение закрыто', 'error'); }};
  }} catch (error) {{ setLiveStatus(side, 'Не удалось открыть WebSocket: ' + error.message, 'error'); }}
}}
function openLiveModal(button) {{
  stopLiveSockets();
  const pair = button.dataset.pair;
  liveModal.hidden = false;
  document.getElementById('live-pair-title').textContent = pair + ' · котировки WebSocket';
  document.getElementById('live-buy-title').textContent = button.dataset.buyExchange + ' · ' + button.dataset.buyPair;
  document.getElementById('live-sell-title').textContent = button.dataset.sellExchange + ' · ' + button.dataset.sellPair;
  ['buy', 'sell'].forEach(side => {{
    document.getElementById('live-' + side + '-bid').textContent = '—';
    document.getElementById('live-' + side + '-ask').textContent = '—';
    setLiveStatus(side, 'Подключение…', '');
  }});
  document.getElementById('live-spread').textContent = 'Спред: ожидаем котировки обеих бирж';
  document.getElementById('live-spread').style.color = '';
  connectLiveExchange('buy', button.dataset.buyExchange, button.dataset.buyPair);
  connectLiveExchange('sell', button.dataset.sellExchange, button.dataset.sellPair);
  document.getElementById('live-close').focus();
}}
function closeLiveModal() {{ liveModal.hidden = true; stopLiveSockets(); }}
document.querySelectorAll('.live-pair-button').forEach(button => button.addEventListener('click', () => openLiveModal(button)));
document.getElementById('live-close').addEventListener('click', closeLiveModal);
liveModal.addEventListener('click', event => {{ if (event.target === liveModal) closeLiveModal(); }});
document.addEventListener('keydown', event => {{ if (event.key === 'Escape' && !liveModal.hidden) closeLiveModal(); }});
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
                empty_counters = {minutes: {} for minutes in (60, 720, 1440)}
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
                    rows, counters = [], {minutes: {} for minutes in (60, 720, 1440)}
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
