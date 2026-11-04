# MediSlot Doctor Dashboard

A Flask 3 application for doctors. The dashboard is a live API client: all application data and authentication are read from or written to the deployed MediSlot API Gateway over HTTPS. It does not connect to MySQL, DynamoDB, or AWS services directly.

## Requirements

- Python 3.12 or newer (Python 3.13 is supported; `tzdata` supplies the IANA timezone database on Windows).
- A deployed MediSlot API Gateway base URL.
- A doctor account registered through the MediSlot authentication API.

## Windows: local setup

Open PowerShell in the repository's `frontend` directory:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation, allow scripts for the current terminal only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

Configure the live API. Replace the example URL with your deployed API Gateway URL; do not include a trailing slash:

```powershell
$env:API_BASE_URL = "https://YOUR_API_ID.execute-api.us-east-1.amazonaws.com/dev"
$env:SECRET_KEY = "replace-with-a-long-random-secret"
$env:APP_ENV = "development"
$env:SESSION_COOKIE_SECURE = "0"
$env:REQUEST_TIMEOUT_SECONDS = "10"
```

Run:

```powershell
flask --app "app:create_app" run --debug
```

Open http://127.0.0.1:5000.

The login page uses your real doctor credentials. The app does not ship with a demo account or a local fake backend.

## Windows timezone verification

```powershell
python -c "from zoneinfo import ZoneInfo; from datetime import datetime; print(datetime.now(ZoneInfo('Asia/Kolkata')))"
```

If the timezone is unavailable, install the dependency:

```powershell
pip install tzdata
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `API_BASE_URL` | Yes | HTTPS API Gateway base URL, without trailing slash |
| `SECRET_KEY` | Yes | Long random Flask session-signing secret |
| `APP_ENV` | No | `development` locally; `production` on EC2 |
| `SESSION_DIR` | No | Server-side filesystem session directory |
| `SESSION_COOKIE_SECURE` | No | Set to `1` when HTTPS is used; local HTTP uses `0` |
| `STATIC_BASE_URL` | No | Static asset prefix; defaults to `/static` |
| `REQUEST_TIMEOUT_SECONDS` | No | API request timeout; defaults to `10` |

No access token, refresh token, password, AWS key, or database credential belongs in source code or browser storage. Access and refresh tokens remain in the server-side Flask session.

## Pages

- `/` landing page
- `/doctor/login` and `/doctor/register`
- `/doctor/dashboard`
- `/doctor/profile`
- `/doctor/consultation-types`
- `/doctor/availability`
- `/doctor/blackouts`
- `/doctor/schedule`
- `/doctor/appointments` (disabled until the corresponding backend API is available)
- `/patient/login` (information page)

## Tests

```powershell
pytest -v
```

Tests intercept outbound HTTP calls and do not contact AWS or your deployed API.

## EC2 deployment

The `deploy/` directory contains the Amazon Linux 2023 bootstrap, Gunicorn systemd unit, and Nginx configuration. The bootstrap expects the uploaded archive to contain `frontend/` at its root and requires `S3_BUCKET`, `S3_KEY`, `API_BASE_URL`, and `SECRET_KEY` to be supplied as user-data parameters or substitutions.

Attach an EC2 instance role with permission to read only the specified deployment object. Do not place AWS credentials in the application. Configure HTTPS termination before setting `SESSION_COOKIE_SECURE=1`; the supplied Nginx configuration proxies HTTP to Gunicorn.

## API limitations

- The API contract does not currently provide appointment listing, slot cancellation, or appointment outcome endpoints. Those controls remain disabled.
- There is no hospital-list endpoint; hospital IDs are entered as numeric values and can be prefilled from existing availability or blackout records.
- There is no profile-photo read URL; the dashboard shows initials and an upload indicator rather than attempting to render a private S3 object key.
- Consultation types are returned as a shared catalog, not a doctor-owned list.
- Schedule has no range endpoint; week view makes seven daily schedule requests concurrently.
- Availability changes appear in generated slots after the next scheduled slot-materializer run.
