"""Every user-facing report, built only from live state. Missing data is said to be missing.

A number appears only if the bot measured it. Anything paper is labeled paper. Evidence claims in
format_evidence() are copied from a dated research run and carry their limits.
"""

import time
from typing import List, Optional

from . import settings
from .arbitrage import KINDS
from .desk import has_credentials

BOOK_LABEL = {"rules": "Kural", "desk": "Masa"}
EXIT_LABEL = {"hedef": "hedef", "stop": "stop", "süre doldu": "süre doldu", "tasfiye": "TASFİYE", "elle": "elle"}
PAPER_NOTE = "Tüm rakamlar SAHTE PARADIR. Gerçek emir gönderilmiyor."
STALE_SECONDS = 90


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


def book_title(stats: dict) -> str:
    kind = "CEX" if stats["venue"] != "hyperliquid" else "DEX"
    return f"{BOOK_LABEL.get(stats['source'], stats['source'])} / {kind} ({stats['venue']})"


def format_help(bot) -> str:
    return (
        "🤖 Kapsamlı trading botu\n"
        f"{PAPER_NOTE}\n\n"
        "Neler var:\n"
        "• Kural kasaları: 2 strateji, CEX ve DEX'te ayrı ayrı\n"
        "• Masa kasaları: Yapay zeka ajanları tartışır, karar verir\n"
        "• Haber: 4 kaynaktan toplar\n"
        "• Arbitraj: 5 türü ölçer, masraf sonrası kârlıysa sahte işlem yapar\n\n"
        "Komutlar:\n"
        "/durum kasalar\n/pozisyon açık işlemler\n/islemler son kapananlar\n/masa son tartışma\n"
        "/haber son haberler\n/arbitraj ölçümler\n/maliyet tüm giderler\n/test canlı öncesi kontrol\n"
        "/kanit geçmiş test sonucu\n/sifirla kasaları sıfırla"
    )


def format_status(bot, now: Optional[float] = None) -> str:
    now = now or time.time()
    lines = ["📊 DURUM", PAPER_NOTE, f"Çalışma süresi: {(now - bot.started_at) / 3600:.1f} saat", ""]
    for book in bot.books.values():
        s = book.stats()
        lines.append(f"{book_title(s)}")
        lines.append(f"  Bakiye {s['balance']:.2f}$ | Özsermaye {s['equity']:.2f}$ | Açık {s['open']}")
        wr = f"%{s['win_rate']:.0f}" if s["trades"] else "yok"
        lines.append(f"  Kapanan {s['trades']} (kazanma {wr}) | Net {money(s['net'])} | En büyük düşüş {s['max_drawdown']:.2f}$")
        if s["halted"]:
            lines.append(f"  ⛔ DURDU: {s['halted']}")
    lines.append("")
    lines.append(desk_line(bot, now))
    errors = bot.data_errors(now)
    if errors:
        lines.append("")
        lines.append("⚠️ Veri sorunu: " + "; ".join(errors))
    return "\n".join(lines)


def desk_line(bot, now: float) -> str:
    desk = bot.desk
    block = desk.block_reason(now)
    state = desk.state
    head = f"🧠 Masa ({desk.model}): "
    head += "hazır" if not block else f"çalışmıyor, sebep: {block}"
    return (f"{head}\nBugün {state['sessions_today']} oturum, {state['spent_today']:.3f}$ / "
            f"{desk.budget_usd:.2f}$ | Toplam {state['sessions_total']} oturum, {state['spent_total']:.3f}$")


