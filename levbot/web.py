"""The panel: one page, refreshed every few seconds from /api/state."""

import time
from pathlib import Path

from aiohttp import web

from . import config
from .strategy import BY_KEY, STRATEGIES

STATIC = Path(__file__).parent / "static"


def strategy_stats(engine) -> list:
    out = []
    for s in STRATEGIES:
        closed = [t for t in engine.state["closed"] if t["strategy"] == s.key]
        wins = [t for t in closed if t["net"] > 0]
        decisions = [d for d in engine.state["decisions"] if d["strategy"] == s.key]
        out.append({"key": s.key, "label": s.label, "description": s.description, "interval": s.interval,
                    "need": s.need, "trades": len(closed), "wins": len(wins), "net": sum(t["net"] for t in closed),
                    "avg_r": (sum(t["r"] for t in closed) / len(closed)) if closed else 0.0,
                    "open": sum(t["strategy"] == s.key for t in engine.state["open"]),
                    "signals": len(decisions), "rejected": sum(not d["passed"] for d in decisions)})
    return out


def snapshot(engine) -> dict:
    st, now = engine.state, time.time()
    eq = engine.equity()
    closed = st["closed"]
    wins = sum(t["net"] > 0 for t in closed)
    open_rows = []
    for t in st["open"]:
        m = engine.marks.get(t["symbol"], {})
        open_rows.append({**t, "label": BY_KEY[t["strategy"]].label, "mark": m.get("mark"), "unrealized": m.get("unrealized"),
                          "liquidation": m.get("liquidation"), "age": now - t["opened_at"]})
    margin = sum(t["margin"] for t in st["open"])
    return {
        "now": now, "mode": config.MODE, "started_at": st["started_at"],
        "money": {"budget": st["budget"], "equity": eq, "net": eq - st["budget"], "closed_net": engine.closed_net(),
                  "unrealized": engine.unrealized(), "day_net": engine.day_net(), "margin_used": margin,
                  "free": max(0.0, eq - margin), "peak": st["peak"], "drawdown": max(0.0, st["peak"] - eq)},
        "stats": {"trades": len(closed), "wins": wins, "win_rate": wins / len(closed) * 100 if closed else 0.0,
                  "fees": sum(t["entry_fee"] + t["exit_fee"] for t in closed), "funding": sum(t["funding"] for t in closed)},
        "rules": {"budget": config.BUDGET_USD, "margin_per_trade": config.MARGIN_PER_TRADE_USD, "risk_per_trade": config.RISK_PER_TRADE_USD,
                  "max_leverage": config.MAX_LEVERAGE, "max_open": config.MAX_OPEN_POSITIONS, "max_same_direction": config.MAX_SAME_DIRECTION,
                  "daily_loss_stop": config.DAILY_LOSS_STOP_USD, "drawdown_halt": config.DRAWDOWN_HALT_USD, "symbols": list(config.SYMBOLS)},
        "strategies": strategy_stats(engine),
        "open": open_rows,
        "closed": [{**t, "label": BY_KEY[t["strategy"]].label} for t in closed[:100]],
        "decisions": [{**d, "label": BY_KEY[d["strategy"]].label} for d in st["decisions"][:60]],
        "events": st["events"][:80],
        "curve": st["curve"],
        "unknown_positions": st.get("unknown_positions", []),
        "errors": [f"{k}: {v}" for k, v in engine.errors.items()],
        "next_scan_at": engine.next_scan_at,
    }


def make_app(engine) -> web.Application:
    @web.middleware
    async def headers(request, handler):
        resp = await handler(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    async def index(_):
        return web.Response(body=(STATIC / "index.html").read_bytes(), content_type="text/html", charset="utf-8")

    async def state(_):
        return web.json_response(snapshot(engine), dumps=lambda o: __import__("json").dumps(o, default=float))

    app = web.Application(middlewares=[headers])
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    return app
