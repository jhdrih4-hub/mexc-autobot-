import os
import time

import numpy as np
import pandas as pd

from .database import Database
from .config import AppConfig
from .utils import LOG, clamp, safe_float

try:
    import joblib
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    SKLEARN = True
except Exception:
    SKLEARN = False

HORIZON = 4


def _features(df):
    c = df["close"]
    v = df["volume"]
    f = pd.DataFrame(index=df.index)
    f["ret1"] = c.pct_change(1)
    f["ret4"] = c.pct_change(4)
    f["ret12"] = c.pct_change(12)
    m20 = c.rolling(20).mean()
    s20 = c.rolling(20).std(ddof=0)
    f["z20"] = (c - m20) / (s20 + 1e-12)
    e9 = c.ewm(span=9, adjust=False).mean()
    e21 = c.ewm(span=21, adjust=False).mean()
    e50 = c.ewm(span=50, adjust=False).mean()
    e200 = c.ewm(span=200, adjust=False).mean()
    f["ema_gap"] = (e9 - e21) / (c + 1e-12)
    f["trend_gap"] = (e50 - e200) / (c + 1e-12)
    f["vol_norm"] = s20 / (c + 1e-12)
    vm = v.rolling(20).mean()
    f["volr"] = v / (vm + 1e-12)
    return f


def build_dataset(df, horizon=HORIZON):
    if not SKLEARN or len(df) < 300:
        return None, None
    f = _features(df).iloc[200:]
    fut = (df["close"].shift(-horizon) / df["close"] - 1.0).iloc[200:]
    ok = f.dropna().index.intersection(fut.dropna().index)
    if len(ok) < 200:
        return None, None
    return f.loc[ok], (fut.loc[ok] > 0).astype(int)


class MLEngine:
    def __init__(self, config: AppConfig, db: Database):
        self.cfg = config
        self.db = db
        self.model = None
        self.name = None
        if config.ml_enabled and not SKLEARN:
            LOG.warning("ML_ENABLED but scikit-learn unavailable - ML disabled")

    def models_dir(self):
        p = os.path.join(self.cfg.data_dir, "models")
        os.makedirs(p, exist_ok=True)
        return p

    def maybe_train(self, df_map: dict):
        if not (self.cfg.ml_enabled and SKLEARN):
            return
        last = safe_float(self.db.get_meta("ml:last_train"), 0.0)
        if time.time() - last < self.cfg.ml_retrain_days * 86400:
            return
        Xs, ys = [], []
        for sym, df in df_map.items():
            X, y = build_dataset(df)
            if X is not None:
                Xs.append(X)
                ys.append(y)
        if not Xs:
            LOG.warning("ML: no usable datasets")
            return
        X, y = pd.concat(Xs), pd.concat(ys)
        n = len(X)
        if n < 500:
            LOG.warning("ML: not enough samples (%d)", n)
            return
        accs = []
        for k in range(4):
            tr_end = int(n * (0.5 + 0.125 * k))
            te_end = int(n * (0.5 + 0.125 * (k + 1)))
            if te_end - tr_end < 50:
                continue
            pipe = make_pipeline(StandardScaler(), LogisticRegression(max_iter=200))
            pipe.fit(X.iloc[:tr_end], y.iloc[:tr_end])
            accs.append(float((pipe.predict(X.iloc[tr_end:te_end]) ==
                               y.iloc[tr_end:te_end]).mean()))
        mean_acc = float(np.mean(accs)) if accs else 0.0
        if len(accs) < 3 or mean_acc <= 0.53:
            LOG.info("ML: walk-forward acc %.3f insufficient - model NOT activated", mean_acc)
            self.db.set_meta("ml:last_train", str(time.time()))
            return
        pipe = make_pipeline(StandardScaler(), LogisticRegression(max_iter=200))
        pipe.fit(X, y)
        name = f"lr_{time.strftime('%Y%m%d%H%M%S')}.joblib"
        path = os.path.join(self.models_dir(), name)
        joblib.dump(pipe, path)
        self.db.save_model_version(name, path,
                                   {"walk_forward_acc": mean_acc, "folds": len(accs),
                                    "samples": n}, active=True)
        self.db.set_meta("ml:active_model", name)
        self.db.set_meta("ml:last_train", str(time.time()))
        self.model, self.name = pipe, name
        LOG.info("ML trained: %s acc=%.3f folds=%d n=%d", name, mean_acc, len(accs), n)

    def tilt(self, df) -> float:
        if not (self.cfg.ml_enabled and SKLEARN and self.model is not None):
            return 0.0
        try:
            f = _features(df).iloc[[-1]].dropna()
            if f.empty:
                return 0.0
            p = float(self.model.predict_proba(f)[0][1])
            return clamp((p - 0.5) * 2 * 0.1, -0.1, 0.1)
        except Exception as e:
            LOG.warning("ML tilt failed: %s", e)
            return 0.0
