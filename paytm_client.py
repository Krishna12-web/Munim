"""Paytm Settlement API client (production only; the API is not hosted on staging).
Docs: https://www.paytmpayments.com/docs/api/settlement-api
One call covers at most one day and 20 rows per page, so we loop over days and pages."""
import json, os, time
from datetime import date, timedelta
import pandas as pd
import requests

URL = "https://secure.paytmpayments.com/merchant-settlement-service/settlement/list"


def _sign(body, key):
    from paytmchecksum import PaytmChecksum  # pip install paytmchecksum
    return PaytmChecksum.generateSignature(json.dumps(body), key)


def fetch_day(mid, key, day):
    rows, page = [], 1
    while True:
        body = {"MID": mid, "utrProcessedStartTime": day, "pageNum": page, "pageSize": 20}
        body["checksumHash"] = _sign({k: v for k, v in body.items()}, key)
        r = requests.post(URL, json=body, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("status") == "RECORD_NOT_FOUND":
            break
        if j.get("status") != "SUCCESS":
            raise RuntimeError("Paytm error %s: %s" % (j.get("resultCode"), j.get("errorMessage")))
        rows += j.get("settlementDetailList") or []
        if page >= (j.get("paginatorTotalPage") or 1):
            break
        page += 1
        time.sleep(0.2)  # the API rate limits per second (result code 00000074)
    return rows


def fetch(mid=None, key=None, days=14):
    mid, key = mid or os.getenv("PAYTM_MID"), key or os.getenv("PAYTM_MERCHANT_KEY")
    if not mid or not key:
        raise RuntimeError("Set PAYTM_MID and PAYTM_MERCHANT_KEY in .env")
    out = []
    for i in range(days, 0, -1):
        out += fetch_day(mid, key, str(date.today() - timedelta(days=i)))
        time.sleep(0.3)
    return out


def to_frame(rows):
    """Keep sale transactions, dedupe by txnId, map to the app's schema."""
    seen, recs = set(), []
    for r in rows:
        if r.get("txnType") != "TRANSACTION" or r.get("txnId") in seen:
            continue
        seen.add(r.get("txnId"))
        recs.append(r)
    if not recs:
        raise ValueError("No settled sale transactions in that period")
    ts = pd.to_datetime([r["txnDate"] for r in recs])
    return pd.DataFrame({
        "date": ts.normalize(), "time": ts.strftime("%H:%M"),
        "name": [r.get("custId") or "Walk-in" for r in recs],
        "amount": [float(r["txnAmount"]) for r in recs],
        "mode": [r.get("paymentMode", "") for r in recs], "phone": ""})
