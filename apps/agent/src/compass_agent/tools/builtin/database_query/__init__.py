"""Read-only PostgreSQL discovery and queries on authorized workspace connections."""

import asyncio
import json
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal

import psycopg
from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.stream import RawStream
from pglast.visitors import Visitor, referenced_relations
from pydantic import Field, TypeAdapter

from compass_agent.data_connections.transport import (
    AuthorizedDatabaseBinding,
    DatabaseIdentityMismatch,
    read_database,
)
from compass_agent.execution_context import uuid4
from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import (
    RUN_STREAM_LIMITS,
    RunContractModel,
    RunToolError,
    ToolCall,
    ToolDefinition,
    ToolFunction,
)
from compass_agent.tools.builtin.database_query.model_schema import SCHEMA
from compass_agent.tools.builtin.database_query.slim import (
    column_error,
    is_metadata,
    normalize_input,
)
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import ToolControl, ToolGovernance
from compass_agent.tools.errors import ActionableToolError
from compass_agent.tools.registry import parse_arguments as parse_tool_arguments

DATABASE_QUERY = "database_query"
MAX_ROWS = 50
MAX_RESULT_BYTES = 4096

_FUNCTIONS = frozenset(
    """
    abs avg array_agg array_length array_to_string btrim ceil ceiling char_length concat
    concat_ws corr count cume_dist date_part date_trunc dense_rank extract first_value floor
    greatest json_agg json_build_object jsonb_agg jsonb_build_object lag last_value lead least
    left length lower lpad ltrim max min mod now nth_value ntile nullif percentile_cont
    percentile_disc
    position rank replace right round row_number rpad rtrim split_part stddev stddev_pop stddev_samp
    string_agg strpos substr substring sum timezone to_char trim trunc upper variance
    var_pop var_samp
""".split()
)
_TYPES = frozenset(
    """
    bool bpchar date float4 float8 int2 int4 int8 interval json jsonb numeric text time timestamp
    timestamptz timetz uuid varchar
""".split()
)

_CATALOG = r"""
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname !~ '^pg_'
      AND n.nspname <> 'information_schema' AND c.relkind IN ('r', 'p')
      AND pg_catalog.has_schema_privilege(n.oid, 'USAGE')
      AND pg_catalog.has_any_column_privilege(c.oid, 'SELECT')
"""
_TABLES = (
    "SELECT n.nspname AS schema, c.relname AS table, "
    "pg_catalog.obj_description(c.oid) AS description " + _CATALOG + " ORDER BY 1, 2 OFFSET %s"
)
_COLUMNS = """
    SELECT column_name, data_type, is_nullable,
        pg_catalog.col_description(pg_catalog.to_regclass(pg_catalog.format(
            '%%I.%%I', table_schema, table_name)), ordinal_position) AS description
    FROM information_schema.columns
    WHERE table_schema = %s AND table_name = %s
      AND pg_catalog.has_column_privilege(
        pg_catalog.format('%%I.%%I', table_schema, table_name), column_name, 'SELECT'
      )
    ORDER BY ordinal_position OFFSET %s
"""
_ALLOWED_RELATIONS = (
    "SELECT pg_catalog.quote_ident(n.nspname) || '.' || pg_catalog.quote_ident(c.relname) "
    + _CATALOG
    + " AND pg_catalog.quote_ident(n.nspname) || '.' || pg_catalog.quote_ident(c.relname) = ANY(%s)"
)


class ConnectionAction(RunContractModel):
    connection_id: str | None = Field(default=None, alias="connectionId", max_length=36)


class ListConnections(RunContractModel):
    op: Literal["list_connections"]


class ListTables(ConnectionAction):
    op: Literal["list_tables"]
    offset: int = Field(default=0, ge=0, le=10_000)


class DescribeTable(ConnectionAction):
    op: Literal["describe_table"]
    schema_name: str = Field(alias="schema", min_length=1, max_length=63)
    table: str = Field(min_length=1, max_length=63)
    offset: int = Field(default=0, ge=0, le=10_000)


