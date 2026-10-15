-- DAV state tables for the Zotero WebDAV server (v2.03 and later).
--
-- They hold PROPPATCHed dead properties and locks so that both survive a
-- restart.  Both are optional: when the tables are missing the service still
-- starts and keeps that state in memory, logging a warning to the journal.
--
-- Apply once as a DBA:
--
--     sudo mysql webdav < docs/schema_dav_state.sql
--
-- Then let the application account use them (adjust the account and host):
--
--     GRANT SELECT, INSERT, UPDATE, DELETE ON webdav.dav_properties TO 'webdav_app'@'localhost';
--     GRANT SELECT, INSERT, UPDATE, DELETE ON webdav.dav_locks      TO 'webdav_app'@'localhost';
--     FLUSH PRIVILEGES;
--
-- Only DML privileges are needed: the tables are created here, not at runtime.

CREATE TABLE IF NOT EXISTS `dav_properties` (
    `ref_url`    VARCHAR(512) NOT NULL,
    `prop_name`  VARCHAR(255) NOT NULL,
    `prop_value` LONGTEXT     NOT NULL,
    `ref_hash`   BINARY(32)   NOT NULL,
    `name_hash`  BINARY(32)   NOT NULL,
    `updated_at` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`ref_hash`, `name_hash`),
    KEY `idx_dav_properties_url` (`ref_url`(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `dav_locks` (
    `lock_key`   VARCHAR(512) NOT NULL,
    `lock_value` LONGTEXT     NOT NULL,
    `updated_at` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`lock_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
