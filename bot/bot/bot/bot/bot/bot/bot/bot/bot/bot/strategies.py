import time

import pandas as pd

from .config import AppConfig
from .indicators import adx, atr, bollinger, donchian, ema, last, macd, rolling_vwap, rsi
from .models import Decision, Signal
from .utils import clamp, safe_float

MEAN_REVERT = {"bollinger_reversion", "vwap_reversion", "rsi_momentum"}


def _sig(ts, symbol, tf, name, direction, strength, price, reason=""):
    return Signal(ts=ts, symbol=symbol, timeframe=tf, strategy=name,
                  direction=direction, strength=float(clamp(strength, 0.0, 1.0)),
                  price=float(price), reason=reason)


class StrategyEngine:
    def __init__(self, config: AppConfig):
        self.cfg = config

    def indicators(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        _, _, hist = macd(close)
        _, _, _, _, pb = bollinger(close)
        ax, pdi, mdi = adx(df)
        vwap = rolling_vwap(df, 50)
        don_h, don_l = donchian(df, 20)
        price = last(close, 0.0)
        ind = {"price": price,
               "ema9": last(ema(close, 9)), "ema21": last(ema(close, 21)),
               "ema50": last(ema(close, 50)), "ema200": last(ema(close, 200)),
               "atr": last(atr(df)), "adx": last(ax), "pdi": last(pdi), "mdi": last(mdi),
               "rsi": last(rsi(close)),
               "macd_hist": last(hist),
               "macd_hist_prev": safe_float(hist.iloc[-2]) if len(hist) > 1 else None,
               "bb_pb": last(pb), "vwap": last(vwap),
               "don_high": safe_float(don_h.iloc[-2]) if len(don_h) > 1 else None,
               "don_low": safe_float(don_l.iloc[-2]) if len(don_l) > 1 else None}
        vma = last(df["volume"].rolling(20).mean())
        ind["vol_ratio"] = float(df["volume"].iloc[-1]) / vma if vma else None
        ind["atr_pct"] = (ind["atr"] / price * 100) if (ind["atr"] and price) else None
        return ind

    def compute_full(self, symbol, df, df_high=None, ml_tilt=0.0):
        ts = time.time()
        tf = self.cfg.primary_timeframe
        names = list(self.cfg.strategy_weights.keys())
        if df is None or len(df) < 60:
            d = Decision(ts=ts, symbol=symbol, action="hold", score=0.0, confidence=0.0,
                         reasons=["insufficient data"], regime="unknown", data_ok=False)
            sigs = [_sig(ts, symbol, tf, n, "flat", 0, 0.0, "insufficient data") for n in names]
            return d, sigs, {}
        ind = self.indicators(df)
        price = ind["price"]
        sigs = [self._trend_follow(ts, symbol, tf, price, ind),
                self._ema_cross(ts, symbol, tf, price, ind),
                self._rsi_momentum(ts, symbol, tf, price, ind),
                self._macd(ts, symbol, tf, price, ind),
                self._bollinger_rev(ts, symbol, tf, price, ind),
                self._volume_breakout(ts, symbol, tf, price, ind),
                self._vwap_reversion(ts, symbol, tf, price, ind),
                self._multi_timeframe(ts, symbol, tf, price, df_high)]
        return self.aggregate(sigs, ind, ml_tilt=ml_tilt), sigs, ind

    def aggregate(self, sigs, ind, ml_tilt=0.0) -> Decision:
        ts = time.time()
        symbol, tf, price = sigs[0].symbol, sigs[0].timeframe, sigs[0].price
        ax_v, atr_pct = ind.get("adx"), ind.get("atr_pct")
        regime = "ranging"
        if ax_v is not None and ax_v >= 25:
            regime = "uptrend" if (ind.get("pdi") or 0) > (ind.get("mdi") or 0) else "downtrend"
        w_map = self.cfg.strategy_weights
        num = den = 0.0
        nonflat = 0
        for s in sigs:
            w = float(w_map.get(s.strategy, 0.5))
            if regime != "ranging" and s.strategy in MEAN_REVERT:
                w *= 0.5
            if regime == "ranging" and s.strategy in {"trend_follow", "volume_breakout"}:
                w *= 0.5
            den += w
            if s.direction == "flat" or s.strength <= 0:
                continue
            d = 1.0 if s.direction == "long" else -1.0
            num += w * s.strength * d
            nonflat += 1
        net = num / den if den > 0 else 0.0
        score = clamp(net + ml_tilt, -1.0, 1.0)
        aligned = sum(1 for s in sigs if s.direction != "flat" and
                      ((score > 0 and s.direction == "long") or
                       (score < 0 and s.direction == "short")))
        agreement = aligned / nonflat if nonflat else 0.0
        reasons = [f"{s.strategy}:{s.direction}:{s.strength:.2f}" for s in sigs
                   if s.direction != "flat"]
        action = "hold"
        if atr_pct is not None and atr_pct > self.cfg.risk.max_atr_pct:
            reasons.append(f"blocked: ATR% {atr_pct:.2f} > {self.cfg.risk.max_atr_pct}")
        elif nonflat >= self.cfg.min_nonflat_signals and agreement >= self.cfg.min_agreement:
            if score >= self.cfg.buy_score_threshold:
                action = "buy"
            elif score <= self.cfg.sell_score_threshold:
                action = "sell"
            else:
                reasons.append("score below threshold")
        else:
            reasons.append(f"insufficient agreement ({aligned}/{nonflat})")
        confidence = clamp(abs(score) * (0.5 + 0.5 * agreement), 0.0, 1.0)
        return Decision(ts=ts, symbol=symbol, action=action, score=score,
                        confidence=confidence, reasons=reasons[:12],
                        regime=regime, data_ok=True)

    # ---- individual strategies
    def _trend_follow(self, ts, symbol, tf, price, ind):
        e50, e200, ax = ind.get("ema50"), ind.get("ema200"), ind.get("adx")
        if not (e50 and ax is not None):
            return _sig(ts, symbol, tf, "trend_follow", "flat", 0, price, "warming up")
        e2 = e200 or e50
        if e50 > e2 and price > e50 and ax >= 20:
            return _sig(ts, symbol, tf, "trend_follow", "long",
                        clamp((ax - 15) / 35, 0, 1), price, f"uptrend ADX={ax:.0f}")
        if e50 < e2 and price < e50 and ax >= 20:
            return _sig(ts, symbol, tf, "trend_follow", "short",
                        clamp((ax - 15) / 35, 0, 1), price, f"downtrend ADX={ax:.0f}")
        return _sig(ts, symbol, tf, "trend_follow", "flat", 0, price, "neutral")

    def _ema_cross(self, ts, symbol, tf, price, ind):
        e9, e21, a = ind.get("ema9"), ind.get("ema21"), ind.get("atr")
        if not (e9 and e21 and a):
            return _sig(ts, symbol, tf, "ema_cross", "flat", 0, price, "warming up")
        d = e9 - e21
        if d > 0:
            return _sig(ts, symbol, tf, "ema_cross", "long", clamp(abs(d) / (2 * a), 0, 1),
                        price, "EMA9>EMA21")
        if d < 0:
            return _sig(ts, symbol, tf, "ema_cross", "short", clamp(abs(d) / (2 * a), 0, 1),
                        price, "EMA9<EMA21")
        return _sig(ts, symbol, tf, "ema_cross", "flat", 0, price, "neutral")

    def _rsi_momentum(self, ts, symbol, tf, price, ind):
        r = ind.get("rsi")
        if r is None:
            return _sig(ts, symbol, tf, "rsi_momentum", "flat", 0, price, "warming up")
        if r <= 32:
            return _sig(ts, symbol, tf, "rsi_momentum", "long", clamp((35 - r) / 20, 0.2, 1),
                        price, f"RSI={r:.0f} oversold")
        if r >= 68:
            return _sig(ts, symbol, tf, "rsi_momentum", "short", clamp((r - 65) / 20, 0.2, 1),
                        price, f"RSI={r:.0f} overbought")
        return _sig(ts, symbol, tf, "rsi_momentum", "flat", 0, price, "neutral")

    def _macd(self, ts, symbol, tf, price, ind):
        mh, mp, a = ind.get("macd_hist"), ind.get("macd_hist_prev"), ind.get("atr")
        if not (mh is not None and mp is not None and a):
            return _sig(ts, symbol, tf, "macd", "flat", 0, price, "warming up")
        if mh > 0 and mh >= mp:
            return _sig(ts, symbol, tf, "macd", "long", clamp(mh / (3 * a), 0.2, 1),
                        price, "MACD positive & rising")
        if mh < 0 and mh <= mp:
            return _sig(ts, symbol, tf, "macd", "short", clamp(-mh / (3 * a), 0.2, 1),
                        price, "MACD negative & falling")
        return _sig(ts, symbol, tf, "macd", "flat", 0, price, "weak")

    def _bollinger_rev(self, ts, symbol, tf, price, ind):
        pb = ind.get("bb_pb")
        if pb is None:
            return _sig(ts, symbol, tf, "bollinger_reversion", "flat", 0, price, "warming up")
        if pb <= 0.05:
            return _sig(ts, symbol, tf, "bollinger_reversion", "long",
                        clamp((0.05 - pb) * 12, 0.2, 1), price, f"%B={pb:.2f} lower band")
        if pb >= 0.95:
            return _sig(ts, symbol, tf, "bollinger_reversion", "short",
                        clamp((pb - 0.95) * 12, 0.2, 1), price, f"%B={pb:.2f} upper band")
        return _sig(ts, symbol, tf, "bollinger_reversion", "flat", 0, price, "inside bands")

    def _volume_breakout(self, ts, symbol, tf, price, ind):
        vr, dh = ind.get("vol_ratio"), ind.get("don_high")
        if not (vr and dh):
            return _sig(ts, symbol, tf, "volume_breakout", "flat", 0, price, "no breakout")
        if vr >= 1.8 and price >= dh * 0.995:
            return _sig(ts, symbol, tf, "volume_breakout", "long",
                        clamp((vr - 1.5) / 1.5, 0.2, 1), price, f"volume {vr:.1f}x + breakout")
        return _sig(ts, symbol, tf, "volume_breakout", "flat", 0, price, "no breakout")

    def _vwap_reversion(self, ts, symbol, tf, price, ind):
        vw, a = ind.get("vwap"), ind.get("atr")
        if not (vw and a and price):
            return _sig(ts, symbol, tf, "vwap_reversion", "flat", 0, price, "warming up")
        z = (price - vw) / a
        if z <= -2.0:
            return _sig(ts, symbol, tf, "vwap_reversion", "long", clamp(-z / 4, 0.2, 1),
                        price, f"{z:.1f} ATR below VWAP")
        if z >= 2.0:
            return _sig(ts, symbol, tf, "vwap_reversion", "short", clamp(z / 4, 0.2, 1),
                        price, f"{z:.1f} ATR above VWAP")
        return _sig(ts, symbol, tf, "vwap_reversion", "flat", 0, price, "near vwap")

    def _multi_timeframe(self, ts, symbol, tf, price, df_high):
        if df_high is None or len(df_high) < 60:
            return _sig(ts, symbol, tf, "multi_timeframe", "flat", 0, price, "no HTF data")
        ind = self.indicators(df_high)
        e50, e200, ax = ind.get("ema50"), ind.get("ema200"), ind.get("adx")
        if not (e50 and ax is not None):
            return _sig(ts, symbol, tf, "multi_timeframe", "flat", 0, price, "HTF warming up")
        e2 = e200 or e50
        s = clamp((ax - 15) / 35, 0, 1) * 0.8
        if e50 > e2:
            return _sig(ts, symbol, tf, "multi_timeframe", "long", s, price, "HTF bullish")
        if e50 < e2:
            return _sig(ts, symbol, tf, "multi_timeframe", "short", s, price, "HTF bearish")
        return _sig(ts, symbol, tf, "multi_timeframe", "flat", 0, price, "HTF neutral")