class ReadSql(ConnectionAction):
    op: Literal["query"]
    sql: str = Field(min_length=1, max_length=8192)


DATABASE_ACTION = TypeAdapter(
    Annotated[ListConnections | ListTables | DescribeTable | ReadSql, Field(discriminator="op")]
)


def database_input_preview(call: ToolCall) -> str | None:
    try:
        action = DATABASE_ACTION.validate_python(parse_tool_arguments(call.function.arguments))
    except (ValueError, RecursionError):
        return None
    preview = action.model_dump_json(by_alias=True, exclude_unset=True)
    return preview if len(preview.encode()) <= RUN_STREAM_LIMITS["toolInputBytes"] else None


def definition(_context=None) -> ToolDefinition:
    return ToolDefinition(
        function=ToolFunction(
            name=DATABASE_QUERY,
            description=(tool_text("database_query")),
            parameters={"type": "object", **DATABASE_ACTION.json_schema(by_alias=True)},
        )
    )


class SqlRejected(ValueError):
    pass


class ReadOnlySql(Visitor):
    def visit_SelectStmt(self, _ancestors, node):
        if node.intoClause or node.lockingClause:
            raise SqlRejected()

    def visit_CommonTableExpr(self, _ancestors, node):
        if not isinstance(node.ctequery, ast.SelectStmt):
            raise SqlRejected()

    def visit_FuncCall(self, _ancestors, node):
        path = tuple(name.sval for name in node.funcname)
        if path[:-1] not in ((), ("pg_catalog",)) or path[-1] not in _FUNCTIONS:
            raise SqlRejected()
        node.funcname = (ast.String(sval="pg_catalog"), node.funcname[-1])

    def visit_TypeName(self, _ancestors, node):
        path = tuple(name.sval for name in node.names)
        if path[:-1] not in ((), ("pg_catalog",)) or path[-1] not in _TYPES:
            raise SqlRejected()
        node.names = (ast.String(sval="pg_catalog"), node.names[-1])

    def visit_A_Expr(self, _ancestors, node):
        self._check_operator(node.name)

    def visit_SortBy(self, _ancestors, node):
        self._check_operator(node.useOp)

    def visit_RangeTableSample(self, _ancestors, _node):
        raise SqlRejected()

    def visit_CollateClause(self, _ancestors, node):
        self._check_operator(node.collname)

    @staticmethod
    def _check_operator(names):
        if names and tuple(name.sval for name in names[:-1]) not in ((), ("pg_catalog",)):
            raise SqlRejected()


def parse_read_sql(text: str) -> tuple[str, list[str]]:
    try:
        statements = parse_sql(text)
        if len(statements) != 1 or not isinstance(statements[0].stmt, ast.SelectStmt):
            raise SqlRejected()
        statement = statements[0].stmt
        ReadOnlySql()(statement)
        return RawStream()(statement), sorted(referenced_relations(statement))
    except (ParseError, RecursionError, ValueError) as error:
        raise SqlRejected() from error


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class DatabaseResult:
    id: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool
    next_offset: int | None = None
    source_ref: dict | None = None
    context_result: dict | None = None

    def observation(self, *, include_result_id: bool = True) -> str:
        payload = {
            "status": "ok",
            "columns": self.columns,
            "rows": self.rows,
            "truncated": self.truncated,
        }
        if include_result_id:
            payload["resultId"] = self.id
            payload["sourceQueryId"] = self.id
        if self.next_offset is not None:
            payload["nextOffset"] = self.next_offset
        return _json(payload)


class JsonPairs(list[tuple[str, Any]]):
    """Keep duplicate column labels and distinguish JSON objects from arrays."""


