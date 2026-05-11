-- Idempotent SELECT-only grants for mega_ro.
-- Role create/alter is handled in bootstrap.py (Python control flow) so the
-- password literal is escaped without relying on PL/pgSQL dollar quoting,
-- which is breakable when the password itself contains "$$".

GRANT CONNECT ON DATABASE mega TO mega_ro;
GRANT USAGE ON SCHEMA public TO mega_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mega_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mega_ro;