def format_positions(bot, now: Optional[float] = None) -> str:
    now = now or time.time()
    lines = ["📌 AÇIK POZİSYONLAR", PAPER_NOTE]
    count = 0
    for book in bot.books.values():
        for p in book.state["open"]:
            count += 1
            sign = 1 if p["direction"] == "LONG" else -1
            pnl = (p.get("last_mark", p["entry"]) - p["entry"]) * p["qty"] * sign
            target = f"{p['target']:.6g}" if p["target"] is not None else "yok (izleyen stop)"
            lines += ["", f"{book_title(book.stats())}",
                      f"{p['symbol']} {p['direction']} {p['leverage']}x | teminat {p['margin']:.2f}$ | {p.get('strategy', '')}",
                      f"Giriş {p['entry']:.6g} | Stop {p['stop']:.6g} | Hedef {target} | Tasfiye {p['liquidation']:.6g}",
                      f"Anlık kâr/zarar {money(pnl)} (masraf öncesi) | Fonlama {money(-p['funding'])}"]
            fresh = bot.quote_age(book.venue.name, p["symbol"], now)
            if fresh is None or fresh > STALE_SECONDS:
                lines.append(f"⚠️ Fiyat verisi eski: {age_text(fresh)}")
    if not count:
        lines.append("\nAçık pozisyon yok.")
    return "\n".join(lines)


def format_trades(bot, limit: int = 8) -> str:
    rows = []
    for book in bot.books.values():
        rows += [(t, book) for t in book.state["history"]]
    rows.sort(key=lambda x: x[0]["closed_at"], reverse=True)
    lines = ["🧾 SON KAPANAN İŞLEMLER", PAPER_NOTE]
    if not rows:
        lines.append("\nHenüz kapanan işlem yok.")
    for t, book in rows[:limit]:
        lines += ["", f"{book_title(book.stats())}", trade_line(t)]
    return "\n".join(lines)


def trade_line(t: dict) -> str:
    return (f"{t['symbol']} {t['direction']} | çıkış: {EXIT_LABEL.get(t['exit_reason'], t['exit_reason'])}\n"
            f"Brüt {money(t['gross'])} | Komisyon {t['entry_fee'] + t['exit_fee']:.3f}$ | "
            f"Fonlama {money(-t['funding'])} | NET {money(t['net'])}\n"
            f"Kaymanın etkisi fiyatlara dahil: {t['entry_slippage'] + t['exit_slippage']:.3f}$")


def format_open_event(book, p: dict) -> str:
    target = f"{p['target']:.6g}" if p["target"] is not None else "yok (izleyen stop)"
    return (f"🟢 SAHTE İŞLEM AÇILDI\n{book_title(book.stats())}\n"
            f"{p['symbol']} {p['direction']} {p['leverage']}x | {p.get('strategy', '')}\n"
            f"Büyüklük {p['qty'] * p['entry']:.2f}$ | Teminat {p['margin']:.2f}$\n"
            f"Giriş {p['entry']:.6g} | Stop {p['stop']:.6g} | Hedef {target}\n"
            f"Giriş komisyonu {p['entry_fee']:.3f}$ | Kayma {p['entry_slippage']:.3f}$\n"
            f"Neden: {p.get('reason') or 'belirtilmedi'}")


def format_close_event(book, t: dict) -> str:
    s = book.stats()
    icon = "✅" if t["net"] > 0 else "❌"
    return (f"{icon} SAHTE İŞLEM KAPANDI\n{book_title(s)}\n{trade_line(t)}\n"
            f"Kasa: {s['balance']:.2f}$ | Toplam net {money(s['net'])}")


def format_session(record: Optional[dict], full: bool = True) -> str:
    if not record:
        return "🧠 Henüz masa oturumu yapılmadı."
    head = (f"🧠 MASA OTURUMU: {record['symbol']}\n"
            f"Tetikleyen: {record['label']}\n"
            f"Sonuç: {action_label(record)}\n"
            f"Sebep: {record['reason']}\n"
            f"Maliyet: {record['cost_usd']:.4f}$")
    if not full or not record["turns"]:
        return head
    parts = [head]
    for t in record["turns"]:
        veto = " | VETO" if t.get("veto") else ""
        parts.append(f"\n{t['role']} [{t['stance']} {t['conviction']}/100{veto}]\n{t['argument']}")
    return "\n".join(parts)


