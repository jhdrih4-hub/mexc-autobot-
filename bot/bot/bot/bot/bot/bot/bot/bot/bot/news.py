import calendar
import hashlib
import math
import re
import time
from urllib.parse import urlparse

import feedparser

from .config import AppConfig
from .database import Database
from .utils import LOG, safe_float


class NewsEngine:
    FEEDS = [
        ("https://cointelegraph.com/rss", "crypto"),
        ("https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto"),
        ("https://www.federalreserve.gov/feeds/press_all.xml", "macro"),
        ("https://www.ecb.europa.eu/rss/press.html", "macro"),
    ]
    POS = {"surge", "rally", "bullish", "gain", "gains", "soar", "jump", "adoption",
           "partnership", "approval", "approved", "upgrade", "inflow", "breakout",
           "record", "launch", "etf", "positive", "integration", "rate cut", "easing",
           "stimulus", "rate cuts"}
    NEG = {"crash", "plunge", "bearish", "loss", "losses", "hack", "hacked", "exploit",
           "ban", "banned", "lawsuit", "fraud", "sell-off", "selloff", "dump", "outflow",
           "liquidation", "warning", "fears", "collapse", "downgrade", "delist", "risk",
           "rate hike", "hiking", "inflation", "recession", "tariff", "sanctions",
           "probe", "charges"}
    ALIASES = {"BTC": ["bitcoin"], "ETH": ["ethereum"], "SOL": ["solana"],
               "BNB": ["binance", "bnb"], "XRP": ["ripple"], "DOGE": ["dogecoin"],
               "ADA": ["cardano"], "AVAX": ["avalanche"], "TRX": ["tron"],
               "LINK": ["chainlink"], "DOT": ["polkadot"], "MATIC": ["polygon", "matic"],
               "TON": ["toncoin"], "SHIB": ["shiba"], "LTC": ["litecoin"],
               "BCH": ["bitcoin cash"], "NEAR": ["near protocol"], "APT": ["aptos"],
               "ARBITRUM": ["arbitrum"], "SUI": ["sui"], "PEPE": ["pepe"],
               "FIL": ["filecoin"], "ATOM": ["cosmos"], "XLM": ["stellar"],
               "HBAR": ["hedera"], "UNI": ["uniswap"], "ETC": ["ethereum classic"],
               "OP": ["optimism"], "RENDER": ["render"]}
    MACRO_WEIGHT = 0.4

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.cfg = config
        self._last = 0.0
        self.last_macro = 0.0

    def refresh(self, force=False):
        if not force and time.time() - self._last < self.cfg.news_refresh_minutes * 60:
            return
        n = 0
        cutoff = time.time() - self.cfg.news_max_age_hours * 3600
        future_limit = time.time() + 600
        for url, kind in self.FEEDS:
            try:
                d = feedparser.parse(url)
                for e in getattr(d, "entries", [])[:25]:
                    title = (getattr(e, "title", "") or "").strip()
                    if not title:
                        continue
                    ts = self._entry_ts(e)
                    if ts == 0.0 or ts < cutoff or ts > future_limit:
                        continue  # missing / stale / future timestamp -> rejected
                    link = getattr(e, "link", "") or ""
                    h = hashlib.sha1((title or link).encode("utf-8")).hexdigest()
                    sent = self._sentiment(title) * (self.MACRO_WEIGHT if kind == "macro" else 1.0)
                    if self.db.insert_news(h, ts, urlparse(url).netloc, title, link,
                                           sent, abs(sent), self._assets(title)):
                        n += 1
            except Exception as e:
                LOG.warning("news feed failed %s: %s", url, e)
        self._last = time.time()
        self.last_macro = self.macro_sentiment(hours=24)
        LOG.info("news scan done: %d new items; macro sentiment %.2f", n, self.last_macro)

    def _entry_ts(self, e) -> float:
        try:
            pp = getattr(e, "published_parsed", None)
            if pp:
                return float(calendar.timegm(pp))
        except Exception:
            pass
        return 0.0

    def _sentiment(self, text) -> float:
        t = text.lower()
        s = 0
        for w in self.POS:
            if w in t:
                s += 1
        for w in self.NEG:
            if w in t:
                s -= 1
        return float(math.tanh(s / 3.0))

    def _assets(self, title) -> list:
        t = title.lower()
        bases = sorted({s.split("/")[0] for s in self.cfg.symbols}) if self.cfg.symbols else []
        return [b for b in bases
                if b.lower() in t or any(a in t for a in self.ALIASES.get(b, []))]

    def sentiment_for(self, symbol, hours=24) -> float:
        base = symbol.split("/")[0]
        aliases = [base.lower()] + [a.lower() for a in self.ALIASES.get(base, [])]
        try:
            df = self.db.recent_news(time.time() - hours * 3600)
        except Exception:
            return 0.0
        if df is None or df.empty:
            return 0.0
        num = den = 0.0
        now = time.time()
        for _, row in df.iterrows():
            tl = str(row.get("title") or "").lower()
            if not any(a in tl for a in aliases):
                continue
            age = max(0.0, now - safe_float(row.get("published"), now))
            w = max(safe_float(row.get("impact"), 0.0), 0.05) * math.exp(-age / 21600.0)
            num += safe_float(row.get("sentiment"), 0.0) * w
            den += w
        return num / den if den > 0 else 0.0

    def macro_sentiment(self, hours=24) -> float:
        try:
            df = self.db.recent_news(time.time() - hours * 3600)
        except Exception:
            return 0.0
        if df is None or df.empty:
            return 0.0
        src_macro = {"www.federalreserve.gov", "www.ecb.europa.eu"}
        num = den = 0.0
        now = time.time()
        for _, row in df.iterrows():
            if str(row.get("source") or "") not in src_macro:
                continue
            age = max(0.0, now - safe_float(row.get("published"), now))
            w = math.exp(-age / 43200.0)
            num += safe_float(row.get("sentiment"), 0.0) * w
            den += w
        return num / den if den > 0 else 0.0
