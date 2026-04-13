# Saksham MVP — Ingestion API

FastAPI service that ingests documents and URLs into PostgreSQL with **pgvector** (LangChain PGVector), using **OpenAI** embeddings (`text-embedding-3-large`, 1024 dimensions). It exposes admin endpoints to manage platforms / OS / versions, run tech training (contextual RAG), and scam knowledge-base ingestion.

---

## What you need installed

| Requirement | Notes |
|-------------|--------|
| **Python** | 3.10 or newer (3.12+ recommended). |
| **PostgreSQL** | With the **[pgvector](https://github.com/pgvector/pgvector)** extension available (the app enables it on startup). |
| **OpenAI API key** | Used for embeddings (and any models LangChain resolves via env). |

Optional but recommended for **URL crawling** (`/train/url`, bulk URLs, etc.): **Playwright** browser binaries (see step 5).

---

## Step-by-step: run locally

### 1. Install PostgreSQL + pgvector

- Install PostgreSQL and install the `pgvector` extension in your database (exact steps depend on your OS; many use `CREATE EXTENSION vector;` after the extension package is installed on the server).
- Create an empty database and a user with access to it.

### 2. Go to the project folder

```bash
cd "/path/to/saksham-mvp"
```

All commands below assume this directory is your current working directory.

### 3. Create a virtual environment and install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root (same folder as `settings.py`). The app loads it automatically via `python-dotenv`.

**Required**

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | SQLAlchemy URL to your Postgres database. You may use `postgresql://user:pass@host:port/dbname` — the app rewrites it to `postgresql+psycopg://` for the driver. |
| `ADMIN_TOKEN` | Secret string. Every admin route requires `Authorization: Bearer <ADMIN_TOKEN>`. |
| `OPENAI_API_KEY` | Your OpenAI API key (used by LangChain `OpenAIEmbeddings`; not listed in `settings.py` but required at runtime). |

**Optional**

| Variable | Default | Purpose |
|----------|---------|---------|
| `SCAM_VECTOR_COLLECTION` | `scam_kb` | Name of the PGVector collection for scam KB (must stay consistent with your DB; see `settings.py`). |

Example (replace with your real values):

```bash
DATABASE_URL=postgresql://myuser:mypass@localhost:5432/saksham
ADMIN_TOKEN=change-me-to-a-long-random-secret
OPENAI_API_KEY=sk-...
# SCAM_VECTOR_COLLECTION=scam_kb
```

### 5. Install Playwright browsers (for URL ingestion)

Crawl4AI depends on Playwright. After `pip install`, install browser binaries **inside the same venv**:

```bash
playwright install
```

If `playwright` is not on your PATH, use:

```bash
python -m playwright install
```

Skip this only if you will not use URL-based training endpoints.

### 6. Start the API

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

On startup the app:

- Validates embedding settings (1024-dim `text-embedding-3-large` only).
- Creates SQLAlchemy tables from models.
- Ensures PGVector tables/collections for the **tech** and **scam** collections.

### 7. Verify it is running

- **Health (no auth):** [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) — expect `{"status":"ok"}`.
- **Interactive docs:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) (Swagger UI).

---

## Calling admin endpoints

All routes under the **admin** router require:

```http
Authorization: Bearer <ADMIN_TOKEN>
```

Example with `curl`:

```bash
curl -s http://127.0.0.1:8000/platforms \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

Typical flow for **tech** training:

1. `POST /platforms` — create a platform.
2. `POST /platforms/{platform_uuid}/operating-systems` — add an OS.
3. `POST /platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions` — add a version.
4. Use training endpoints such as `POST .../train/url`, `.../train/urls`, `.../train/excel`, `.../train/pdf` with the corresponding UUIDs.

**Scam** training uses routes like `POST /scam/train/url` (no platform/OS/version path).

---

## Troubleshooting

| Issue | What to check |
|-------|----------------|
| `Missing required environment variable` | `.env` path, variable names, no typos; restart the server after edits. |
| Database connection errors | `DATABASE_URL`, Postgres running, firewall, user permissions. |
| `pgvector` / extension errors | Extension installed on the server; database user can create extensions if needed. |
| Embedding / OpenAI errors | `OPENAI_API_KEY`, billing, and model access for `text-embedding-3-large`. |
| URL crawl failures | Playwright browsers installed (step 5); network access to target URLs. |

---

## Project layout (short)

- `app/main.py` — FastAPI app entrypoint.
- `settings.py` — Environment and embedding/vector collection configuration.
- `config/database.py` — SQLAlchemy engine.
- `routers/admin.py` — Authenticated admin + ingestion routes.
- `routers/health.py` — Public health check.
- `ingestion/` — Crawling, chunking, and ingest pipelines.

---

## Production notes

- Run behind a reverse proxy with TLS; do not expose `ADMIN_TOKEN` or `.env`.
- Use a strong `ADMIN_TOKEN` and restrict who can reach admin routes.
- Tune Postgres, connection pooling, and resource limits for your load; this README targets local development.


Public
GET /health
Checks if API is alive. Returns something like { "status": "ok" }.
Platform Management
POST /platforms
Creates a new platform (example: android, ios, web).

GET /platforms
Lists all platforms (optionally includes deleted ones if query supports it).

GET /platforms/{platform_uuid}
Gets one specific platform by UUID.

DELETE /platforms/{platform_uuid}
Soft-deletes a platform (is_deleted=true), not hard DB removal.

Operating System Management (under a platform)
POST /platforms/{platform_uuid}/operating-systems
Creates an OS under that platform (example: windows, ubuntu, android).

GET /platforms/{platform_uuid}/operating-systems
Lists OS entries for that platform.

GET /platforms/{platform_uuid}/operating-systems/{operating_system_uuid}
Gets one OS record.

DELETE /platforms/{platform_uuid}/operating-systems/{operating_system_uuid}
Soft-deletes that OS record.

Version Management (under platform + OS)
POST /platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions
Creates a version entry (example: v1.0, android 14, 22H2).

GET /platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions
Lists versions under that OS.

GET /platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}
Gets one version record.

DELETE /platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}
Soft-deletes that version.

Tech Training / Ingestion (contextual knowledge)
These ingest content tied to a specific platform + OS + version:

POST /platforms/{...}/versions/{version_uuid}/train/url
Ingests one URL.

POST /platforms/{...}/versions/{version_uuid}/train/urls
Ingests multiple URLs in one request.

POST /platforms/{...}/versions/{version_uuid}/train/excel
Uploads an Excel file and ingests URLs/content from it.

POST /platforms/{...}/versions/{version_uuid}/train/pdf
Uploads a PDF and ingests extracted content.

Scam KB Training (separate collection)
These go into scam knowledge base (not tied to platform/OS/version):

GET /scam/ingestions
Lists scam ingestion records/history.

POST /scam/train/url
Ingests one URL into scam KB.

POST /scam/train/urls
Ingests multiple URLs into scam KB.

POST /scam/train/excel
Ingests scam data from uploaded Excel.

POST /scam/train/pdf
Ingests scam data from uploaded PDF.

Ingestion Usage Tracking
GET /ingestion-usage
Lists ingestion usage records (processed/skipped/failed/chunks/tokens/cost).

GET /ingestion-usage/{usage_uuid}
Gets one usage record.

PATCH /ingestion-usage/{usage_uuid}
Updates usage status fields (for example marking status transitions).

DELETE /ingestion-usage/{usage_uuid}
Soft-deletes a usage record.