def action_label(record: dict) -> str:
    if record["action"] == "NO_TRADE":
        return "İŞLEM YOK"
    size = " (yarım boy)" if record.get("size_factor") == 0.5 else ""
    return f"{record['action']}{size}"


def format_news(bot, now: Optional[float] = None) -> str:
    now = now or time.time()
    lines = ["📰 SON HABERLER (başlıklar kaynaktan, doğruluğu garanti değil)"]
    items = bot.news.latest(10)
    if not items:
        lines.append("Henüz haber alınamadı.")
    for item in items:
        tags = ",".join(s.removesuffix("USDT") for s in item.symbols)
        mark = "🔴 " if item.impact == 2 else ""
        lines.append(f"\n{mark}{item.title}\n{item.source} | {age_text(now - item.published)}" + (f" | {tags}" if tags else ""))
    bad = [f"{k}: {v['detail']}" for k, v in bot.news.feed_status.items() if not v["ok"]]
    if bad:
        lines.append("\n⚠️ Çalışmayan kaynak: " + "; ".join(bad))
    return "\n".join(lines)


def format_arbitrage(bot, now: Optional[float] = None) -> str:
    now = now or time.time()
    state = bot.arb.state
    hours = (now - state["started_at"]) / 3600.0
    lines = ["💱 ARBİTRAJ ÖLÇÜMÜ", PAPER_NOTE,
             f"Ölçüm süresi {hours:.1f} saat. Rakamlar işlem başına {settings.ARB_NOTIONAL_USD:.0f}$ içindir.",
             "bps: yüzde 0,01. Net: tüm masraflardan sonra.", ""]
    for key, label in KINDS.items():
        k = state["kinds"][key]
        lines.append(label)
        if k.get("unavailable"):
            lines.append(f"  ⚠️ Ölçülemiyor: {k['unavailable']}")
        elif not k["scans"]:
            lines.append("  Henüz tarama yok.")
        else:
            lines.append(f"  Tarama {k['scans']} | son {age_text(now - k['last_scan_at'])}")
            if k["last_net_bps"] is not None:
                lines.append(f"  Şu anki en iyi: brüt {k['last_gross_bps']:+.1f} bps, net {k['last_net_bps']:+.1f} bps")
                lines.append(f"  Ölçülen en iyi net: {k['best_net_bps']:+.1f} bps | Kârlı görünen tarama: {k['positive']}")
            lines.append(f"  Sahte işlem {k['trades']} | açık {len(k['open'])} | net {money(k['net_usd'])} | "
                         f"tekrar kontrolde kaybolan {k['rejected_on_recheck']}")
        if k["last_error"]:
            lines.append(f"  ⚠️ Son hata: {k['last_error']['detail']}")
    return "\n".join(lines)


def format_costs(bot) -> str:
    lines = ["💸 TÜM GİDERLER", PAPER_NOTE, ""]
    for book in bot.books.values():
        s = book.stats()
        c = s["costs"]
        lines.append(book_title(s))
        lines.append(f"  Komisyon {c['taker_fee'] + c['maker_fee']:.3f}$ | Fonlama {c['funding']:+.3f}$ | Transfer (tahmini) {c['transfer']:.2f}$")
        lines.append(f"  Kayma {c['slippage']:.3f}$ (fiyatların içinde, ayrıca düşülmez)")
    d = bot.desk.state
    lines += ["", "Yapay zeka masası (gerçek para olur)",
              f"  {d['calls']} çağrı | {d['input_tokens']:,} giriş, {d['output_tokens']:,} çıkış, "
              f"{d['cache_read_tokens']:,} önbellek token",
              f"  Toplam {d['spent_total']:.3f}$"]
    arb_fees = sum(k["fees_usd"] for k in bot.arb.state["kinds"].values())
    arb_other = sum(k["other_usd"] for k in bot.arb.state["kinds"].values())
    lines += ["", f"Arbitraj: komisyon {arb_fees:.3f}$ | gaz ve transfer {arb_other:.3f}$"]
    if not has_credentials():
        lines += ["", "Not: API anahtarı olmadığı için masa çalışmıyor, gideri 0."]
    return "\n".join(lines)


