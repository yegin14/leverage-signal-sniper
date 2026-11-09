"""Short Telegram texts. Details live on the web panel, so the chat stays quiet.

A number appears only if the bot measured it. Everything is labeled as paper money.
"""

import time
from typing import List, Optional

from . import settings
from .strategies import SOURCE_LABELS

PAPER_NOTE = "Tüm rakamlar SAHTE PARADIR. Gerçek emir gönderilmiyor."

# Copied from a dated research run. The panel shows these with their limits.
EVIDENCE = {
    "measured_on": "7 Kasım 2026",
    "note": "Tüm masraflar dahil, ilk 10 parite. Rastgele işlemden anlamlı şekilde iyi olan yok. "
            "Pozitif görünenler şans olabilir.",
    "rows": [
        {"name": "Aşırı fonlama", "net_usd": 5.7, "trades": 31, "period": "Son 9,5 ay"},
        {"name": "4 saatlik trend", "net_usd": 3.9, "trades": 99, "period": "Son 9,5 ay"},
        {"name": "1 saatlik trend", "net_usd": -27.3, "trades": 277, "period": "Son 9,5 ay"},
        {"name": "Aşırı alım-satımdan dönüş (botta yok)", "net_usd": -57.5, "trades": 204, "period": "Son 9,5 ay"},
        {"name": "15 dakikalık sinyal", "net_usd": -59.4, "trades": 359, "period": "Son 72 gün"},
    ],
}


def money(x: float) -> str:
    return f"{x:+,.2f}$"


def age_text(seconds: Optional[float]) -> str:
    if seconds is None:
        return "hiç"
    if seconds < 90:
        return f"{seconds:.0f} sn önce"
    if seconds < 5400:
        return f"{seconds / 60:.0f} dk önce"
    return f"{seconds / 3600:.1f} saat önce"


def panel_line() -> str:
    url = settings.EXTERNAL_URL
    return f"🌐 Panel: {url}" if url else "🌐 Panel adresi bu sunucuda tanımlı değil."


def totals(bot) -> dict:
    stats = [b.stats() for b in bot.books.values()]
    return {
        "net": sum(s["equity"] - b.state["initial"] for s, b in zip(stats, bot.books.values())),
        "open": sum(s["open"] for s in stats),
        "trades": sum(s["trades"] for s in stats),
        "wins": sum(s["wins"] for s in stats),
    }


def source_lines(bot) -> List[str]:
    lines = []
    for source, label in SOURCE_LABELS.items():
        books = [b for b in bot.books.values() if b.source == source]
        if not books:
            continue
        parts = []
        for b in books:
            kind = "DEX" if b.venue.kind == "dex" else "CEX"
            parts.append(f"{kind} {money(b.equity() - b.state['initial'])}")
        trades = sum(len(b.state["history"]) for b in books)
        lines.append(f"{label}: {' | '.join(parts)} ({trades} işlem)")
    return lines


def format_help() -> str:
    return (
        "🤖 Kaldıraç botu\n"
        f"{PAPER_NOTE}\n\n"
        "Ne yapar:\n"
        "• Kaldıraç: 4 strateji, CEX ve DEX'te ayrı kasalarda\n"
        "• Haber: Haber olumluysa alır, olumsuzsa açığa satar\n"
        "• Yapay zeka masası: API anahtarı girilince çalışır\n\n"
        "Tüm ayrıntılar panelde. Burada sadece özet ve önemli uyarılar gelir.\n\n"
        "/durum özet\n/panel site adresi\n/sifirla kasaları sıfırla\n\n"
        f"{panel_line()}"
    )


def format_status(bot, now: Optional[float] = None) -> str:
    now = now or time.time()
    t = totals(bot)
    lines = ["📊 DURUM", PAPER_NOTE, "",
             f"Toplam net: {money(t['net'])} (açık pozisyonlar ve giderler dahil)",
             f"Açık pozisyon: {t['open']} | Kapanan işlem: {t['trades']}", ""]
    lines += source_lines(bot)
    lines += ["", desk_line(bot, now)]
    errors = bot.data_errors(now)
    if errors:
        lines += ["", "⚠️ Veri sorunu: " + "; ".join(errors)]
    lines += ["", panel_line()]
    return "\n".join(lines)


def format_digest(bot, now: Optional[float] = None) -> str:
    now = now or time.time()
    t = totals(bot)
    lines = [f"🕒 {settings.TELEGRAM_DIGEST_HOURS:g} saatlik özet (sahte para)",
             f"Toplam net: {money(t['net'])} | Açık {t['open']} | Kapanan {t['trades']}", ""]
    lines += source_lines(bot)
    lines += ["", panel_line()]
    return "\n".join(lines)


def desk_line(bot, now: float) -> str:
    desk = bot.desk
    block = desk.block_reason(now)
    state = desk.state
    status = "hazır" if not block else f"çalışmıyor ({block})"
    return f"Masa: {status}, {state['sessions_total']} oturum, {state['spent_total']:.3f}$ harcandı"


def format_startup(bot) -> str:
    desk = "hazır" if bot.desk.enabled else f"kapalı ({bot.desk.disabled_reason})"
    return ("🟢 Bot başladı\n" + PAPER_NOTE + "\n"
            f"CEX: {bot.cex_venue or 'erişilemiyor'} | DEX: {settings.DEX_VENUE}\n"
            f"Masa: {desk}\n" + panel_line())


def split_message(text: str, limit: int = 3800) -> List[str]:
    """Telegram rejects messages over 4096 characters. Splits on line breaks where possible."""
    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks
