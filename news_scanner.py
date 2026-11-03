"""
LeverageSignalSniper - News & Sentiment Scanner
Kripto piyasasındaki son dakika haberlerini (Cointelegraph RSS vb.) arka planda dinler.
Bir coinde flaş bir gelişme olduğunda sinyale ekler veya tehlikeli haberlerde sinyali durdurur.
"""

import time
import xml.etree.ElementTree as ET
import aiohttp

NEWS_RSS_URL = "https://cointelegraph.com/rss"

BEARISH_KEYWORDS = ["hack", "exploit", "sec", "lawsuit", "dava", "delist", "ban", "stolen", "scam", "halt", "down"]
BULLISH_KEYWORDS = ["approved", "partnership", "launch", "upgrade", "etf", "mainnet", "adoption", "surge", "all-time high"]

class NewsScanner:
    def __init__(self):
        self.cached_news: list[dict] = []
        self.last_fetch_time: float = 0
        self.fetch_interval: float = 120.0  # 2 dakikada bir güncelle

    async def update_news_feed(self, session: aiohttp.ClientSession):
        now = time.time()
        if (now - self.last_fetch_time) < self.fetch_interval and self.cached_news:
            return

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        try:
            async with session.get(NEWS_RSS_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    root = ET.fromstring(text)
                    items = root.findall(".//item")
                    news_list = []
                    for it in items[:25]:
                        title = it.find("title").text if it.find("title") is not None else ""
                        link = it.find("link").text if it.find("link") is not None else ""
                        pub_date = it.find("pubDate").text if it.find("pubDate") is not None else ""
                        news_list.append({
                            "title": title,
                            "link": link,
                            "pub_date": pub_date
                        })
                    self.cached_news = news_list
                    self.last_fetch_time = now
        except Exception:
            pass

    def check_coin_news(self, symbol: str) -> dict | None:
        """Belirtilen coin ile ilgili sıcak bir haber var mı kontrol eder"""
        clean = symbol.replace("USDT", "").upper()
        # İsim eşlemeleri
        aliases = [clean]
        name_map = {
            "BTC": ["BITCOIN", "BTC"],
            "ETH": ["ETHEREUM", "ETH"],
            "SOL": ["SOLANA", "SOL"],
            "XRP": ["RIPPLE", "XRP"],
            "DOGE": ["DOGECOIN", "DOGE"],
            "SUI": ["SUI"],
            "PEPE": ["PEPE"],
            "AVAX": ["AVALANCHE", "AVAX"],
            "LINK": ["CHAINLINK", "LINK"],
            "NEAR": ["NEAR"],
            "SHIB": ["SHIBA", "SHIB"]
        }
        if clean in name_map:
            aliases = name_map[clean]

        for item in self.cached_news:
            t_upper = item["title"].upper()
            if any(alias in t_upper for alias in aliases):
                # Sentiment tespiti
                is_bearish = any(w in t_upper.lower() for w in BEARISH_KEYWORDS)
                is_bullish = any(w in t_upper.lower() for w in BULLISH_KEYWORDS)
                
                tag = "NÖTR"
                if is_bearish:
                    tag = "🚨 TEHLİKE / OLUMSUZ"
                elif is_bullish:
                    tag = "🚀 OLUMLU / BOĞA"

                return {
                    "headline": item["title"],
                    "tag": tag,
                    "is_bearish": is_bearish,
                    "is_bullish": is_bullish,
                    "link": item["link"]
                }
        return None
