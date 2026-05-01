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
docker-compose up --build

# Run migrations
docker-compose exec web python manage.py migrate

# Trigger a sync manually (from Django shell)
docker-compose exec web python manage.py shell -c \
"from integrator.tasks import sync_products; sync_products.delay()"
```

### Running Tests

Tests use an in-memory SQLite database and mocked HTTP responses — no external services needed.

```bash
# Inside Docker
docker-compose exec web pytest -v
```


## Test Coverage

- **Transformation logic**: VAT calculation, stock summation (including non-numeric values), color defaults, active flag, hash determinism.
- **Validation**: Negative price rejection, null price skipping, duplicate deduplication for the same SKU (last valid product wins).
- **API mocking**: Full sync cycle (POST for new, PATCH for updates), delta sync detection, deactivation on product removal, failure status tracking, retry of failed products.
- **Rate limiting**: 429 retry with back-off, retry exhaustion, correct URL routing (POST vs PATCH).


## Additional Configuration/settings (core/settings.py)
- Variables for communicating with the e-shop and ERP (base url, api key, rate limit, ERP input file)
- MOCK_ESHOP (bool) - Setting this to True will replace e-shop communication with hardcoded HTTP 200/201 responses and log the payload - good for manual testing with real database.
- VAT PERCENT (float) - VAT percentage to be added to the price (configurable since it can depend on country).


## Important notes and decisions made

### Delta sync logic and edge cases
- Since no DELETE method was specified, products no longer present in ERP are marked as inactive in both the database and the payload sent to e-shop (via PATCH or even CREATE, see `active` field in `EshopProduct`)
- Since e-shop could be unavailable, all data is always stored in the database (see `payload` field in `SyncedProduct`), unsuccessful syncs are marked as `FAILED` and processed on the next sync
- Edge case: If a product is removed from ERP without being previously sent to e-shop in previous syncs, it still gets sent to e-shop as an archived product (`active=False` in the payload)
- The logged stats (created, updated, unchanged, deactivated, failed) always represent the DB -> e-shop synchronization status


### Database model (`models/SyncedProduct`)
- `status` is either `PENDING` (loaded from ERP to DB, not yet sent to e-shop), `SUCCESS` (sent to e-shop), `FAILED` (cannot contact e-shop, will be retried on next sync)
- `synced_at` is timestamp of the last successful sync (successfully contacted e-shop)
- `payload` field ALWAYS reflects the latest state of the product in the ERP system
- `last_hash` is the SHA-256 hash of the last payload that was successfully sent to e-shop (therefore can differ from hash of the current payload)
- `active` flag is `True`, until the product is marked as inactive in the ERP system AND the e-shop is successfully contacted about this event (as opposed to `active` flag inside the payload, which always reflects ERP).


### Other notes
- `ESHOP_API_KEY` should not be hardcoded anywhere in code, but rather read from some secret management system (keyvault)
- The task can currently only be triggered manually, usually Celery Beat would be used to run it periodically