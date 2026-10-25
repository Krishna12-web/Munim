"""Run: python tests/check_auth.py (plain asserts; no pytest needed)."""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def create_account(client, email, password):
    response = client.post("/api/auth/signup", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"] is True
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    return response.json()["csrf_token"]


with tempfile.TemporaryDirectory() as directory:
    app.ACCOUNT_DB = Path(directory) / "accounts.sqlite3"
    app.LOGIN_ATTEMPTS.clear()
    app.SIGNUP_ATTEMPTS.clear()
    app.os.environ["COOKIE_SECURE"] = "false"
    assert app.Sync().days == 60

    first = TestClient(app.app)
    second = TestClient(app.app)

    assert first.get("/api/state").status_code == 401
    assert first.post("/api/auth/signup", json={"email": "bad", "password": "long enough password"}).status_code == 400
    assert first.post("/api/auth/signup", json={"email": "first@example.com", "password": "short"}).status_code == 400

    csrf_first = create_account(first, "first@example.com", "a-secure-password-1")
    assert first.get("/api/state").status_code == 200
    assert first.post("/api/import", files={"file": ("demo.csv", Path("demo_payments.csv").read_bytes(), "text/csv")}).status_code == 403

    imported = first.post(
        "/api/import",
        files={"file": ("demo.csv", Path("demo_payments.csv").read_bytes(), "text/csv")},
        headers={"X-CSRF-Token": csrf_first},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["source"] == "demo.csv"

    csrf_second = create_account(second, "second@example.com", "another-secure-pass-2")
    second_state = second.get("/api/state").json()
    assert second_state["source"] == "synthetic demo data"
    assert first.get("/api/state").json()["source"] == "demo.csv"
    second_csv = b"date,amount,status,customer\n2026-10-01,111,SUCCESS,Second User\n2026-10-02,222,SUCCESS,Second User\n"
    second_import = second.post(
        "/api/import",
        files={"file": ("second.csv", second_csv, "text/csv")},
        headers={"X-CSRF-Token": csrf_second},
    )
    assert second_import.status_code == 200, second_import.text
    assert second.get("/api/state").json()["source"] == "second.csv"
    assert first.get("/api/state").json()["source"] == "demo.csv"
    assert first.post("/api/auth/signup", json={"email": "FIRST@example.com", "password": "a-secure-password-1"}).status_code == 409
    assert second.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_second}).status_code == 200
    assert second.get("/api/state").status_code == 401

    login = second.post("/api/auth/login", json={"email": "second@example.com", "password": "another-secure-pass-2"})
    assert login.status_code == 200, login.text
    csrf_second = login.json()["csrf_token"]
    assert second.get("/api/auth/status").json()["authenticated"] is True
    assert first.post("/api/paytm/sync", json={}, headers={"X-CSRF-Token": csrf_first}).status_code == 409

    previous_credential_key = app.os.environ.get("PAYTM_CREDENTIALS_KEY")
    app.os.environ["PAYTM_CREDENTIALS_KEY"] = ""
    assert first.post(
        "/api/paytm/credentials",
        json={"mid": "NO-PLAINTEXT", "merchant_key": "no-plaintext"},
        headers={"X-CSRF-Token": csrf_first},
    ).status_code == 503
    app.os.environ["PAYTM_CREDENTIALS_KEY"] = Fernet.generate_key().decode("ascii")
    first_creds = {"mid": "MERCHANT-FIRST-1234", "merchant_key": "first-secret-merchant-key"}
    second_creds = {"mid": "MERCHANT-SECOND-5678", "merchant_key": "second-secret-merchant-key"}
    saved_first = first.post("/api/paytm/credentials", json=first_creds, headers={"X-CSRF-Token": csrf_first})
    saved_second = second.post("/api/paytm/credentials", json=second_creds, headers={"X-CSRF-Token": csrf_second})
    assert saved_first.status_code == saved_second.status_code == 200
    assert first.get("/api/paytm/status").json() == {"configured": True, "mid": "***************1234"}
    assert second.get("/api/paytm/status").json() == {"configured": True, "mid": "****************5678"}
    assert first_creds["merchant_key"] not in str(first.get("/api/paytm/status").json())
    with app._db() as db:
        encrypted = db.execute(
            "SELECT mid_encrypted, merchant_key_encrypted FROM paytm_credentials ORDER BY user_id"
        ).fetchall()
    assert len(encrypted) == 2
    assert all(first_creds["mid"].encode() not in bytes(row["mid_encrypted"]) for row in encrypted)
    assert all(second_creds["mid"].encode() not in bytes(row["mid_encrypted"]) for row in encrypted)
    assert all(first_creds["merchant_key"].encode() not in bytes(row["merchant_key_encrypted"]) for row in encrypted)
    assert all(second_creds["merchant_key"].encode() not in bytes(row["merchant_key_encrypted"]) for row in encrypted)

    calls = []
    def fake_fetch(mid, key, days):
        if days == 13:
            raise RuntimeError("provider echoed " + key)
        calls.append((mid, key, days))
        amount = "111" if key == first_creds["merchant_key"] else "222"
        return [
            {
                "txnType": "TRANSACTION",
                "txnId": mid + "-previous",
                "txnDate": "2026-10-04 12:30:00",
                "txnAmount": "10",
                "custId": "Paytm customer",
                "paymentMode": "UPI",
            },
            {
                "txnType": "TRANSACTION",
                "txnId": mid + "-latest",
                "txnDate": "2026-10-05 12:30:00",
                "txnAmount": amount,
                "custId": "Paytm customer",
                "paymentMode": "UPI",
            },
        ]
    original_fetch = app.paytm_client.fetch
    app.paytm_client.fetch = fake_fetch
    try:
        failed_sync = first.post(
            "/api/paytm/sync", json={"days": 13}, headers={"X-CSRF-Token": csrf_first}
        )
        assert failed_sync.status_code == 502
        assert first_creds["merchant_key"] not in failed_sync.text
        sync_first = first.post(
            "/api/paytm/sync", json={"days": 99}, headers={"X-CSRF-Token": csrf_first}
        )
        assert sync_first.status_code == 200, sync_first.text
        assert sync_first.json()["source"] == "Paytm settlement API"
        assert second.get("/api/state").json()["source"] == "second.csv"
        sync_second = second.post(
            "/api/paytm/sync", json={"days": 0}, headers={"X-CSRF-Token": csrf_second}
        )
        assert sync_second.status_code == 200, sync_second.text
        assert calls == [
            (first_creds["mid"], first_creds["merchant_key"], 60),
            (second_creds["mid"], second_creds["merchant_key"], 1),
        ]
        assert first.get("/api/state").json()["T"][0][2] == 111.0
        assert second.get("/api/state").json()["T"][0][2] == 222.0
        assert first.delete("/api/paytm/credentials", headers={"X-CSRF-Token": csrf_first}).status_code == 200
        assert first.get("/api/paytm/status").json() == {"configured": False, "mid": None}
    finally:
        app.paytm_client.fetch = original_fetch
        if previous_credential_key is None:
            app.os.environ.pop("PAYTM_CREDENTIALS_KEY", None)
        else:
            app.os.environ["PAYTM_CREDENTIALS_KEY"] = previous_credential_key

print("account auth/data isolation and encrypted per-account Paytm credential/sync tests passed")
