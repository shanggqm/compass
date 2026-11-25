"""Capture access witnesses alongside certified retail SELECT results."""

from __future__ import annotations

from pglast import ast, parse_sql
from pglast.enums import SetOperation
from pglast.stream import RawStream
from psycopg import sql

from compass_agent.data_connections.legacy import LegacyScopeCheck

MAX_KEYS = 64

# Only certify the retail adapter's tenant/region policy. An added restrictive
# policy or a change of the covered column invalidates existing witnesses.
RETAIL_POLICY = """
    SELECT count(*) = 1 AND bool_and(polpermissive AND polroles = ARRAY[0]::oid[]
        AND pg_catalog.pg_get_expr(polqual,polrelid) = %s)
    FROM pg_catalog.pg_policy WHERE polrelid=pg_catalog.to_regclass(%s) AND polcmd IN ('r','*')
"""


def expected_policy(region):
    return "sec.allowed(tenant_id, " + ("region_id)" if region else "NULL::bigint)")


def _nodes(value):
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, ast.Node):
        yield value
        for name in value:
            yield from _nodes(getattr(value, name))


async def retail_projection(cursor, binding, query):
    """Return a witness query only for SELECTs whose base-table inputs are identifiable."""
    if not isinstance(binding.scope_check, LegacyScopeCheck):
        return None
    stmt = parse_sql(query)[0].stmt
    if isinstance(stmt, ast.SelectStmt) and stmt.op == SetOperation.SETOP_UNION:
        # Certify every branch with the ordinary single-SELECT rules. Collecting
        # all branch inputs also covers UNION DISTINCT and outer ORDER/LIMIT.
        branches = []

        def collect(node):
            if not isinstance(node, ast.SelectStmt) or node.withClause:
                return False
            if node.op == SetOperation.SETOP_UNION:
                return collect(node.larg) and collect(node.rarg)
            branches.append(node)
            return len(branches) <= 4

        if not collect(stmt):
            return None
        projections = []
        columns = {}
        for branch in branches:
            projection = await retail_projection(cursor, binding, RawStream()(branch))
            if projection is None:
                return None
            projections.append("(" + projection[0] + ")")
            for relation, used in projection[1].items():
                columns.setdefault(relation, set()).update(used)
        if len(columns) > 4:
            return None
        return " UNION ".join(projections), {k: sorted(v) for k, v in columns.items()}
    if not isinstance(stmt, ast.SelectStmt) or not stmt.fromClause:
        return None
    nodes = list(_nodes(stmt))
    if sum(isinstance(n, ast.SelectStmt) for n in nodes) != 1 or any(
        isinstance(n, (ast.RangeFunction, ast.RangeTableSample, ast.RangeSubselect))
        or isinstance(n, ast.JoinExpr)
        and (n.usingClause or n.isNatural or n.alias)
        for n in nodes
    ):
        return None
    sources = [n for n in nodes if isinstance(n, ast.RangeVar)]
    if not 1 <= len(sources) <= 4 or any(
        (s.schemaname != "core" and (s.schemaname, s.relname) != ("ops", "source_watermark"))
        or s.catalogname
        or (s.alias and s.alias.colnames)
        for s in sources
    ):
        return None
    catalog = {}
    for s in sources:
        if not s.schemaname or not s.relname:
            return None
        alias = s.alias.aliasname if s.alias else s.relname
        name = s.schemaname + "." + s.relname
        await cursor.execute(
            "SELECT attname FROM pg_catalog.pg_attribute "
            "WHERE attrelid=pg_catalog.to_regclass(%s) AND attnum>0 AND NOT attisdropped "
            "AND pg_catalog.row_security_active(attrelid)",
            (name,),
        )
        all_cols = {r[0] for r in await cursor.fetchall()}
        if "tenant_id" not in all_cols or alias in catalog:
            return None
        region = "region_id" in all_cols
        await cursor.execute(RETAIL_POLICY, (expected_policy(region), name))
        if await cursor.fetchone() != (True,):
            return None
        catalog[alias] = (
            name,
            all_cols,
            {"tenant_id", "region_id"} if region else {"tenant_id"},
            region,
        )
    # Only a plain SELECT-list star expands to these base columns. Other uses
    # (whole-row expressions, functions, joins with merged columns) stay transient.
    projections = [target.val for target in stmt.targetList or ()]
    for ref in (n for n in nodes if isinstance(n, ast.ColumnRef)):
        if (
            ref.fields
            and isinstance(ref.fields[-1], ast.A_Star)
            and any(ref is projected for projected in projections)
        ):
            if len(ref.fields) == 1:
                expanded = list(catalog.values())
            elif (
                len(ref.fields) == 2
                and isinstance(ref.fields[0], ast.String)
                and ref.fields[0].sval in catalog
            ):
                expanded = [catalog[ref.fields[0].sval]]
            else:
                return None
            for _, columns, used, _ in expanded:
                used.update(columns)
            continue
        if not ref.fields or any(not isinstance(c, ast.String) for c in ref.fields):
            return None
        parts = [c.sval for c in ref.fields]
        if len(parts) == 1:
            applicable = [v for v in catalog.values() if parts[0] in v[1]]
        elif len(parts) == 2 and parts[0] in catalog:
            applicable = [catalog[parts[0]]]
        else:
            return None
        if len(applicable) != 1 or parts[-1] not in applicable[0][1]:
            return None
        applicable[0][2].add(parts[-1])
    clauses = " FROM " + ", ".join(RawStream()(f) for f in stmt.fromClause)
    if stmt.whereClause is not None:
        clauses += " WHERE " + RawStream()(stmt.whereClause)
    witnesses = []
    columns = {}
    for alias, (relation, _, used, region) in catalog.items():
        table = sql.Identifier(alias).as_string(cursor)
        reg = table + ".region_id" if region else "NULL::bigint"
        literal = sql.Literal(relation).as_string(cursor)
        witnesses.append(
            f"SELECT DISTINCT {literal} AS relation, {table}.tenant_id AS tenant, {reg} AS region"
            + clauses
        )
        columns.setdefault(relation, set()).update(used)
    return (" UNION ".join(witnesses), {k: sorted(v) for k, v in columns.items()})


def reference(binding, projection, witness):
    if not projection or not witness or len(witness) > MAX_KEYS:
        return {"kind": "transient"}
    _, columns = projection
    for entry in witness:
        if entry["tenant"] != binding.scope_check.tenant_id or (
            "region_id" in columns[entry["relation"]] and entry["region"] is None
        ):
            return {"kind": "transient"}
    return {
        "kind": "retail",
        "workspace": str(binding.workspace_id),
        "tenant": str(binding.tenant_id),
        "connection": str(binding.connection_id or binding.workspace_id),
        "revision": binding.revision,
        "role": binding.role,
        "columns": columns,
        "keys": witness,
    }