def _json_value(value: Any) -> Any:
    if isinstance(value, JsonPairs):
        return {key: _json_value(item) for key, item in value}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _bounded_result(raw_rows: list[tuple[str]], offset: int | None) -> DatabaseResult:
    result_id = str(uuid4())
    columns: tuple[str, ...] = ()
    rows: list[tuple[Any, ...]] = []
    for (raw,) in raw_rows[:MAX_ROWS]:
        if len(raw.encode()) > MAX_RESULT_BYTES:
            break
        decoded = json.loads(raw, object_pairs_hook=JsonPairs, parse_float=str)
        if not rows:
            columns = tuple(name for name, _ in decoded)
        row = tuple(_json_value(value) for _, value in decoded)
        next_offset = offset + len(rows) + 1 if offset is not None else None
        candidate = DatabaseResult(result_id, columns, (*rows, row), False, next_offset)
        if len(candidate.observation().encode()) > MAX_RESULT_BYTES:
            if not rows:
                columns = ()
            break
        rows.append(row)
    truncated = len(raw_rows) > len(rows)
    next_offset = offset + len(rows) if offset is not None and truncated and rows else None
    return DatabaseResult(result_id, columns, tuple(rows), truncated, next_offset)


async def query_database(
    call: ToolCall, binding: AuthorizedDatabaseBinding, control: ToolControl
) -> DatabaseResult | RunToolError | ActionableToolError:
    from compass_agent.execution_context import recorded

    return await recorded(
        "tool",
        f"database:{call.id}",
        {
            "call": call.model_dump(mode="json"),
            "scope": [str(binding.tenant_id), str(binding.workspace_id), binding.role],
            "connectionId": str(binding.connection_id) if binding.connection_id else None,
            "revision": binding.revision,
        },
        lambda: query_database_live(call, binding, control),
        DatabaseResult | RunToolError | ActionableToolError,
        # read_database drains and closes its connection before returning a value.
        settled_on_return=True,
    )


