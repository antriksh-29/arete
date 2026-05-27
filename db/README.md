# db/

Postgres schema + migrations for the Arete platform.

## Connection

The shared Supabase Postgres URL lives in 1Password as `ARETE_SUPABASE → db_url`. Pick **Session mode** (port `5432`), not Transaction mode (port `6543`) — we use `SET LOCAL` for tenant scoping (Phase E) which only works in session mode.

## Layout

```
db/
├── migrate.sh           # Migration runner — apply pending .sql files
├── migrations/
│   └── 0001_init.sql    # Extensions, tenants table, schema_migrations
└── README.md
```

## Migrating

From your laptop (or `arete-1`):

```bash
export DATABASE_URL="postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres"

# What's pending?
bash db/migrate.sh --status

# Dry run
bash db/migrate.sh --dry-run

# Apply pending
bash db/migrate.sh
```

The runner uses `schema_migrations` to track what's applied. Idempotent — running twice is safe.

## Migration conventions

- Filename: `NNNN_short_description.sql` (zero-padded 4 digits)
- Always `on conflict do nothing` for the final `insert into schema_migrations`
- Use `create … if not exists` / `drop … if exists` for top-level objects so re-runs don't error
- Wrap multi-statement files in a transaction (we pass `-1` to psql which does this)
- Don't modify a migration after it's been applied to prod — add a new one
