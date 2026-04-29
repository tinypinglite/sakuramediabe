"""数据库方言感知的通用表达式。"""

from peewee import MySQLDatabase, PostgresqlDatabase, SqliteDatabase, fn

from src.model.base import get_database


def year_expression(date_field):
    """根据当前数据库方言返回提取年份的表达式。"""
    database = get_database()
    if isinstance(database, SqliteDatabase):
        return fn.strftime("%Y", date_field)
    if isinstance(database, MySQLDatabase):
        return fn.YEAR(date_field)
    if isinstance(database, PostgresqlDatabase):
        return fn.DATE_PART("year", date_field)
    raise RuntimeError(f"Unsupported database type: {type(database)!r}")
