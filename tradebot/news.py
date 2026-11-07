"""Collects crypto news from RSS and decides, by keywords, whether a headline is good or bad.

Headlines are untrusted text from the internet. This module removes markup and control characters
and limits length. Tagging uses word boundaries, so "SOLD" never matches SOL.

The keyword reading is crude and misses context. It runs without any API cost, and the panel shows
each headline next to the position it caused, so its quality can be judged. No past test exists:
RSS feeds only keep recent items.
"""

import email.utils
import hashlib
import html
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import aiohttp
from defusedxml import ElementTree as SafeET

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LeverageSignalSniper/2.0)"}
MAX_TITLE_CHARS = 200
MAX_ITEMS = 300
MAX_AGE_SECONDS = 48 * 3600
# A headline older than this is probably priced in, so it opens no position.
FRESH_SECONDS = 30 * 60

# Tickers that are also English words match only in capitals; names match in any case.
_NAMES = {
    "BTCUSDT": (r"bitcoin(?! cash)", r"BTC"),
    "ETHUSDT": (r"ethereum|ether", r"ETH"),
    "SOLUSDT": (r"solana", r"SOL"),
    "XRPUSDT": (r"xrp|ripple", r"XRP"),
    "DOGEUSDT": (r"dogecoin", r"DOGE"),
    "BNBUSDT": (r"bnb", r"BNB"),
    "ADAUSDT": (r"cardano", r"ADA"),
    "LINKUSDT": (r"chainlink", r"LINK"),
    "AVAXUSDT": (r"avalanche", r"AVAX"),
    "SUIUSDT": (r"sui network", r"SUI|Sui"),
    "ZECUSDT": (r"zcash", r"ZEC"),
    "HYPEUSDT": (r"hyperliquid", r"HYPE"),
    "NEARUSDT": (r"near protocol", r"NEAR"),
    "ENAUSDT": (r"ethena", r"ENA"),
    "UNIUSDT": (r"uniswap", r"UNI"),
    "WLDUSDT": (r"worldcoin", r"WLD"),
    "BCHUSDT": (r"bitcoin cash", r"BCH"),
    "AAVEUSDT": (r"aave", r"AAVE"),
    "ARBUSDT": (r"arbitrum", r"ARB"),
    "LTCUSDT": (r"litecoin", r"LTC"),
}
SYMBOL_PATTERNS = {
    symbol: (re.compile(rf"\b(?:{name})\b", re.IGNORECASE), re.compile(rf"\b(?:{ticker})\b"))
    for symbol, (name, ticker) in _NAMES.items()
}

HIGH_IMPACT = re.compile(
    r"\b(hack(ed|s)?|exploit(ed|s)?|drain(ed|s)?|stolen|bankrupt\w*|insolven\w*|delist\w*|halt(ed|s)?|outage|"
    r"lawsuit|sue[sd]|indict\w*|arrest\w*|bans?|banned|sanction\w*|depeg\w*|emergency|"
    r"etf approv\w*|approves? .{0,30}etf|sec charges?|rate (cut|hike)s?|fomc|cpi)\b",
    re.IGNORECASE,
)
MEDIUM_IMPACT = re.compile(
    r"\b(partnership|mainnet|upgrade|listing|lists|airdrop|unlock|acquisition|acquires?|inflows?|outflows?|"
    r"treasury|whale|liquidations?|surge[sd]?|plunge[sd]?|all-time high|record high)\b",
    re.IGNORECASE,
)
MACRO = re.compile(r"\b(fed|fomc|cpi|inflation|tariffs?|treasury yields?|rate (cut|hike)s?|powell|jobs report)\b",
                   re.IGNORECASE)

# Words that report a price move ("surges", "plunges") are left out on purpose: the move has
# already happened by the time the headline appears.
NEGATIVE = re.compile(
    r"\b(hack(ed|s|ers?)?|exploit(ed|s)?|drain(ed|s)?|stolen|theft|bankrupt\w*|insolven\w*|delist\w*|outage|"
    r"halt(ed|s)? (withdrawals?|trading|deposits?)|lawsuit|sue[sd]|indict\w*|arrest\w*|banned|bans|sanction\w*|"
    r"depeg\w*|fraud\w*|breach\w*|seized?|crackdown|sec charges?|rejects?|rejected|rate hikes?|outflows?)\b",
    re.IGNORECASE,
)
POSITIVE = re.compile(
    r"\b(etf approv\w*|approv\w* .{0,30}etf|approved|partnership|partners with|launches mainnet|mainnet launch\w*|"
    r"upgrade (goes live|activated|completed?)|lists? on|listing on|inflows?|acquisition|acquires?|adopts?|adoption|"
    r"integrat\w*|rate cuts?|accumulat\w*|buys|bought)\b",
    re.IGNORECASE,
)
# "SEC drops lawsuit" contains a bad word but is good news.
REVERSAL = re.compile(
    r"\b(dismiss\w*|drops?|dropped|wins?|won|ends?|ended|lifts?|lifted|overturn\w*)\b.{0,25}"
    r"\b(lawsuit|case|charges?|ban|sanctions?)\b",
    re.IGNORECASE,
)


@dataclass
class NewsItem:
    id: str
    title: str
    source: str
    link: str
    published: float
    symbols: List[str]
    impact: int  # 0 none, 1 medium, 2 high
    macro: bool
    sentiment: int = 0  # +1 good, -1 bad, 0 unclear


