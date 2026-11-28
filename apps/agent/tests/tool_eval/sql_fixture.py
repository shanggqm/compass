"""Synthetic SQL feedback with columns matching SELECT; not PostgreSQL acceptance.

The real PostgreSQL read-only AST validator runs before this in-memory engine.
Unsupported fixture expressions produce errors instead of invented rows.
"""

import re
import sqlite3


def query(sql: str) -> dict:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("ATTACH DATABASE ':memory:' AS sales")
        connection.execute("ATTACH DATABASE ':memory:' AS information_schema")
        connection.execute(
            "CREATE TABLE sales.orders (order_id TEXT, business_date TEXT, "
            "amount_cent INTEGER, status TEXT)"
        )
        connection.executemany(
            "INSERT INTO sales.orders VALUES (?,?,?,?)",
            [
                ("O-100", "2026-10-30", 12000, "completed"),
                ("O-102", "2026-10-30", 8000, "completed"),
                ("O-101", "2026-10-12", -500, "completed"),
                ("O-103", "2026-10-10", 15000, "completed"),
            ],
        )
        connection.execute(
            "CREATE TABLE information_schema.tables (table_catalog TEXT, table_schema TEXT, "
            "table_name TEXT, table_type TEXT)"
        )
        connection.execute(
            "INSERT INTO information_schema.tables "
            "VALUES ('synthetic','sales','orders','BASE TABLE')"
        )
        connection.execute(
            "CREATE TABLE information_schema.columns (table_catalog TEXT, table_schema TEXT, "
            "table_name TEXT, column_name TEXT, data_type TEXT, ordinal_position INTEGER, "
            "is_nullable TEXT)"
        )
        connection.executemany(
            "INSERT INTO information_schema.columns "
            "VALUES ('synthetic','sales','orders',?,?,?,'NO')",
            [
                ("order_id", "text", 1),
                ("business_date", "date", 2),
                ("amount_cent", "bigint", 3),
                ("status", "text", 4),
            ],
        )
        connection.execute("PRAGMA query_only=ON")
        compatible = re.sub(r"\bDATE\s+('(?:[^']|'')*')", r"\1", sql, flags=re.IGNORECASE)
        compatible = re.sub(
            r"::(?:date|text|numeric|decimal|bigint|integer)\b", "", compatible, flags=re.IGNORECASE
        )
        compatible = re.sub(r"\bILIKE\b", "LIKE", compatible, flags=re.IGNORECASE)
        cursor = connection.execute(compatible)
        return {
            "columns": [column[0] for column in cursor.description or []],
            "rows": [list(row) for row in cursor.fetchmany(100)],
            "truncated": False,
        }
