-- Least-privilege Postgres role for the WhatsApp sidecar. Run by the `whatsapp-db-init` one-shot service in
-- docker-compose.whatsapp.yml as the stack's Postgres superuser; the role password arrives as psql variable :pw
-- (from WHATSAPP_SIDECAR_DB_PASSWORD). Idempotent: safe on every deploy; re-syncs the password after a rotation.
--
-- What the role can do: connect, create its own schema (the sidecar bootstraps `whatsapp_sidecar` itself at boot), and use
-- that schema. It owns nothing else and has NO grants on the application's tables (PostgreSQL 15: PUBLIC cannot create in
-- schema public and has no table privileges by default), so a compromised sidecar cannot read tenants, messages or users.
--
-- Keep the password out of the server log: a failing CREATE/ALTER would otherwise log the statement text.
SET log_statement = 'none';
SET log_min_error_statement = 'panic';

SELECT format('CREATE ROLE whatsapp_sidecar LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8 PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'whatsapp_sidecar')
\gexec

SELECT format('ALTER ROLE whatsapp_sidecar LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8 PASSWORD %L', :'pw')
\gexec

-- CREATE on the database lets the sidecar's `CREATE SCHEMA IF NOT EXISTS whatsapp_sidecar` pass its privilege check
-- (PostgreSQL checks it before the IF NOT EXISTS test). It allows nothing beyond creating schemas.
SELECT format('GRANT CONNECT, CREATE ON DATABASE %I TO whatsapp_sidecar', current_database())
\gexec

-- Pre-create the schema owned by the role so ownership never depends on boot order.
CREATE SCHEMA IF NOT EXISTS whatsapp_sidecar AUTHORIZATION whatsapp_sidecar;
