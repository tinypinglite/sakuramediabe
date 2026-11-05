"""Add protocol-neutral download source identity to submission records.

New submissions keep BT hash in info_hash only for BT sources. source_kind and
resource_key identify magnet/torrent/eD2k/HTTP sources consistently.
New databases receive these columns directly from create_tables.
"""

name = "20261103_01_add_download_source_identity"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE download_submission_record "
        "ADD COLUMN IF NOT EXISTS source_kind VARCHAR(16) NOT NULL DEFAULT 'magnet'"
    )
    database.execute_sql(
        "ALTER TABLE download_submission_record "
        "ADD COLUMN IF NOT EXISTS resource_key VARCHAR(255) NULL"
    )
    database.execute_sql(
        "UPDATE download_submission_record "
        "SET resource_key = 'bt:' || lower(info_hash) "
        "WHERE resource_key IS NULL AND info_hash IS NOT NULL AND info_hash <> ''"
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS downloadsubmissionrecord_client_id_resource_key "
        "ON download_submission_record (client_id, resource_key)"
    )
