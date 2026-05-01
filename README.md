# ERP → E-shop Synchronization Bridge

A robust synchronization bridge between an ERP system and a fictional e-shop API, built with Django, Celery, Redis, and PostgreSQL.

## Architecture

```
┌──────────────┐      ┌──────────────────┐      ┌───────────────────┐
│ erp_data.json│─────▶│  Celery Worker    │─────▶│  E-shop API       │
│ (ERP source) │      │  (sync_products)  │      │  (fake-eshop.cz)  │
└──────────────┘      └────────┬─────────┘      └───────────────────┘
                               │
                      ┌────────▼─────────┐
                      │   PostgreSQL DB   │
                      │  (SyncedProduct)  │
                      └──────────────────┘
```

### Key Components

| Component | Purpose |
|---|---|
| **`integrator/schemas.py`** | Pydantic models for ERP data validation (`ERPProduct`) and e-shop payload construction (`EshopProduct`). Handles VAT calculation, stock summation, and default color. |
| **`integrator/tasks.py`** | Celery task `sync_products` — the core sync logic: load → validate → transform → delta-sync → send to API. Includes rate limiter and 429 retry handling. |
| **`integrator/models.py`** | Django `SyncedProduct` model tracking sync state per SKU: hash for delta detection, status (`PENDING`/`SUCCESS`/`FAILED`), and `active` flag for soft-delete. |

### Sync Flow

1. **Load** — Read `erp_data.json` from disk (simulates ERP endpoint).
2. **Validate** — Parse with Pydantic; skip invalid items (negative price, null price, duplicates).
3. **Transform** — Add 21% VAT, sum stock quantities across warehouses, default missing color to `"N/A"`.
4. **Deactivate** — Products no longer in ERP get `active=False` sent via `PATCH` (soft-delete, no `DELETE` endpoint).
5. **Delta Sync** — Compute SHA-256 hash of each product payload; only send products whose hash differs from the last successful sync. Previously `FAILED` products are always retried.
6. **Rate Limit** — Token-bucket limiter caps outgoing requests to 5/sec. On HTTP 429, exponential back-off with up to 5 retries.

### Status Lifecycle

```
 Load from ERP ──▶ PENDING ──▶ Send to API ──▶ SUCCESS
                                     │
                                     └──▶ FAILED (retry on next sync)
```

## Setup & Running

### Prerequisites

- Docker & Docker Compose

### Quick Start

```bash
# Build and start all services
docker-compose up --build -d

# Run migrations
docker-compose exec web python manage.py migrate

# Trigger a sync manually (from Django shell)
docker-compose exec web python manage.py shell -c "
from integrator.tasks import sync_products
result = sync_products.delay()
print(result.get(timeout=30))
"
```

### Running Tests

Tests use an in-memory SQLite database and mocked HTTP responses — no external services needed.

```bash
# Inside Docker
docker-compose exec web pytest -v

# Or locally with a virtualenv
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -v
```

### Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | Redis URL for Celery broker |
| `DATABASE_URL` | (see `settings.py`) | PostgreSQL connection |

Settings in `core/settings.py`:

| Setting | Value | Description |
|---|---|---|
| `ESHOP_API_BASE_URL` | `https://api.fake-eshop.cz/v1` | Target e-shop API base URL |
| `ESHOP_API_KEY` | `symma-secret-token` | API authentication key |
| `ESHOP_API_RATE_LIMIT` | `5` | Max requests per second |

## Test Coverage

- **Transformation logic**: VAT calculation, stock summation (including non-numeric values), color defaults, active flag, hash determinism.
- **Validation**: Negative price rejection, null price skipping, duplicate deduplication.
- **API mocking**: Full sync cycle (POST for new, PATCH for updates), delta sync detection, deactivation on product removal, failure status tracking, retry of failed products.
- **Rate limiting**: 429 retry with back-off, retry exhaustion, correct URL routing (POST vs PATCH).
