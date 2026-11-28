"""Route SQL only among current authorized connections; never guess an ambiguous owner."""

import asyncio

from compass_agent.data_connections.transport import read_database
from compass_agent.tools.errors import ActionableToolError, ActionableToolRejected

METADATA_RELATIONS = frozenset(
    {"information_schema.tables", "information_schema.columns", "information_schema.schemata"}
)


def is_metadata(relations):
    return bool(relations) and set(relations) <= METADATA_RELATIONS


def normalize_input(raw, _context):
    return raw if "op" in raw else {"op": "query", **raw}


def query_relations(sql):
    from compass_agent.tools.builtin.database_query import SqlRejected, parse_read_sql

    try:
        return parse_read_sql(sql)[1]
    except SqlRejected:
        # Validation has not opened a database execution resource.
        raise ActionableToolRejected(
            ActionableToolError(
                "invalid_parameters",
                "fix_arguments",
                "SQL 不符合只读查询约束，尚未执行。",
                field="sql",
                next_action="使用单条 SELECT 和支持的函数；可直接读取原始列后再计算。",
            )
        ) from None


async def route_binding(bindings, sql, control):
    from compass_agent.tools.builtin.database_query import _ALLOWED_RELATIONS

    relations = query_relations(sql)
    if len(bindings) == 1:
        return bindings[0]

    async def owns(binding):
        async def check(cursor):
            await cursor.execute(_ALLOWED_RELATIONS, (relations,))
            return bool(relations) and set(relations) <= {row[0] for row in await cursor.fetchall()}

        return binding if await read_database(binding, control, check) else None

    candidates = [b for b in await asyncio.gather(*(owns(b) for b in bindings)) if b is not None]
    if len(candidates) != 1:
        raise ActionableToolRejected(
            ActionableToolError(
                "sql_connection_ambiguous" if candidates else "sql_relation_unavailable",
                "clarify_with_user" if candidates else "fix_arguments",
                "请确认使用的数据源和完整 schema.table 名称。",
                field="sql",
                next_action="先查询 information_schema 确认可用表；同名表需用户明确数据源。",
            )
        )
    return candidates[0]


async def column_error(cursor, relations):
    candidates = []
    for relation in relations:
        await cursor.execute(
            "SELECT attname FROM pg_catalog.pg_attribute "
            "WHERE attrelid = pg_catalog.to_regclass(%s) AND attnum > 0 AND NOT attisdropped "
            "AND pg_catalog.has_column_privilege(attrelid, attname, 'SELECT') "
            "ORDER BY attnum LIMIT 8",
            (relation,),
        )
        candidates.extend(row[0] for row in await cursor.fetchall())
    return ActionableToolError(
        "sql_undefined_column",
        "fix_arguments",
        "查询列名不可用，请核对当前可见字段。",
        field="sql",
        candidates=tuple(dict.fromkeys(candidates))[:8],
        next_action="使用候选列名修正 SQL 后重试。",
    )
