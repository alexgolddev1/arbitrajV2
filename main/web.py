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
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from config import CALCULATOR_SPREAD_LEVELS
from database import connect_db, initialize_database, read_config

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo("Europe/Kyiv")


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


def _iso_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    utc_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_opportunities() -> tuple[list[dict[str, Any]], int]:
    connection = connect_db()
    try:
        config = read_config(connection)
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
                "AND life.buy_price > life.sell_price "
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
            counters: dict[int, dict[str, int]] = {}
            low, middle, high = CALCULATOR_SPREAD_LEVELS
            for minutes in (60, 720, 1440):
                cursor.execute(
                    "SELECT "
                    "COUNT(CASE WHEN threshold_percent = %s THEN 1 END) AS low_band, "
                    "COUNT(CASE WHEN threshold_percent = %s THEN 1 END) AS middle_band, "
                    "COUNT(CASE WHEN threshold_percent = %s THEN 1 END) AS high_band "
                    "FROM opportunity_lifetimes "
                    "WHERE started_at >= TIMESTAMPADD(MINUTE, -%s, UTC_TIMESTAMP())",
                    (low, middle, high, minutes),
                )
                counters[minutes] = cursor.fetchone()
        return rows, counters
    finally:
        connection.close()


def _load_lifetimes(is_active: bool = False) -> list[dict[str, Any]]:
    connection = connect_db()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT source_pair, threshold_percent, spread_percent, max_spread_percent, "
                "buy_exchange, buy_market_pair, buy_price, sell_exchange, sell_market_pair, sell_price, "
                "started_at, last_confirmed_at, ended_at, duration_seconds, is_active "
                "FROM opportunity_lifetimes WHERE is_active = %s "
                "AND buy_price > sell_price "
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
    quote_groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows_lifetimes:
        quote_key = (
            row["source_pair"], row["buy_exchange"], row["buy_market_pair"], row["buy_price"],
            row["sell_exchange"], row["sell_market_pair"], row["sell_price"],
        )
        group = quote_groups.setdefault(quote_key, {"row": row, "episodes": {}})
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
        thresholds = "<br>".join(f"{_fmt(item['threshold_percent'])}%" for item in episodes)
        spreads = "<br>".join(
            f"+{_fmt(item['spread_percent'])}%<small>Максимум: +{_fmt(item['max_spread_percent'])}%</small>"
            for item in episodes
        )
        starts = "<br>".join(_fmt_time(item["started_at"]) for item in episodes)
        states = "<br>".join("Активна" if item["is_active"] else _fmt_time(item["ended_at"]) for item in episodes)
        durations = "<br>".join(
            f'<span class="{"live-duration" if item["is_active"] else ""}" data-started-at="{_iso_utc(item["started_at"]) if item["is_active"] else ""}">{int(max(0, (datetime.utcnow() - item["started_at"]).total_seconds())) if item["is_active"] else int(item["duration_seconds"] or 0)} сек.</span>'
            for item in episodes
        )
        lifetime_rows_list.append(
            "<tr>"
            f"<td>{html.escape(str(row['source_pair']))}</td>"
            f"<td>{thresholds}</td><td>{spreads}</td>"
            f"<td>{html.escape(str(row['buy_exchange']))}<small>{html.escape(str(row['buy_market_pair']))} · {_fmt(row['buy_price'])}</small></td>"
            f"<td>{html.escape(str(row['sell_exchange']))}<small>{html.escape(str(row['sell_market_pair']))} · {_fmt(row['sell_price'])}</small></td>"
            f"<td>{starts}</td><td>{states}</td><td>{durations}</td></tr>"
        )
    lifetime_rows = "".join(lifetime_rows_list)
    if not lifetime_rows:
        lifetime_rows = '<tr><td colspan="8">Ожидаются подтверждения от WebSocket-стаканов.</td></tr>'
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
</style></head><body>
<header><h1>{page_title}</h1><div class="muted">{('Текущие подтверждённые ситуации · список обновляется автоматически' if active_view else f'Последние 500 наблюдений · данные хранятся 30 дней · обновлено {refreshed_at}')}</div>
<nav><a href="/">Возможности</a><a class="{'active' if active_view else ''}" href="/situations">Активные ситуации</a><a class="{'active' if not active_view else ''}" href="/history">История</a></nav></header>
<main class="{'active-view' if active_view else ''}"><section class="counter-section"><h2>Подтверждённые возможности за период</h2><div class="table-wrap"><table><thead><tr><th>Порог спреда</th><th>1 час</th><th>12 часов</th><th>24 часа</th></tr></thead>
<tbody>{counter_rows}</tbody></table></div>
</section><h2>{'Активные ситуации и время жизни' if active_view else 'Время жизни подтверждённых возможностей'}</h2><div class="table-wrap"><table><thead><tr><th>Пара</th><th>Порог</th><th>Текущий спред</th><th>Купить</th><th>Продать</th><th>Подтверждена с (Киев)</th><th>Состояние / завершена (Киев)</th><th>Длительность</th></tr></thead>
<tbody id="lifetimes-body">{lifetime_rows}</tbody></table></div>
</main><script>
function updateLiveDurations() {{
  const now = Date.now();
  document.querySelectorAll('.live-duration[data-started-at]').forEach(cell => {{
    const startedAt = Date.parse(cell.dataset.startedAt);
    if (!Number.isFinite(startedAt)) return;
    let seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
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
        band = "low" if spread < CALCULATOR_SPREAD_LEVELS[1] else (
            "medium" if spread < CALCULATOR_SPREAD_LEVELS[2] else "high"
        )
        table_rows.append(
            f'<tr data-source-id="{html.escape(str(row["source_market_id"]))}" data-band="{band}">'
            f"<td>{html.escape(str(source_pair))}<small>ID {html.escape(str(row['source_market_id']))}</small></td>"
            f"<td class=spread>+{_fmt(row['price_difference_percent'])}%</td>"
            f"<td>{html.escape(str(row['exchange_name']))}</td>"
            f"<td>{html.escape(str(row['venue_pair'] or '—'))}</td>"
            f"<td class=price>{_fmt(row['price'])}</td>"
            f"<td>{html.escape(str(row['sell_exchange']))}</td>"
            f"<td>{html.escape(str(row['sell_market_pair'] or '—'))}</td>"
            f"<td class=price>{_fmt(row['sell_price'])}</td>"
            f'<td><span class="{freshness_class}">{freshness}</span>'
            f"<small>Последняя котировка: {_fmt_time(last_updated)}</small></td>"
            f"<td>{_fmt_time(row['started_at'])}</td>"
        f'<td class="live-duration" data-started-at="{_iso_utc(row["started_at"])}">{int(max(0, (now - row["started_at"]).total_seconds()))} сек.</td>'
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
  <nav><a class="active" href="/">Возможности</a><a href="/situations">Активные ситуации</a><a href="/history">История</a></nav>
  <header><h1>Арбитражный скринер</h1>
  <div id="worker-status" class="worker-status"><strong>Проверяю обновление…</strong><small></small></div>
  <div class="muted">Длительность активных возможностей обновляется каждую секунду · данные пересчитываются по расписанию</div></header>
  <main><div class="summary">Позиций: {distinct_positions} · Биржевых строк: {len(rows)} · Страница обновлена: {refreshed_at}</div>
  {body}</main>
<script>
function updateLiveDurations() {{
  const now = Date.now();
  document.querySelectorAll('.live-duration[data-started-at]').forEach(cell => {{
    const startedAt = Date.parse(cell.dataset.startedAt);
    if (!Number.isFinite(startedAt)) return;
    let seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
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
