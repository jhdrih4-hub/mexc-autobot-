import os
import time

import numpy as np
import pandas as pd

from .config import AppConfig
from .errors import TradingSystemError
from .exchange import CcxtAdapter
from .strategies import StrategyEngine
from .utils import LOG, TF_SECONDS


def fetch_history(adapter, symbol, timeframe, days, max_bars=4000):
    now_ms = int(time.time() * 1000)
    since = now_ms - int(days * 86400 * 1000)
    frames = []
    while since < now_ms and sum(len(f) for f in frames) < max_bars:
        try:
            df = adapter.fetch_ohlcv_df(symbol, timeframe, limit=500, since_ms=since)
        except TradingSystemError:
            break
        if df.empty:
            break
        frames.append(df)
        last_ts = int(df["ts"].iloc[-1].value / 1e6)
        if last_ts <= since:
            break
        since = last_ts + 1
        time.sleep(0.15)
    if not frames:
        return None
    df = pd.concat(frames).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    sec = TF_SECONDS[timeframe]
    close_time = df["ts"] + pd.to_timedelta(sec, unit="s")
    df = df[close_time <= pd.Timestamp.now(tz="UTC")].reset_index(drop=True)
    return df.iloc[-max_bars:].reset_index(drop=True) if len(df) > max_bars else df


def _simulate(symbol, df, strat, config):
    rl = config.risk
    fee = config.paper_fee_rate
    slip = config.paper_slippage_bps / 10000.0
    equity = config.paper_start_balance
    trades = []
    n = len(df)
    i = 210
    while i < n - 1:
        window = df.iloc[max(0, i - 300):i + 1]
        d, _, ind = strat.compute_full(symbol, window, None, ml_tilt=0.0)
        if d.action == "buy":
            entry_i = i + 1
            entry = float(df["open"].iloc[entry_i]) * (1 + slip)
            a = ind.get("atr") or entry * rl.default_stop_loss_pct / 100.0
            stop = entry - a * rl.atr_stop_multiplier
            take = entry + a * rl.atr_target_multiplier
            qty = (equity * rl.max_risk_per_trade_pct / 100.0) / max(1e-9, a * rl.atr_stop_multiplier)
            qty = min(qty, equity * rl.max_position_value_pct / 100.0 / entry)
            if qty * entry < rl.min_order_quote_usdt:
                i += 1
                continue
            exit_price = None
            reason = ""
            trail_high = entry
            j = entry_i
            end = min(n - 1, entry_i + rl.max_hold_bars)
            while j <= end:
                hi = float(df["high"].iloc[j])
                lo = float(df["low"].iloc[j])
                trail_high = max(trail_high, hi)
                profit_pct = (trail_high - entry) / entry * 100.0
                if lo <= stop:
                    exit_price, reason = stop, "stop_loss"
                    break
                if hi >= take:
                    exit_price, reason = take, "take_profit"
                    break
                if profit_pct >= rl.trail_activation_pct:
                    ts_ = trail_high * (1 - rl.trail_distance_pct / 100.0)
                    if lo <= ts_:
                        exit_price, reason = ts_, "trailing_stop"
                        break
                if j == end:
                    exit_price = float(df["open"].iloc[j + 1]) if j + 1 < n else float(df["close"].iloc[j])
                    reason = "time_stop"
                j += 1
            if exit_price is None:
                exit_price, reason = float(df["close"].iloc[-1]), "end_of_data"
            exit_price = exit_price * (1 - slip)
            gross = (exit_price - entry) * qty
            fees = (entry + exit_price) * qty * fee
            net = gross - fees
            equity += net
            trades.append({"symbol": symbol,
                           "opened_at": df["ts"].iloc[entry_i].timestamp(),
                           "closed_at": df["ts"].iloc[min(j, n - 1)].timestamp(),
                           "entry": entry, "exit": exit_price, "qty": qty,
                           "net": net, "reason": reason})
            i = j + 1
        else:
            i += 1
    return trades


def run_backtest(config: AppConfig, adapter: CcxtAdapter):
    strat = StrategyEngine(config)
    rl = config.risk
    symbols = config.symbols or adapter.top_usdt_symbols(5, rl.min_24h_quote_volume_usd)
    all_trades = []
    for symbol in symbols:
        df = fetch_history(adapter, symbol, config.primary_timeframe,
                           config.backtest_days, config.max_bt_bars)
        if df is None or len(df) < 220:
            LOG.warning("backtest %s: insufficient history", symbol)
            continue
        trades = _simulate(symbol, df, strat, config)
        all_trades.extend(trades)
        net = sum(t["net"] for t in trades)
        wins = sum(1 for t in trades if t["net"] > 0)
        LOG.info("backtest %s: trades=%d win=%d net=%.2f", symbol, len(trades), wins, net)
    n = len(all_trades)
    wins = sum(1 for t in all_trades if t["net"] > 0)
    gp = sum(t["net"] for t in all_trades if t["net"] > 0)
    gl = -sum(t["net"] for t in all_trades if t["net"] <= 0)
    equity = config.paper_start_balance
    curve = [equity]
    for t in sorted(all_trades, key=lambda x: x["closed_at"]):
        equity += t["net"]
        curve.append(equity)
    curve = np.array(curve)
    peak = np.maximum.accumulate(curve)
    dd = ((peak - curve) / peak * 100.0) if len(curve) else np.array([0.0])
    print("\n===== BACKTEST SUMMARY (simulated, spot long-only) =====")
    print(f"symbols       : {', '.join(symbols)}")
    print(f"period        : {config.backtest_days} days | tf {config.primary_timeframe}")
    print(f"trades        : {n} (win rate {100 * wins / n if n else 0:.1f}%)")
    print(f"net PnL       : {equity - config.paper_start_balance:+.2f} USDT")
    print(f"profit factor : {gp / gl if gl > 0 else float('inf'):.2f}")
    print(f"max drawdown  : {float(dd.max()):.2f}%")
    print("NOTE: simulation != live results. No profit guarantee.")
    try:
        os.makedirs(config.data_dir, exist_ok=True)
        pd.DataFrame(all_trades).to_csv(
            os.path.join(config.data_dir, "backtest_trades.csv"), index=False)
        print(f"trades saved  : {os.path.join(config.data_dir, 'backtest_trades.csv')}")
    except OSError:
        pass
    return all_trades