async def query_database_live(
    call: ToolCall,
    binding: AuthorizedDatabaseBinding,
    control: ToolControl,
    *,
    fingerprint_only: bool = False,
) -> DatabaseResult | RunToolError | ActionableToolError:
    try:
        action = DATABASE_ACTION.validate_python(parse_tool_arguments(call.function.arguments))
        values = None
        relations = []
        offset = None
        if isinstance(action, ListConnections):
            return "invalid_parameters"
        if isinstance(action, ListTables):
            query = _TABLES
            offset = action.offset
            values = (offset,)
        elif isinstance(action, DescribeTable):
            query = _COLUMNS
            offset = action.offset
            values = (action.schema_name, action.table, offset)
        else:
            query, relations = parse_read_sql(action.sql)

        async def read(cursor):
            if isinstance(action, DescribeTable):
                await cursor.execute(
                    "SELECT pg_catalog.has_schema_privilege(%s, 'USAGE') AND %s NOT LIKE 'pg_%%' "
                    "AND %s <> 'information_schema'",
                    (action.schema_name, action.schema_name, action.schema_name),
                )
                if await cursor.fetchone() != (True,):
                    raise SqlRejected()

            await cursor.execute("SET LOCAL search_path = pg_catalog")
            if relations and not is_metadata(relations):
                await cursor.execute(_ALLOWED_RELATIONS, (relations,))
                allowed = {row[0] for row in await cursor.fetchall()}
                if not set(relations) <= allowed:
                    raise SqlRejected()
            control.require_running()
            if (
                control.phase_deadline is not None
                and asyncio.get_running_loop().time() >= control.phase_deadline
            ):
                raise TimeoutError
            # Project bounded text on the server before transporting the preview.
            from compass_agent.data_connections.retail_access_refs import (
                MAX_KEYS,
                reference,
                retail_projection,
            )

            await cursor.execute("SAVEPOINT dq_exec")
            try:
                projected = (
                    await retail_projection(cursor, binding, query)
                    if isinstance(action, ReadSql) and not is_metadata(relations)
                    else None
                )
                from compass_agent.execution_context import current_execution

                journal = current_execution.get()
                if fingerprint_only and isinstance(action, ReadSql):
                    from compass_agent.tools.builtin.database_query.result_output import (
                        result_fingerprint,
                    )

                    return await result_fingerprint(cursor, query, control)
                if (
                    isinstance(action, ReadSql)
                    and not is_metadata(relations)
                    and getattr(journal, "context_results", None)
                ):
                    from compass_agent.tools.builtin.database_query.result_output import full_result

                    return await full_result(
                        cursor, query, binding, control, journal, projected, call.id
                    )
                preview = (
                    "SELECT pg_catalog.left(pg_catalog.row_to_json(result)::text, "
                    f"{MAX_RESULT_BYTES + 1}) AS line FROM ({query}) AS result LIMIT {MAX_ROWS + 1}"
                )
                if projected:
                    await cursor.execute(
                        f"WITH preview AS ({preview}), "
                        f"keys AS ({projected[0]} LIMIT {MAX_KEYS + 1}) "
                        "SELECT (SELECT json_agg(line) FROM preview), "
                        "(SELECT json_agg(keys) FROM keys)"
                    )
                    values_read, keys = await cursor.fetchone()
                    rows = [(line,) for line in values_read or ()]
                    ref = reference(binding, projected, keys)
                else:
                    await cursor.execute(preview, values)
                    rows = await cursor.fetchall()
                    ref = {"kind": "transient"}
                    if isinstance(action, (ListTables, DescribeTable)):
                        metadata = [json.loads(row[0]) for row in rows]
                        columns = (
                            {
                                action.schema_name + "." + action.table: [
                                    m["column_name"] for m in metadata
                                ]
                            }
                            if isinstance(action, DescribeTable)
                            else {m["schema"] + "." + m["table"]: [] for m in metadata}
                        )
                        ref = {
                            "kind": "metadata",
                            "columns": columns,
                            "tenant": str(binding.tenant_id),
                            "workspace": str(binding.workspace_id),
                            "role": binding.role,
                            "revision": binding.revision,
                            "connection": str(binding.connection_id or binding.workspace_id),
                        }
                from dataclasses import replace

                if is_metadata(relations):
                    ref = {
                        "kind": "metadata",
                        "columns": {},
                        "tenant": str(binding.tenant_id),
                        "workspace": str(binding.workspace_id),
                        "role": binding.role,
                        "revision": binding.revision,
                        "connection": str(binding.connection_id or binding.workspace_id),
                    }
                return replace(_bounded_result(rows, offset), source_ref=ref)
            except psycopg.errors.UndefinedColumn:
                await cursor.execute("ROLLBACK TO SAVEPOINT dq_exec")
                return await column_error(cursor, relations)

        from compass_agent.execution_context import current_execution

        return await read_database(
            binding,
            control,
            read,
            repeatable_read=bool(getattr(current_execution.get(), "context_results", None)),
        )
    except (ValueError, RecursionError):
        return "invalid_parameters"
    except DatabaseIdentityMismatch:
        return "database_unavailable"
    except (TimeoutError, psycopg.errors.QueryCanceled):
        return "database_query_timeout"
    except psycopg.Error:
        return "database_query_failed"


def fingerprint(args, context=None):
    from hashlib import sha256

    from compass_agent.llm.continuation import canonical

    if args.get("op", "query") != "query" or not args.get("sql"):
        return None
    return sha256(canonical({"tool": "database_query", "args": args})).hexdigest(), "query"


SPEC = ToolSpec(
    "database_query",
    "builtin",
    "database",
    definition,
    lambda context: context.database,
    parallel_safe=True,
    fingerprint=fingerprint,
)


SPEC = replace(
    SPEC, governance=ToolGovernance(channel="always_on", pack="data_base", subagent_allowed=True)
)

SPEC = replace(SPEC, normalize_input=normalize_input)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="database_query",
            description=tool_text("database_query"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
