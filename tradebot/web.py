"""The web panel: read-only pages and JSON. Nothing here can change the bot or place an order.

Everything shown comes from live state. Headlines and agent text are untrusted, so the page inserts
them as plain text and a strict Content-Security-Policy blocks any injected script.
"""

import json
import math
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from . import reports, settings
from .costs import fee_usd, sign
from .desk import has_credentials
from .strategies import SOURCE_LABELS

STATIC_DIR = Path(__file__).parent / "static"
STATE_CACHE_SECONDS = 2.0
MAX_TRADES = 200
MAX_NEWS = 60
MAX_SESSIONS = 20
MAX_EVENTS = 150
STATIC_FILES = {"app.js": "text/javascript", "style.css": "text/css"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def sanitize(value: Any) -> Any:
    """JSON has no Infinity or NaN. A profit factor with no losses becomes null, shown as "kayıp yok"."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value


def _kind(book) -> str:
    return "DEX" if book.venue.kind == "dex" else "CEX"


def book_row(book) -> dict:
    s = book.stats()
    initial = book.state["initial"]
    return {
        **s,
        "key": book.name,
        "source_label": SOURCE_LABELS.get(book.source, book.source),
        "kind": _kind(book),
        "initial": initial,
        "total_net": s["equity"] - initial,
    }


def position_row(book, p: dict, bot, now: float) -> dict:
    s = sign(p["direction"])
    mark = p.get("last_mark", p["entry"])
    gross = (mark - p["entry"]) * p["qty"] * s
    est_exit = fee_usd(mark * p["qty"], book.venue.taker_pct)
    return {
        "book": book.name,
        "source": book.source,
        "source_label": SOURCE_LABELS.get(book.source, book.source),
        "kind": _kind(book),
        "venue": book.venue.name,
        "symbol": p["symbol"],
        "direction": p["direction"],
        "leverage": p["leverage"],
        "notional": p["qty"] * p["entry"],
        "margin": p["margin"],
        "entry": p["entry"],
        "mark": mark,
        "stop": p["stop"],
        "target": p["target"],
        "liquidation": p["liquidation"],
        "gross_now": gross,
        "funding": p["funding"],
        # What closing right now would leave, entry fee and an estimated exit fee included.
        "net_now": gross - p["entry_fee"] - est_exit - p["funding"],
        "opened_at": p["opened_at"],
        "quote_age": bot.quote_age(book.venue.name, p["symbol"], now),
        "reason": p.get("reason", ""),
    }


def trade_row(book, t: dict) -> dict:
    return {
        "book": book.name,
        "source": book.source,
        "source_label": SOURCE_LABELS.get(book.source, book.source),
        "kind": _kind(book),
        "venue": book.venue.name,
        "symbol": t["symbol"],
        "direction": t["direction"],
        "entry": t["entry"],
        "exit": t["exit"],
        "exit_reason": t["exit_reason"],
        "gross": t["gross"],
        "fees": t["entry_fee"] + t["exit_fee"],
        "funding": t["funding"],
        "slippage": t["entry_slippage"] + t["exit_slippage"],
        "net": t["net"],
        "opened_at": t["opened_at"],
        "closed_at": t["closed_at"],
        "leverage": t["leverage"],
        "notional": t["qty"] * t["entry"],
        "reason": t.get("reason", ""),
        "liquidated": t["liquidated"],
    }


def build_state(bot, now: float) -> dict:
    positions = [position_row(b, p, bot, now) for b in bot.books.values() for p in b.state["open"]]
    positions.sort(key=lambda r: r["opened_at"], reverse=True)
    trades = [trade_row(b, t) for b in bot.books.values() for t in b.state["history"]]
    trades.sort(key=lambda r: r["closed_at"], reverse=True)
    desk_state = bot.desk.state
    return sanitize({
        "generated_at": now,
        "mode": "paper",
        "started_at": bot.started_at,
        "panel_note": reports.PAPER_NOTE,
        "venues": {"cex": bot.cex_venue, "dex": settings.DEX_VENUE, "perp": bot.perp_venues},
        "feeds": [f.status(now) for f in getattr(bot.market, "feeds", {}).values()],
        "loop_lag_ms": round(bot.loop_lag_ms, 1),
        "errors": bot.data_errors(now),
        "sources": [{"key": k, "label": v} for k, v in SOURCE_LABELS.items()],
        "limits": {"risk_per_trade_usd": bot.limits.risk_per_trade_usd, "max_leverage": bot.limits.max_leverage},
        "books": [book_row(b) for b in bot.books.values()],
        "positions": positions,
        "trades": trades[:MAX_TRADES],
        "trades_total": len(trades),
        "news": {
            "last_fetch": bot.news.last_fetch,
            "feeds": bot.news.feed_status,
            "items": [{"id": i.id, "title": i.title, "source": i.source, "link": i.link, "published": i.published,
                       "symbols": i.symbols, "impact": i.impact, "sentiment": i.sentiment,
                       "actions": bot.news.actions.get(i.id, [])} for i in bot.news.latest(MAX_NEWS)],
        },
        "desk": {
            "enabled": bot.desk.enabled,
            "reason": bot.desk.disabled_reason,
            "block": bot.desk.block_reason(now),
            "has_key": has_credentials(),
            "model": bot.desk.model,
            "budget_usd": bot.desk.budget_usd,
            "spent_today": desk_state["spent_today"],
            "spent_total": desk_state["spent_total"],
            "sessions_today": desk_state["sessions_today"],
            "sessions_total": desk_state["sessions_total"],
            "calls": desk_state["calls"],
            "input_tokens": desk_state["input_tokens"],
            "output_tokens": desk_state["output_tokens"],
            "cache_read_tokens": desk_state["cache_read_tokens"],
            "outcomes": desk_state["outcomes"],
            "last_error": desk_state["last_error"],
            "sessions": list(reversed(desk_state["sessions"][-MAX_SESSIONS:])),
        },
        "events": list(reversed(bot.events[-MAX_EVENTS:])),
        "evidence": reports.EVIDENCE,
    })


def make_app(bot) -> web.Application:
    cache = {"at": 0.0, "body": b""}

    @web.middleware
    async def security_headers(request, handler):
        response = await handler(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def index(_request):
        return web.Response(body=(STATIC_DIR / "index.html").read_bytes(), content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-cache"})

    async def static_file(request):
        name = request.match_info["name"]
        if name not in STATIC_FILES:
            raise web.HTTPNotFound()
        return web.Response(body=(STATIC_DIR / name).read_bytes(), content_type=STATIC_FILES[name], charset="utf-8",
                            headers={"Cache-Control": "no-cache"})

    async def api_state(_request):
        now = time.time()
        if now - cache["at"] >= STATE_CACHE_SECONDS or not cache["body"]:
            cache["body"] = json.dumps(build_state(bot, now), ensure_ascii=False, allow_nan=False).encode("utf-8")
            cache["at"] = now
        response = web.Response(body=cache["body"], content_type="application/json", charset="utf-8",
                                headers={"Cache-Control": "no-store"})
        response.enable_compression()
        return response

    async def api_curve(_request):
        books = {k: {"source": b.source, "kind": _kind(b), "initial": b.state["initial"]} for k, b in bot.books.items()}
        response = web.json_response({"books": books, "points": bot.curve}, headers={"Cache-Control": "no-store"})
        response.enable_compression()
        return response

    async def status(_request):
        now = time.time()
        return web.json_response({
            "ok": True, "mode": "paper", "uptime_hours": round((now - bot.started_at) / 3600.0, 2),
            "cex": bot.cex_venue, "dex": settings.DEX_VENUE, "errors": bot.data_errors(now),
            "reachable": {"perp": bot.perp_venues},
            "feeds": [f.status(now) for f in getattr(bot.market, "feeds", {}).values()],
            "loop_lag_ms": round(bot.loop_lag_ms, 1),
            "books": sanitize({k: {x: b.stats()[x] for x in ("balance", "equity", "trades", "net", "open")}
                               for k, b in bot.books.items()}),
            "news_items": len(bot.news.items),
        })

    app = web.Application(middlewares=[security_headers])
    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", static_file)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/curve", api_curve)
    app.router.add_get("/status", status)
    return app
