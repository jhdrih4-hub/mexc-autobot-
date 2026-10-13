import functools
import logging
import math
import os
import random
import re
import threading
import time
from logging.handlers import RotatingFileHandler

from .errors import RetryableError

_SECRET_STORE = set()
LOG = logging.getLogger("autobot")


def register_secret(s):
    if s and len(str(s)) >= 6:
        _SECRET_STORE.add(str(s))


def scrub(text):
    t = str(text)
    for s in list(_SECRET_STORE):
        if s in t:
            t = t.replace(s, "***")
    return re.sub(r"\b[A-Fa-f0-9]{64}\b", "***", t)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return scrub(super().format(record))


def setup_logging(data_dir="data"):
    if LOG.handlers:
        return LOG
    LOG.setLevel(logging.INFO)
    fmt = RedactingFormatter("%(asctime)s | %(levelname)-7s | %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    LOG.addHandler(ch)
    try:
        os.makedirs(data_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(data_dir, "system.log"),
                                 maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError:
        pass
    return LOG


def with_retry(attempts=3, base_delay=1.5, max_delay=30.0):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            last = None
            for i in range(attempts):
                try:
                    return fn(*a, **k)
                except RetryableError as e:
                    last = e
                    d = min(max_delay, base_delay * (2 ** i)) * (getattr(e, "backoff", 1.0) or 1.0)
                    d *= 0.8 + 0.4 * random.random()
                    LOG.warning("retry %d/%d %s: %s (%.1fs)", i + 1, attempts, fn.__name__, e, d)
                    time.sleep(d)
            raise last
        return wrapper
    return deco


class RateLimiter:
    def __init__(self, min_interval=0.2):
        self.min_interval = min_interval
        self._last = {}
        self._lock = threading.Lock()

    def wait(self, key="g"):
        with self._lock:
            dt = time.monotonic() - self._last.get(key, 0.0)
            if dt < self.min_interval:
                time.sleep(self.min_interval - dt)
            self._last[key] = time.monotonic()


RATE = RateLimiter(0.2)

TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
              "12h": 43200, "1d": 86400}


def utc_now():
    import pandas as pd
    return pd.Timestamp.now(tz="UTC")


def safe_float(x, default=None):
    try:
        f = float(x)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def fmt_money(x):
    return f"{x:,.2f}" if isinstance(x, (int, float)) and math.isfinite(x) else "n/a"


def load_env_file(path=".env"):
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass
