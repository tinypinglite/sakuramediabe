"""PostgreSQL 通用表达式。"""

from peewee import fn


def year_expression(date_field):
    """返回 PostgreSQL 的年份提取表达式。"""
    return fn.DATE_PART("year", date_field)
