from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.model import ImportJob
from src.start.migrations import SkipMigration

name = "20260714_02_add_import_job_source_cid"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    # import_job 表加 source_cid（cloud115 导入的源目录 cid，重导据此重新枚举源）。
    # 本地导入行保持 NULL；新装用户走 initdb 直接建出终态 schema，本迁移在其之后
    # 二次执行时会看到列已存在——此时正常 return（并写入 SchemaMigration）而不是
    # 抛 SkipMigration，避免全新环境永远缺少这条审计记录、且每次 migrate 都白跑。
    if not database.table_exists("import_job"):
        raise SkipMigration("import_job table does not exist")
    if _column_exists(database, table_name="import_job", column_name="source_cid"):
        return
    run_migration(
        migrator.add_column(
            "import_job",
            "source_cid",
            ImportJob._meta.fields["source_cid"],
        )
    )