def clean_text(text: str) -> str:
    """Plain, single-line, short text. Angle brackets go, so a headline cannot fake a tag."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("<", " ").replace(">", " ")
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Co", "Cs"))
    return re.sub(r"\s+", " ", text).strip()[:MAX_TITLE_CHARS]


def tag_symbols(title: str) -> List[str]:
    return [s for s, (name, ticker) in SYMBOL_PATTERNS.items() if name.search(title) or ticker.search(title)]


def score_impact(title: str) -> int:
    if HIGH_IMPACT.search(title):
        return 2
    if MEDIUM_IMPACT.search(title):
        return 1
    return 0


def score_sentiment(title: str) -> int:
    """+1 good, -1 bad, 0 when neither or both appear. A mixed headline opens no position."""
    if REVERSAL.search(title):
        return 1
    good, bad = bool(POSITIVE.search(title)), bool(NEGATIVE.search(title))
    if good == bad:
        return 0
    return 1 if good else -1


def parse_date(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError):
        return None


def parse_feed(xml_text: str, source: str, now: float) -> List[NewsItem]:
    items: List[NewsItem] = []
    root = SafeET.fromstring(xml_text)
    for node in root.iter("item"):
        title = clean_text(node.findtext("title", ""))
        published = parse_date(node.findtext("pubDate"))
        if not title or published is None or now - published > MAX_AGE_SECONDS:
            continue
        key = re.sub(r"[^a-z0-9]+", "", title.lower())
        link = clean_text(node.findtext("link", ""))[:300]
        items.append(NewsItem(
            id=hashlib.sha1(key.encode()).hexdigest()[:16],
            title=title,
            source=source,
            link=link if link.startswith(("https://", "http://")) else "",
            published=published,
            symbols=tag_symbols(title),
            impact=score_impact(title),
            macro=bool(MACRO.search(title)),
            sentiment=score_sentiment(title),
        ))
    return items


class NewsDesk:
    def __init__(self, state: Optional[dict] = None):
        state = state or {}
        self.items: Dict[str, NewsItem] = {k: NewsItem(**v) for k, v in state.get("items", {}).items()}
        self.triggered: List[str] = list(state.get("triggered", []))
        self.traded: List[str] = list(state.get("traded", []))
        self.actions: Dict[str, List[dict]] = dict(state.get("actions", {}))
        self.feed_status: Dict[str, dict] = {}
        self.last_fetch: float = 0.0

    def to_state(self) -> dict:
        return {"items": {k: asdict(v) for k, v in self.items.items()}, "triggered": self.triggered[-500:],
                "traded": self.traded[-500:], "actions": self.actions}

    def ingest(self, items: List[NewsItem], now: float) -> List[NewsItem]:
        """Adds new items and returns those not seen before. The same story from two feeds is one."""
        new = []
        for item in items:
            if item.id not in self.items:
                self.items[item.id] = item
                new.append(item)
        for key in [k for k, v in self.items.items() if now - v.published > MAX_AGE_SECONDS]:
            del self.items[key]
            self.actions.pop(key, None)
        if len(self.items) > MAX_ITEMS:
            for key, _ in sorted(self.items.items(), key=lambda kv: kv[1].published)[:len(self.items) - MAX_ITEMS]:
                del self.items[key]
                self.actions.pop(key, None)
        return new

    async def refresh(self, session: aiohttp.ClientSession, now: Optional[float] = None) -> List[NewsItem]:
        now = now or time.time()
        new: List[NewsItem] = []
        for source, url in FEEDS.items():
            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        self.feed_status[source] = {"ok": False, "detail": f"HTTP {r.status}", "at": now}
                        continue
                    text = await r.text()
                parsed = parse_feed(text, source, now)
                new.extend(self.ingest(parsed, now))
                self.feed_status[source] = {"ok": True, "detail": f"{len(parsed)} haber", "at": now}
            except Exception as e:  # A broken feed must not stop the others.
                self.feed_status[source] = {"ok": False, "detail": type(e).__name__, "at": now}
        self.last_fetch = now
        return new

    def for_symbol(self, symbol: str, now: float, limit: int = 12, hours: float = 24.0) -> List[NewsItem]:
        """Headlines about the symbol plus macro headlines, newest first."""
        cutoff = now - hours * 3600.0
        rows = [i for i in self.items.values() if i.published >= cutoff and (symbol in i.symbols or i.macro)]
        return sorted(rows, key=lambda i: i.published, reverse=True)[:limit]

    def latest(self, limit: int = 10, min_impact: int = 0) -> List[NewsItem]:
        rows = [i for i in self.items.values() if i.impact >= min_impact]
        return sorted(rows, key=lambda i: i.published, reverse=True)[:limit]

    def session_triggers(self, now: float) -> List[NewsItem]:
        """Fresh high-impact headlines naming a tracked symbol, for the desk. Each fires once."""
        out = []
        for item in sorted(self.items.values(), key=lambda i: i.published):
            if (item.impact >= 2 and item.symbols and now - item.published <= FRESH_SECONDS
                    and item.id not in self.triggered):
                self.triggered.append(item.id)
                out.append(item)
        return out

    def trade_triggers(self, now: float) -> List[NewsItem]:
        """Fresh headlines with a clear direction and a tracked symbol. Each fires once."""
        out = []
        for item in sorted(self.items.values(), key=lambda i: i.published):
            if (item.sentiment != 0 and item.symbols and now - item.published <= FRESH_SECONDS
                    and item.id not in self.traded):
                self.traded.append(item.id)
                out.append(item)
        return out

    def record_action(self, item_id: str, action: dict) -> None:
        self.actions.setdefault(item_id, []).append(action)
