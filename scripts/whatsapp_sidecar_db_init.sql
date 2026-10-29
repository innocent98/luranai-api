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

-- Assert least privilege BEFORE the sidecar starts (the deploy runs this one-shot first and aborts on error). Catches a
-- database whose ACLs are not the PostgreSQL 15 defaults (for example a volume upgraded from <= 14, where PUBLIC could CREATE in
-- schema public) or that carries explicit grants to PUBLIC: the role must hold no CREATE on the database, no CREATE on any
-- schema but its own, and no table or sequence privilege outside its own schema. Reports object NAMES only.
DO $$
DECLARE
  bad text;
BEGIN
  IF has_database_privilege('whatsapp_sidecar', current_database(), 'CREATE') THEN
    RAISE EXCEPTION 'whatsapp_sidecar has CREATE on the database';
  END IF;
  SELECT string_agg(nspname, ', ') INTO bad FROM pg_namespace
   WHERE nspname <> 'whatsapp_sidecar' AND nspname <> 'information_schema' AND nspname !~ '^pg_'
     AND has_schema_privilege('whatsapp_sidecar', oid, 'CREATE');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'whatsapp_sidecar can CREATE in schema(s): %', bad;
  END IF;
  SELECT string_agg(n.nspname || '.' || c.relname, ', ') INTO bad
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname <> 'whatsapp_sidecar' AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
     AND ((c.relkind IN ('r','v','m','p','f') AND has_table_privilege('whatsapp_sidecar', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))
       OR (c.relkind = 'S' AND has_sequence_privilege('whatsapp_sidecar', c.oid, 'USAGE,SELECT,UPDATE')));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'whatsapp_sidecar has privileges on: %', bad;
  END IF;
END
$$;
