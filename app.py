import hashlib, hmac, json, os, re, secrets, sqlite3, time
from contextlib import contextmanager
from pathlib import Path
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field
import core, paytm_client

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

ACCOUNT_DB = Path(os.getenv("ACCOUNT_DB_PATH", "data/accounts.sqlite3"))
app = FastAPI(title="Munim merchant copilot", docs_url=None, redoc_url=None, openapi_url=None)
SESSION_COOKIE = "munim_session"
SESSION_TTL = 8 * 60 * 60
LOGIN_WINDOW = 15 * 60
LOGIN_LIMIT = 5
LOGIN_ATTEMPTS = {}
SIGNUP_ATTEMPTS = {}


@contextmanager
def _db():
    ACCOUNT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ACCOUNT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            salt BLOB NOT NULL,
            password_hash BLOB NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_data (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            transactions_csv BLOB,
            source TEXT NOT NULL DEFAULT 'synthetic demo data',
            winback_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS paytm_credentials (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            mid_encrypted BLOB NOT NULL,
            merchant_key_encrypted BLOB NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _password_hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _credential_cipher():
    key = os.getenv("PAYTM_CREDENTIALS_KEY", "")
    if not key:
        raise HTTPException(503, "Paytm credential encryption is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise HTTPException(503, "PAYTM_CREDENTIALS_KEY is invalid")


def _get_paytm_credentials(user_id):
    with _db() as db:
        row = db.execute(
            "SELECT mid_encrypted, merchant_key_encrypted FROM paytm_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    cipher = _credential_cipher()
    try:
        return (
            cipher.decrypt(row["mid_encrypted"]).decode("utf-8"),
            cipher.decrypt(row["merchant_key_encrypted"]).decode("utf-8"),
        )
    except (InvalidToken, UnicodeDecodeError):
        raise HTTPException(500, "Stored Paytm credentials cannot be decrypted")


def _session(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _db() as db:
        session = db.execute(
            "SELECT user_id, csrf_token, expires_at FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not session:
            return None
        if session["expires_at"] <= time.time():
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        return {"user_id": session["user_id"], "csrf": session["csrf_token"]}


def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    return response


def _set_auth_cookie(token, csrf, user_id):
    expires = time.time() + SESSION_TTL
    with _db() as db:
        db.execute("INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
                   (hashlib.sha256(token.encode()).hexdigest(), user_id, csrf, expires))
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
    response = JSONResponse({"authenticated": True, "csrf_token": csrf})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True,
        secure=os.getenv("COOKIE_SECURE", "false").lower() not in {"0", "false", "no"},
        samesite="strict", path="/",
    )
    return response


@app.middleware("http")
async def protect_api(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        endpoint = request.url.path in {"/api/auth/status", "/api/auth/login", "/api/auth/signup"}
        if not endpoint:
            session = _session(request)
            if not session:
                return _security_headers(JSONResponse({"detail": "Authentication required"}, status_code=401))
            request.state.user_id = session["user_id"]
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                csrf = request.headers.get("X-CSRF-Token", "")
                if not csrf or not hmac.compare_digest(csrf, session["csrf"]):
                    return _security_headers(JSONResponse({"detail": "Invalid CSRF token"}, status_code=403))
        elif request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return _security_headers(JSONResponse({"detail": "Invalid request origin"}, status_code=403))
    response = await call_next(request)
    return _security_headers(response)


def _current_user(request):
    return request.state.user_id


def _load(user_id):
    with _db() as db:
        row = db.execute(
            "SELECT transactions_csv, source FROM account_data WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row and row["transactions_csv"]:
        return core.load_csv(row["transactions_csv"]), row["source"]
    return core.synthetic(), "synthetic demo data"


def _log(user_id):
    with _db() as db:
        row = db.execute("SELECT winback_json FROM account_data WHERE user_id = ?", (user_id,)).fetchone()
    return json.loads(row["winback_json"]) if row else {}


def _set(user_id, df, source):
    with _db() as db:
        db.execute("""
            INSERT INTO account_data (user_id, transactions_csv, source)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET transactions_csv = excluded.transactions_csv, source = excluded.source
        """, (user_id, core.to_csv_bytes(df), source))
    return core.state(df, source, _log(user_id))


@app.get("/api/state")
def get_state(request: Request):
    user_id = _current_user(request)
    df, source = _load(user_id)
    return core.state(df, source, _log(user_id))


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


@app.get("/api/auth/status")
def auth_status(request: Request):
    session = _session(request)
    if not session:
        return {"authenticated": False, "csrf_token": None}
    with _db() as db:
        row = db.execute("SELECT email FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    return {"authenticated": row is not None, "csrf_token": session["csrf"],
            "email": row["email"] if row else None}


@app.post("/api/auth/login")
def login(body: Credentials, request: Request):
    address = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = LOGIN_ATTEMPTS.get(address, [])
    attempts = [attempt for attempt in attempts if now - attempt < LOGIN_WINDOW]
    if len(attempts) >= LOGIN_LIMIT:
        raise HTTPException(429, "Too many login attempts. Try again later.")
    email = body.email.strip().lower()
    with _db() as db:
        user = db.execute("SELECT id, salt, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if not user or not hmac.compare_digest(_password_hash(body.password, user["salt"]), user["password_hash"]):
        attempts.append(now)
        LOGIN_ATTEMPTS[address] = attempts
        raise HTTPException(401, "Invalid email or password")
    LOGIN_ATTEMPTS.pop(address, None)
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    return _set_auth_cookie(token, csrf, user["id"])


@app.post("/api/auth/signup")
def signup(body: Credentials, request: Request):
    address = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [attempt for attempt in SIGNUP_ATTEMPTS.get(address, []) if now - attempt < LOGIN_WINDOW]
    if len(attempts) >= LOGIN_LIMIT:
        raise HTTPException(429, "Too many account attempts. Try again later.")
    attempts.append(now)
    SIGNUP_ATTEMPTS[address] = attempts
    email = body.email.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Enter a valid email address")
    if len(body.password) < 12:
        raise HTTPException(400, "Password must be at least 12 characters")
    salt = secrets.token_bytes(16)
    user_id = secrets.token_urlsafe(18)
    password_hash = _password_hash(body.password, salt)
    try:
        with _db() as db:
            db.execute(
                "INSERT INTO users (id, email, salt, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email, salt, password_hash, now),
            )
            db.execute("INSERT INTO account_data (user_id) VALUES (?)", (user_id,))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with that email already exists")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    return _set_auth_cookie(token, csrf, user_id)


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with _db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@app.post("/api/import")
async def import_csv(request: Request, file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 5_000_000:
        raise HTTPException(413, "File is larger than 5 MB")
    try:
        return _set(_current_user(request), core.load_csv(raw), file.filename or "uploaded file")
    except ValueError as e:
        raise HTTPException(400, str(e))


class Contact(BaseModel):
    name: str


@app.post("/api/winback")
def winback(body: Contact, request: Request):
    """Record that the merchant contacted a customer. The date is the latest date in the data, so a later
    import that shows the customer paying again counts as a win-back."""
    user_id = _current_user(request)
    df, _ = _load(user_id)
    if body.name not in set(df["name"]):
        raise HTTPException(404, "Unknown customer")
    log = _log(user_id)
    log[body.name] = str(df["date"].max().date())
    with _db() as db:
        db.execute("""
            INSERT INTO account_data (user_id, winback_json) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET winback_json = excluded.winback_json
        """, (user_id, json.dumps(log)))
    return {"ok": True, "contacted": log[body.name]}


class Sync(BaseModel):
    days: int = 60


class PaytmCredentials(BaseModel):
    mid: str = Field(min_length=1, max_length=128)
    merchant_key: str = Field(min_length=1, max_length=512)


@app.get("/api/paytm/status")
def paytm_status(request: Request):
    credentials = _get_paytm_credentials(_current_user(request))
    mid = credentials[0] if credentials else None
    return {
        "configured": credentials is not None,
        "mid": (("*" * (len(mid) - 4) + mid[-4:]) if len(mid) > 4 else "****") if mid else None,
    }


@app.post("/api/paytm/credentials")
def save_paytm_credentials(body: PaytmCredentials, request: Request):
    user_id = _current_user(request)
    mid, merchant_key = body.mid.strip(), body.merchant_key
    if not mid or not merchant_key.strip():
        raise HTTPException(400, "MID and merchant key are required")
    cipher = _credential_cipher()
    with _db() as db:
        db.execute("""
            INSERT INTO paytm_credentials (user_id, mid_encrypted, merchant_key_encrypted, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                mid_encrypted = excluded.mid_encrypted,
                merchant_key_encrypted = excluded.merchant_key_encrypted,
                updated_at = excluded.updated_at
        """, (
            user_id,
            cipher.encrypt(mid.encode("utf-8")),
            cipher.encrypt(merchant_key.encode("utf-8")),
            time.time(),
        ))
    return {"configured": True}


@app.delete("/api/paytm/credentials")
def delete_paytm_credentials(request: Request):
    with _db() as db:
        db.execute("DELETE FROM paytm_credentials WHERE user_id = ?", (_current_user(request),))
    return {"configured": False}


@app.post("/api/paytm/sync")
def sync(body: Sync, request: Request):
    user_id = _current_user(request)
    credentials = _get_paytm_credentials(user_id)
    if not credentials:
        raise HTTPException(409, "Connect your Paytm merchant account before syncing")
    mid, merchant_key = credentials
    try:
        rows = paytm_client.fetch(mid=mid, key=merchant_key, days=max(1, min(body.days, 60)))
        return _set(user_id, paytm_client.to_frame(rows), "Paytm settlement API")
    except Exception as e:
        message = str(e).replace(mid, "[redacted]").replace(merchant_key, "[redacted]")
        raise HTTPException(502, "Paytm settlement sync failed: " + message[:300])


class Ask(BaseModel):
    question: str
    lang: str = "en"


@app.post("/api/ask")
def ask(body: Ask, request: Request):
    df, _ = _load(_current_user(request))
    if not (os.getenv("OPENROUTER_API_KEY") or os.getenv("ANTHROPIC_API_KEY")):
        raise HTTPException(503, "Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY")
    import agent
    try:
        return agent.ask(body.question.strip()[:500], body.lang, df)
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory="static"), name="static")
