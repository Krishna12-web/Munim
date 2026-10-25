"""Data loading and analytics. No web or LLM code in this file, so it can be tested on its own."""
import io, re
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # index = pandas dayofweek
OK = re.compile(r"success|settled|paid|complete", re.I)


def _col(cols, pat):
    for k, c in cols.items():
        if re.search(pat, k):
            return c
    return None


def load_csv(raw: bytes) -> pd.DataFrame:
    """Parse a payment report. Needs date and amount columns. Dates are read day first (DD/MM/YYYY) or ISO."""
    df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    cols = {c.strip().lower(): c for c in df.columns}
    d, a = _col(cols, "date"), _col(cols, "amount|amt")
    if not d or not a:
        raise ValueError("CSV needs a date column and an amount column")
    n, s = _col(cols, "name|payer|customer|party|vpa"), _col(cols, "status")
    t, m, p = _col(cols, "time"), _col(cols, "mode|method"), _col(cols, "phone|mobile")
    get = lambda c: df[c].str.strip() if c else pd.Series("", index=df.index)
    out = pd.DataFrame({
        "date": pd.to_datetime(get(d), dayfirst=True, format="mixed", errors="coerce").dt.normalize(),
        "time": get(t).str[:5],
        "name": get(n).replace("", "Walk-in"),
        "amount": pd.to_numeric(get(a).str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce"),
        "mode": get(m),
        "phone": get(p),
    })
    ok = out["date"].notna() & (out["amount"] > 0)
    if s:
        ok &= get(s).map(lambda x: bool(OK.search(x)))
    out = out[ok].reset_index(drop=True)
    if out["date"].nunique() < 2:
        raise ValueError("Need payments on at least two different days")
    return out


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.assign(date=df["date"].dt.strftime("%Y-%m-%d")).to_csv(index=False).encode()


def daily(df):
    g = df.groupby("date")["amount"].agg(total="sum", n="count")
    return g.reindex(pd.date_range(g.index.min(), g.index.max()), fill_value=0)


def _X(idx, t0):
    X = np.zeros((len(idx), 7))
    X[np.arange(len(idx)), idx.dayofweek] = 1.0          # one weekday profile; no trend term (risky over 7 days)
    return X


def _fit(y, t0):
    """Weekday profile by weighted ridge. One robust pass: days far from the first fit (more than 3 robust sigmas,
    like a one-off bulk order or a power cut) are clipped before refitting, so they do not bend the profile."""
    X = _X(y.index, t0)
    w = 0.5 ** ((len(y) - 1 - np.arange(len(y))) / 14.0)   # recent days count more (half-life 14 days)
    m = Ridge(alpha=1e-3, fit_intercept=False).fit(X, y.values, sample_weight=w)
    res = y.values - m.predict(X)
    mad = np.median(np.abs(res - np.median(res))) * 1.4826
    if mad > 0:
        fit = m.predict(X)
        m = Ridge(alpha=1e-3, fit_intercept=False).fit(X, np.clip(y.values, fit - 3 * mad, fit + 3 * mad), sample_weight=w)
    return m


def forecast(d, horizon=7):
    """Weekday profile weighted toward recent days. Backtests on the last 7 days against 'same day last week'."""
    y = d["total"]
    if len(y) < 14:
        return None
    t0 = y.index[0]
    m = _fit(y, t0)
    sd = float((y.values - m.predict(_X(y.index, t0))).std(ddof=1))
    fut = pd.date_range(y.index[-1] + pd.Timedelta(days=1), periods=horizon)
    p = np.maximum(m.predict(_X(fut, t0)), 0)
    days = [{"day": WD[x.dayofweek], "date": str(x.date()), "pred": int(round(v)),
             "lo": int(round(max(v - 1.28 * sd, 0))), "hi": int(round(v + 1.28 * sd))} for x, v in zip(fut, p)]
    wape = naive = None
    if len(y) >= 28:
        te = y.iloc[-7:]
        pm = _fit(y.iloc[:-7], t0).predict(_X(te.index, t0))
        base = y.shift(7).iloc[-7:].values
        if te.values.sum() > 0:
            wape = round(100 * abs(te.values - pm).sum() / te.values.sum(), 1)
            naive = round(100 * abs(te.values - base).sum() / te.values.sum(), 1)
    return {"days": days, "wape": wape, "naive_wape": naive}


def anomalies(d, z_cut=3.0, last=14):
    """Robust z-score (median and MAD) of daily revenue after removing the weekday profile."""
    y = d["total"]
    if len(y) < 14:
        return []
    res = y.values - _fit(y, y.index[0]).predict(_X(y.index, y.index[0]))
    mad = np.median(np.abs(res - np.median(res))) * 1.4826
    if mad == 0:
        return []
    z = (res - np.median(res)) / mad
    out = []
    for i in range(max(0, len(y) - last), len(y)):
        if abs(z[i]) > z_cut:
            out.append({"date": str(y.index[i].date()), "total": round(float(y.values[i]), 2),
                        "expected": round(float(y.values[i] - res[i]), 2), "z": round(float(z[i]), 1)})
    return out


def lapsed(df, min_visits=3, min_days=7, p_cut=0.85):
    """Probabilistic recency model. If visits arrive at a steady rate (mean gap g), the chance a still-active
    customer stays away for `since` days is exp(-since/g). prob = 1 - that. Ranked by monthly value at risk."""
    asof, rows = df["date"].max(), []
    named = df[~df["name"].str.contains("walk|unknown", case=False, regex=True)]
    for name, g in named.groupby("name"):
        days = np.sort(g["date"].unique())
        if len(days) < min_visits:
            continue
        gap = max(float(np.diff(days).astype("timedelta64[D]").astype(int).mean()), 1.0)
        since = int((asof - pd.Timestamp(days[-1])).days)
        prob = 1 - float(np.exp(-since / gap))
        if since < min_days or prob < p_cut:
            continue
        per_visit = float(g.groupby("date")["amount"].sum().mean())
        phone = next((p for p in g["phone"] if p), "")
        rows.append({"name": name, "days_since_visit": since, "typical_gap_days": round(gap, 1),
                     "prob_stopped": round(prob, 2), "avg_spend": round(per_visit, 2),
                     "monthly_value": round(per_visit * 30 / gap, 2), "phone": phone})
    rows.sort(key=lambda r: r["monthly_value"] * r["prob_stopped"], reverse=True)
    return rows[:6]


def latest_day(df):
    d = daily(df)
    last = d.index[-1]
    g = df[df["date"] == last]
    big = g.loc[g["amount"].idxmax()]
    prev = d["total"].iloc[-7:-1]
    avg = float(prev.mean())
    return {"date": str(last.date()), "weekday": WD[last.dayofweek], "total": round(float(d["total"].iloc[-1]), 2),
            "payments": int(len(g)), "largest": {"customer": big["name"], "amount": round(float(big["amount"]), 2)},
            "avg_previous_6_days": round(avg, 2),
            "percent_vs_avg": round(100 * (float(d["total"].iloc[-1]) - avg) / avg, 1) if avg else None}


def week_totals(df):
    d = daily(df)["total"].iloc[-7:]
    return [{"date": str(i.date()), "day": WD[i.dayofweek], "total": round(float(v), 2)} for i, v in d.items()]


def winback_results(df, log):
    """log maps customer name to the data date on which the merchant contacted them. A customer counts as
    won back if they paid on any later day; recovered is the sum of those later payments."""
    out = []
    for name, day in log.items():
        g = df[(df["name"] == name) & (df["date"] > pd.Timestamp(day))]
        out.append({"name": name, "contacted": day, "returned": bool(len(g)), "recovered": round(float(g["amount"].sum()), 2)})
    return out


def state(df, source="", log=None):
    """Everything the web UI needs in one payload."""
    log = log or {}
    lap = lapsed(df)
    d = daily(df)
    last = d.index[-1]
    rows = df[df["date"] == last].sort_values("time")
    prev = d["total"].iloc[-7:-1]
    return {
        "source": source,
        "T": [[r.time, r.name, round(float(r.amount), 2), r.mode] for r in rows.itertuples()],
        "PREV": [round(float(v), 2) for v in prev],
        "DAYS": [WD[i.dayofweek] for i in list(prev.index) + [last]],
        "LAPSED": [[x["name"], x["days_since_visit"], int(round(x["avg_spend"])), x["phone"], x["prob_stopped"], log.get(x["name"])] for x in lap],
        "AT_RISK": round(sum(x["monthly_value"] for x in lap), 2),
        "WINBACK": winback_results(df, log),
        "SET": None,
        "forecast": forecast(d),
        "anomalies": anomalies(d),
    }


def synthetic(seed=7, days=45):
    """Demo data with a few regulars who stop coming. Used only when no real data has been loaded."""
    rng = np.random.default_rng(seed)
    reg = {"Ramesh Kumar": (2, 240), "Sunita Devi": (3, 120), "Anil Gupta": (5, 810), "Pooja Verma": (4, 330),
           "Mohd. Saleem": (6, 1260), "Kavita Rani": (3, 185), "Geeta Sharma": (3, 380), "Vikram Singh": (4, 620)}
    stop = {"Geeta Sharma": 24, "Vikram Singh": 19}
    end = pd.Timestamp.today().normalize()
    rows = []
    for i in range(days):
        day = end - pd.Timedelta(days=days - 1 - i)
        left = days - 1 - i
        wk = 1.25 if day.dayofweek >= 5 else 1.0
        tm = lambda: "%02d:%02d" % (rng.integers(9, 21), rng.integers(0, 60))
        for n, (gap, amt) in reg.items():
            if n in stop and left < stop[n]:
                continue
            if rng.random() < 1 / gap:
                rows.append((day, tm(), n, round(amt * rng.uniform(.7, 1.3)), "UPI", ""))
        for _ in range(int(rng.integers(19, 26) * wk)):
            rows.append((day, tm(), "Walk-in", int(rng.integers(40, 200)), "UPI", ""))
    return pd.DataFrame(rows, columns=["date", "time", "name", "amount", "mode", "phone"]).astype({"amount": float})
