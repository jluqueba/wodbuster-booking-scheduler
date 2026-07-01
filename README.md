# WodBuster Booking Worker

Unattended booking worker for the WodBuster platform. Single FastAPI ASGI process running on Azure Container Apps with APScheduler, SQLite on Azure Files, and dual-channel notifications (Telegram + web banner).

See `docs/features/wodbuster-booking-worker/` for the feature spec, plan, and tasks, and `docs/architecture/decisions/` for the architectural decisions (ADRs 0001-0007).

## Local development

Requires Python 3.12 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
.\check.ps1
```

On Linux or macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
make check
```

`check` runs `ruff check`, `mypy src`, and `pytest` (excluding the `live_contract` marker).

## Run the app locally

```powershell
uvicorn wodbuster_worker.app:app --reload --port 8000
```

Then `GET http://localhost:8000/health` returns `200 OK`.

## Container build

```powershell
docker build -t wodbuster-worker:dev .
docker run --rm -p 8000:8000 wodbuster-worker:dev
```
