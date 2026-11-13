-- Appended to seed.sql for the access-controlled server of the end-to-end check (started with
-- --rbac-store-uri). Two users, whose passwords e2e.py fills in:
--   viewer    may read shop and uploads, and write nowhere;
--   uploader  may read shop and do anything in the uploads schema: what an upload or a transform into
--             that schema needs, and nothing more.
CREATE ROLE reader;
GRANT SELECT ON SCHEMA shop TO reader;
GRANT SELECT ON SCHEMA uploads TO reader;
CREATE USER viewer WITH PASSWORD '{viewer_password}';
GRANT reader TO viewer;

CREATE ROLE loader;
GRANT SELECT ON SCHEMA shop TO loader;
GRANT ALL PRIVILEGES ON SCHEMA uploads TO loader;
CREATE USER uploader WITH PASSWORD '{uploader_password}';
GRANT loader TO uploader;
