# Munim

Munim is a payment analytics app for small merchants. It turns payment records into a daily sales overview, a short-term forecast, customer follow-up suggestions, and answers to questions about the business. Merchants can import a CSV report or connect their own Paytm account to sync settlement data.

## Features

- **Merchant accounts:** Email and password sign-up, secure password hashing, expiring sessions, and separate transaction data for each account.
- **Payment dashboard:** Daily collections, payment count, largest payment, recent payments, and comparison with recent daily totals.
- **CSV imports:** Upload a payment report with date and amount columns. Customer name, payment status, time, payment method, and phone are used when present. Invalid dates, non-positive amounts, and unsuccessful statuses are excluded.
- **Paytm settlement sync:** Save a merchant MID and key for the signed-in account, then request up to 60 days of settlement records. Credentials are encrypted before storage and are not returned to the browser.
- **Forecast and unusual-day detection:** A weekday-based forecast uses recent data more heavily and limits the effect of outlier days. Unusual daily totals are highlighted for review.
- **Customer insights:** Identify potentially lapsed customers from their payment history and estimated visit patterns. Draft a WhatsApp message and open it in WhatsApp for the merchant to review and send.
- **Ask Munim:** Ask questions in English or Hindi about the loaded payment data. The assistant uses server-side analytics tools and reports the data sources used in its answer.
- **Demo mode:** New accounts start with clearly labelled synthetic data until they import a report or sync Paytm data.

## Technology

- **Backend:** Python, FastAPI, and Uvicorn
- **Analytics and data handling:** pandas, NumPy, and scikit-learn
- **Storage:** SQLite for accounts, sessions, each account's data, and encrypted Paytm credentials
- **AI question answering:** Anthropic SDK with OpenRouter-compatible API support
- **Paytm integration:** Paytm Settlement API client using `requests` and `paytmchecksum`
- **Frontend:** HTML, CSS, and JavaScript served by FastAPI
- **Tests:** Python scripts using FastAPI's test client and assertions

## Run locally

Requires Python 3.10 or newer.

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app:app --reload --host 127.0.0.1 --port 8012
```

Open <http://127.0.0.1:8012/>. Keep the terminal open while the server is running.

### macOS or Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --reload
```

The app works without AI credentials, but the Ask feature needs an API key. Add the key to `.env`; do not commit that file.

```dotenv
OPENROUTER_API_KEY=your_key
OPENROUTER_MODEL=openrouter/free
```

Alternatively, set `ANTHROPIC_API_KEY` to use Anthropic directly. The application sends AI requests from the server; keys are not included in the frontend.

## Connect Paytm

1. Generate a Fernet encryption key:

   ```powershell
   .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. Set the result as `PAYTM_CREDENTIALS_KEY` in `.env` for local development, or in your hosting provider's secret settings.
3. Sign in to Munim and enter the MID and merchant key for that shop in the Paytm section of the Home screen.
4. Select the lookback period and sync.

Paytm must enable Settlement API access for the merchant. The integration has been tested with a mocked API, not live merchant credentials. Settlement records can arrive after the payment and are not a real-time transaction feed. A sync replaces that account's currently loaded CSV data. Keep the encryption key private, persistent, and backed up: without the original key saved credentials cannot be decrypted.

## Data and analytics notes

- CSV import requires at least a date column, an amount column, and payments on at least two distinct days. Dates can be ISO or day-first formats.
- The forecast requires at least 14 days of data. Backtest comparisons are available with 28 days.
- Lapsed-customer estimates need at least three visits on different days. Results depend on the customer identifiers present in the source report.
- The app shows synthetic sample data until that account loads its own records. Each account's imported or synced data is kept separate.
- Paytm settlement records are mapped to payment entries for the app's analytics. The app does not currently reconcile settlement deductions or explain short payouts.
- WhatsApp messages are drafts opened in WhatsApp; the merchant reviews and sends them.

## Deploy

Deploy as a Python web service with:

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`

Set `COOKIE_SECURE=true` when serving over HTTPS. Configure `PAYTM_CREDENTIALS_KEY` and any AI API key in the host's secret settings. Set `ACCOUNT_DB_PATH` to a persistent disk location and ensure the SQLite database survives restarts. Back up the database and encryption key securely.

The current storage layer is SQLite; run one application instance with persistent storage. Sign-up is open, and email verification and password recovery are not implemented. Review the account and access requirements before making the service public.

## Tests

```powershell
.\.venv\Scripts\python.exe tests\check_auth.py
.\.venv\Scripts\python.exe tests\check_core.py
```

`check_auth.py` covers account isolation, authentication, credential encryption, and a mocked Paytm sync. `check_core.py` exercises the analytics over sample data.

## Sample files

- `demo_payments.csv` contains synthetic payment history for trying the dashboard and customer insights.
- `demo_followup_simulated.csv` contains synthetic follow-up activity for the win-back demonstration.

These files are examples only; do not present them as real merchant records.
