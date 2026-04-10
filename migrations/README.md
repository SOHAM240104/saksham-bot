# SQL migrations (manual)

Run these in order against the same Postgres database as `DATABASE_URL` when you are not relying solely on SQLAlchemy `create_all` for schema setup.

**Alembic** is intentionally out of scope for this project phase; use these versioned SQL files plus `Base.metadata.create_all` for local bootstrap.

| Order | File | Purpose |
| --- | --- | --- |
| 1 | [001_scam_ingestions.sql](001_scam_ingestions.sql) | Create `scam_ingestions` table |
| 2 | [002_drop_legacy_scam_collection.sql](002_drop_legacy_scam_collection.sql) | Drop legacy LangChain collection `scam` from `langchain_pg_*` tables after cutover to `scam_kb` |

Example:

```bash
psql "$DATABASE_URL" -f migrations/001_scam_ingestions.sql
psql "$DATABASE_URL" -f migrations/002_drop_legacy_scam_collection.sql
```

Note: `DATABASE_URL` may use the `postgresql+psycopg://` form in app config; `psql` expects a standard `postgresql://` URL.
