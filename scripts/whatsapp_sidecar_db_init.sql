-- Least-privilege Postgres role for the WhatsApp sidecar. Run by the `whatsapp-db-init` one-shot service in
-- docker-compose.whatsapp.yml as the stack's Postgres superuser; the role password arrives as psql variable :pw
-- (from WHATSAPP_SIDECAR_DB_PASSWORD). Idempotent: safe on every deploy; re-syncs the password after a rotation.
--
-- What the role can do: connect, and create/use objects inside the one schema it owns (`whatsapp_sidecar`, pre-created below).
-- It has NO CREATE on the database (it could otherwise create a schema named after the superuser, which the app's default
-- search_path `"$user", public` would resolve first), owns nothing else, and has NO grants on the application's tables
-- (PostgreSQL 15: PUBLIC cannot create in schema public and has no table privileges by default).
--
-- The password is read from the container's environment by psql itself (never on a command line, so not in `ps`).
\getenv pw WHATSAPP_SIDECAR_DB_PASSWORD

-- Keep the password out of the server log: a failing CREATE/ALTER would otherwise log the statement text.
SET log_statement = 'none';
SET log_min_error_statement = 'panic';

SELECT format('CREATE ROLE whatsapp_sidecar LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8 PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'whatsapp_sidecar')
\gexec

SELECT format('ALTER ROLE whatsapp_sidecar LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8 PASSWORD %L', :'pw')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO whatsapp_sidecar', current_database())
\gexec
SELECT format('REVOKE CREATE ON DATABASE %I FROM whatsapp_sidecar', current_database())
\gexec

-- Pre-create the schema owned by the role so ownership never depends on boot order.
CREATE SCHEMA IF NOT EXISTS whatsapp_sidecar AUTHORIZATION whatsapp_sidecar;
