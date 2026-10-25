"""Run: python tests/check_core.py  (plain asserts, no pytest needed)"""
import json, sys, types, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import core

# 1. synthetic data: the two regulars who stopped must be found, active ones must not
df = core.synthetic()
names = [x["name"] for x in core.lapsed(df)]
assert "Geeta Sharma" in names and "Vikram Singh" in names, names
assert "Ramesh Kumar" not in names and "Sunita Devi" not in names, names

# 2. forecast: 7 days, ordered intervals, backtest numbers present for 45 days
fc = core.forecast(core.daily(df))
assert len(fc["days"]) == 7 and all(d["lo"] <= d["pred"] <= d["hi"] for d in fc["days"])
assert fc["wape"] is not None and fc["naive_wape"] is not None
print("forecast backtest error %s%% vs naive %s%%" % (fc["wape"], fc["naive_wape"]))
short = df[df.date >= df.date.max() - pd.Timedelta(days=9)]
assert core.forecast(core.daily(short)) is None

# 3. anomaly: inject a day with 10x revenue inside the last 14 days
d = core.daily(df)
d2 = d.copy(); d2.iloc[-5, 0] = d2["total"].mean() * 10
flag = core.anomalies(d2)
assert any(a["date"] == str(d2.index[-5].date()) for a in flag), flag

# 4. CSV loading: DD/MM/YYYY file, ISO dates, failed rows dropped, bad files rejected
p = "/mnt/user-data/outputs/sample_payments.csv"
if os.path.exists(p):
    c = core.load_csv(open(p, "rb").read())
    assert c["date"].max() == pd.Timestamp("2026-10-22") and c["date"].min() == pd.Timestamp("2026-09-08")
iso = b"Date,Amount,Status,Customer\n2026-10-01,100,SUCCESS,A\n2026-10-02,50,FAILED,A\n2026-10-02,70,SUCCESS,\n"
c = core.load_csv(iso)
assert len(c) == 2 and c["name"].tolist() == ["A", "Walk-in"], c
for bad in (b"a,b\n1,2\n", b"date,amount\n2026-10-01,5\n2026-10-01,6\n"):
    try:
        core.load_csv(bad); raise SystemExit("should have failed")
    except ValueError:
        pass

# 5. UI payload is JSON-serialisable and consistent
s = core.state(df, "test"); json.dumps(s)
assert len(s["PREV"]) == 6 and len(s["DAYS"]) == 7
assert abs(sum(r[2] for r in s["T"]) - core.latest_day(df)["total"]) < 0.01

# 6. agent tool dispatch (anthropic stubbed, no network)
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import agent
for t in agent.TOOLS:
    json.dumps(agent.dispatch(t["name"], df))
print("all checks passed")

# 7. win-back tracking on the demo files (follow-up file is simulated)
import core as _c
base = _c.load_csv(open("demo_payments.csv", "rb").read())
follow = _c.load_csv(open("demo_followup_simulated.csv", "rb").read())
log = {"Vikram Singh": "2026-10-22", "Geeta Sharma": "2026-10-22", "Rohit Mehra": "2026-10-22"}
assert _c.winback_results(base, log) == [{"name": n, "contacted": "2026-10-22", "returned": False, "recovered": 0.0} for n in log]
res = {r["name"]: r for r in _c.winback_results(follow, log)}
assert res["Vikram Singh"]["recovered"] == 590 and res["Geeta Sharma"]["recovered"] == 410 and not res["Rohit Mehra"]["returned"]
st0 = _c.state(base, "demo", log)
assert st0["AT_RISK"] > 0 and {x[0] for x in st0["LAPSED"]} >= {"Vikram Singh", "Geeta Sharma", "Rohit Mehra", "Neha Gupta"}
assert [x[5] for x in st0["LAPSED"] if x[0] == "Vikram Singh"] == ["2026-10-22"]
print("win-back checks passed; revenue at risk", st0["AT_RISK"])