def gate_result(bot, now: Optional[float] = None) -> List[dict]:
    """Checks each book against the short forward-test criteria. Desk books also pay for the desk."""
    now = now or time.time()
    rows = []
    desk_spent = bot.desk.state["spent_total"]
    desk_books = max(1, sum(1 for b in bot.books.values() if b.source == "desk"))
    for book in bot.books.values():
        s = book.stats()
        llm = desk_spent / desk_books if book.source == "desk" else 0.0
        net_after = s["net"] - llm
        checks = [
            (f"En az {settings.GATE_MIN_DAYS:.0f} gün", s["days"] >= settings.GATE_MIN_DAYS, f"{s['days']:.1f} gün"),
            (f"En az {settings.GATE_MIN_TRADES} işlem", s["trades"] >= settings.GATE_MIN_TRADES, f"{s['trades']} işlem"),
            ("Net kâr pozitif", net_after > 0, money(net_after)),
            (f"Kâr faktörü en az {settings.GATE_MIN_PROFIT_FACTOR}", s["profit_factor"] >= settings.GATE_MIN_PROFIT_FACTOR,
             f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "zarar yok"),
            (f"Düşüş en çok {settings.GATE_MAX_DRAWDOWN_USD:.0f}$", s["max_drawdown"] <= settings.GATE_MAX_DRAWDOWN_USD,
             f"{s['max_drawdown']:.2f}$"),
            ("Tasfiye yok", s["liquidations"] == 0, str(s["liquidations"])),
        ]
        rows.append({"book": book, "stats": s, "checks": checks, "passed": all(c[1] for c in checks)})
    return rows


def format_gate(bot, now: Optional[float] = None) -> str:
    lines = ["🧪 CANLI ÖNCESİ KONTROL", PAPER_NOTE,
             "Bu kontrol kazancı KANITLAMAZ. 7 gün ve 10 işlem çok az örnektir. Sadece sistemin düzgün "
             "çalıştığını ve felaket olmadığını gösterir. Gerçek parayla kayıp yine mümkündür.", ""]
    for row in gate_result(bot, now):
        lines.append(f"{'✅ GEÇTİ' if row['passed'] else '⏳ GEÇMEDİ'}: {book_title(row['stats'])}")
        for name, ok, value in row["checks"]:
            lines.append(f"  {'✓' if ok else '✗'} {name}: {value}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_evidence() -> str:
    return (
        "📚 GEÇMİŞ TEST (7 Kasım 2026 ölçümü)\n"
        "Dönem: 2 yıl, 10 parite, tüm masraflar dahil. Son %40 üzerinde puanlandı.\n"
        "R: işlem başına riske edilen para.\n\n"
        "4 saatlik trend kırılımı: +0,03R, 99 işlem\n"
        "Aşırı fonlamaya karşı: +0,12R, 31 işlem\n"
        "1 saatlik trend kırılımı: -0,07R, 277 işlem\n"
        "Aşırı alım-satımdan dönüş: -0,21R, 204 işlem\n"
        "Projedeki eski 15 dakikalık sinyal: -0,12R, 359 işlem (180 günlük ayrı test)\n\n"
        "Sonuç: Hiçbiri rastgele işlemden anlamlı şekilde iyi değil. Pozitif ikisi de "
        "şans olabilir. Bot onları kanıtlanmış olduğu için değil, en az kötü olduğu için izliyor.\n"
        "Yapay zeka masası geçmişte test edilemedi. Değeri sadece ileriye dönük ölçülecek."
    )


def format_startup(bot) -> str:
    desk = "hazır" if bot.desk.enabled else f"kapalı ({bot.desk.disabled_reason})"
    return ("🟢 Bot başladı\n" + PAPER_NOTE + "\n"
            f"CEX: {bot.cex_venue or 'erişilemiyor'} | DEX: {settings.DEX_VENUE}\n"
            f"Masa: {desk}\nKomutlar için /start yazın.")


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
