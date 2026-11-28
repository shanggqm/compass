"""Agent-owned prompt library. Mutating commands serialize on the singleton head."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from compass_agent import schemas as dto
from compass_agent.promptops import catalog
from compass_agent.promptops.entities import (
    PromptCommand,
    PromptFile,
    PromptGeneration,
    PromptHead,
    PromptVersion,
    now,
)
from compass_agent.promptops.errors import PromptError
from compass_agent.promptops.reader import PromptReader
from compass_agent.promptops.starter_catalog import optional_showcase_paths, validate_starter
from compass_agent.promptops.template import validate_template


def missing(what):
    return PromptError("not_found", f"找不到{what}。", 404)


def conflict(message):
    return PromptError("prompt_conflict", message, 409)


def head(tx, lock=False):
    q = select(PromptHead).where(PromptHead.id == 1).execution_options(populate_existing=True)
    row = tx.scalar(q.with_for_update() if lock else q)
    if row is None:
        raise PromptError("baseline_unavailable", "提示词数据库尚未完成初始化。", 503)
    return row


def active(tx, pointer):
    return (
        PromptReader._generation(tx, pointer.generation) if pointer.generation is not None else None
    )


def file_row(tx, path):
    row = tx.get(PromptFile, catalog.normalize_path(path))
    if row is None:
        raise missing("文件")
    return row


def version_row(tx, path, version):
    row = tx.get(PromptVersion, (path, version))
    if row is None:
        raise missing("文件版本")
    return row


def snapshot_row(tx, number):
    row = tx.scalar(select(PromptGeneration).where(PromptGeneration.snapshot_no == number))
    if row is None:
        raise missing("整库快照")
    return row


def entry(row, manifest, refs):
    return dict(
        path=row.path,
        origin=row.origin,
        current_version=manifest.get(row.path),
        edit_rev=row.edit_rev,
        active_epoch=row.active_epoch,
        has_draft=row.draft_body is not None,
        used=row.path in refs,
    )


def asset_title(body):
    for line in (body or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line.startswith("**") and line.endswith("**") and len(line) > 4:
            return line[2:-2].strip()
    return ""


def tree(tx, prefix, q, cursor, limit):
    pointer = head(tx)
    gen = active(tx, pointer)
    manifest = gen.manifest if gen else {}
    refs, _ = catalog.usages()
    query = select(PromptFile).options(defer(PromptFile.last_import_body)).order_by(PromptFile.path)
    if catalog.MEMBERS:
        query = query.where(PromptFile.path.not_in(catalog.MEMBERS))
    if prefix:
        query = query.where(PromptFile.path.startswith(prefix, autoescape=True))
    if cursor:
        query = query.where(PromptFile.path > cursor)
    matches = []
    for row in tx.scalars(query if q else query.limit(limit + 1)):
        if q and q.casefold() not in row.path.casefold():
            body = row.draft_body
            if body is None and row.path in manifest:
                body = version_row(tx, row.path, manifest[row.path]).body
            title = asset_title(body)
            if q.casefold() not in title.casefold():
                continue
        matches.append(entry(row, manifest, refs))
        if len(matches) > limit:
            break
    return dict(
        generation=pointer.generation,
        snapshot_no=pointer.last_snapshot_no,
        items=matches[:limit],
        next_cursor=matches[limit - 1]["path"] if len(matches) > limit else None,
    )


def file_detail(tx, path, version=None):
    row = file_row(tx, path)
    gen = active(tx, head(tx))
    manifest = gen.manifest if gen else {}
    refs, allowed = catalog.usages()
    number = version if version is not None else manifest.get(row.path)
    body = version_row(tx, row.path, number).body if number is not None else None
    from compass_agent.promptops.structured import contract_digest

    return dict(
        **entry(row, manifest, refs),
        body=body,
        version=number,
        draft_body=row.draft_body if version is None else None,
        draft_base_version=row.draft_base_version,
        draft_base_epoch=row.draft_base_epoch,
        usages=sorted(refs.get(row.path, ())),
        variables=sorted(allowed.get(row.path, catalog.EXAMPLE_VALUES)),
        kind="tool"
        if path.startswith("tools/") and path.endswith(".json")
        else "json"
        if path.endswith(".json")
        else "markdown",
        contract_sha256=contract_digest(path),
        governance=tool_governance(path),
        retired=catalog.retired(path),
    )


def numbered_page(tx, query, attr, cursor, limit, project):
    if cursor is not None:
        if not cursor.isascii() or not cursor.isdigit() or len(cursor) > 18:
            raise PromptError("invalid_cursor", "分页游标无效。")
        query = query.where(attr < int(cursor))
    rows = list(tx.scalars(query.order_by(attr.desc()).limit(limit + 1)))
    return {
        "items": [project(r) for r in rows[:limit]],
        "next_cursor": str(getattr(rows[limit - 1], attr.key)) if len(rows) > limit else None,
    }


def history(tx, path, cursor, limit):
    row = file_row(tx, path)
    return numbered_page(
        tx,
        select(PromptVersion).where(PromptVersion.path == row.path),
        PromptVersion.version,
        cursor,
        limit,
        lambda r: dict(
            path=r.path,
            version=r.version,
            sha256=r.sha256,
            created_at=r.created_at,
            created_by=r.created_by,
            note=r.note,
        ),
    )


def snapshot_info(r):
    return dict(
        snapshot_no=r.snapshot_no,
        generation=r.id,
        manifest_digest=r.manifest_digest,
        policy_version=r.policy_version,
        policy_digest=r.policy_digest,
        created_at=r.created_at,
        created_by=r.created_by,
        note=r.note,
    )


def snapshots(tx, cursor, limit):
    return numbered_page(
        tx,
        select(PromptGeneration).where(PromptGeneration.snapshot_no.is_not(None)),
        PromptGeneration.snapshot_no,
        cursor,
        limit,
        snapshot_info,
    )


def snapshot(tx, number, cursor, limit):
    row = snapshot_row(tx, number)
    paths = sorted(p for p in row.manifest if not cursor or p > cursor)
    return dict(
        **snapshot_info(row),
        items=[{"path": p, "version": row.manifest[p]} for p in paths[:limit]],
        next_cursor=paths[limit - 1] if len(paths) > limit else None,
    )


def command(tx, actor, request_id):
    row = tx.get(PromptCommand, (actor, request_id))
    if row is None:
        raise missing("命令回执")
    return row.receipt


def mutate(tx: Session, actor: UUID, name: str, data, apply: Callable):
    pointer = head(tx, lock=True)
    value = catalog.canonical_hash([name, data.model_dump(mode="json")])
    old = tx.get(PromptCommand, (actor, data.request_id))
    if old:
        if old.input_hash != value:
            raise conflict("该请求标识已用于其他操作。")
        return old.receipt
    result = dto.PromptReceipt.model_validate(apply(tx, pointer, actor, data)).model_dump(
        mode="json"
    )
    tx.add(
        PromptCommand(
            actor_id=actor,
            request_id=data.request_id,
            action=name,
            input_hash=value,
            receipt=result,
        )
    )
    tx.flush()
    return result


def new_file(tx, path, origin, actor):
    collision = tx.scalar(select(PromptFile.path).where(PromptFile.portable_key == path.casefold()))
    if collision:
        raise conflict("存在大小写或规范化后同名的文件。")
    row = PromptFile(
        path=path,
        portable_key=path.casefold(),
        origin=origin,
        edit_rev=1,
        max_version=0,
        active_epoch=0,
        updated_by=actor,
    )
    tx.add(row)
    return row


def save_draft(tx, pointer, actor, data):
    path = catalog.normalize_path(data.path)
    if catalog.retired(path):
        from compass_agent.promptops.layout import asset_path

        raise PromptError("asset_consolidated", f"此文件已合并，请编辑 {asset_path(path)}。")
    historical = isinstance(data, dto.PromptDraftFromVersion)
    body = version_row(tx, path, data.version).body if historical else data.body
    catalog.validate_body(body, draft=True)
    if path.endswith(".json"):
        validate_template(path, body)
    row = tx.get(PromptFile, path)
    if (row.edit_rev if row else 0) != data.expected_edit_rev or (
        row.active_epoch if row else 0
    ) != data.base_epoch:
        raise conflict("文件已变化，请重新打开并核对草稿与生效版本。")
    if row is None:
        row = new_file(tx, path, "online", actor)
    else:
        row.edit_rev += 1
    gen = active(tx, pointer)
    row.draft_body = body
    row.draft_base_version = gen.manifest.get(path) if gen else None
    row.draft_base_epoch = row.active_epoch
    row.updated_by, row.updated_at = actor, now()
    return dict(
        action="draft-from-version" if historical else "file-draft",
        path=path,
        edit_rev=row.edit_rev,
    )


def import_rows(tx):
    assets = catalog.bundled()
    pointer = head(tx)
    gen = active(tx, pointer)
    manifest = gen.manifest if gen else {}
    current = {r.path: r for r in tx.scalars(select(PromptFile))}
    known = {
        (r.path, r.version): r.body
        for r in tx.scalars(
            select(PromptVersion)
            .where(PromptVersion.path.in_(assets))
            .where(PromptVersion.version.in_(set(manifest.values())))
        )
        if manifest.get(r.path) == r.version
    }
    package = catalog.canonical_hash({p: catalog.digest_text(body) for p, body in assets.items()})
    rows = []
    for path, local in sorted(assets.items()):
        row = current.get(path)
        base = row.last_import_body if row else None
        online = (
            row.draft_body
            if row and row.draft_body is not None
            else known.get((path, manifest.get(path)))
        )
        if not row:
            state = "new"
        elif online is None:
            state = "conflict"
        elif local == online:
            state = "unchanged"
        elif local == base:
            state = "online"
        elif online == base and base is not None:
            state = "local"
        else:
            state = "conflict"
        rows.append(
            dict(
                path=path,
                state=state,
                edit_rev=row.edit_rev if row else 0,
                base_body=base,
                local_body=local,
                online_body=online,
            )
        )
    confirmation = catalog.canonical_hash(
        [package, [(r["path"], r["state"], r["edit_rev"]) for r in rows]]
    )
    return rows, package, confirmation, manifest


def import_preview(tx, data):
    rows, package, confirmation, _ = import_rows(tx)
    if data.path:
        path = catalog.normalize_path(data.path)
        page = [r for r in rows if r["path"] == path]
        if not page:
            raise missing("随包文件")
    else:
        page = [
            {k: r[k] for k in ("path", "state", "edit_rev")}
            for r in rows
            if not data.cursor or r["path"] > data.cursor
        ]
    return dict(
        total=len(rows),
        package_digest=package,
        confirmation_hash=confirmation,
        items=page[: data.limit],
        next_cursor=page[data.limit - 1]["path"] if len(page) > data.limit else None,
    )


def import_package(tx, pointer, actor, data):
    rows, package, confirmation, manifest = import_rows(tx)
    if (package, confirmation) != (data.package_digest, data.confirmation_hash):
        raise conflict("本地目录或在线草稿已变化，请重新预览。")
    choices = {catalog.normalize_path(c.path): c for c in data.choices}
    if len(choices) != len(data.choices) or set(choices) - set(catalog.bundled()):
        raise PromptError("invalid_selection", "导入选择包含重复或缺失的路径。")
    counts = {"adopted": 0, "retained": 0, "skipped": 0}
    for item in rows:
        path, local, state = item["path"], item["local_body"], item["state"]
        row = tx.get(PromptFile, path)
        item_choice = choices.get(path)
        if not item_choice and state == "conflict":
            raise conflict(f"请先核对 {path} 的导入冲突。")
        choice = (
            item_choice.choice
            if item_choice
            else ("local" if state in {"new", "local"} else "online")
        )
        body = item_choice.body if item_choice and choice == "merge" else local
        if choice == "merge" and body is None:
            raise PromptError("missing_merge", f"{path} 缺少合并稿。")
        if choice != "online":
            catalog.validate_body(body, draft=True)
            if path.endswith(".json"):
                validate_template(path, body)
        if choice == "online" and not row:
            counts["skipped"] += 1
            continue
        if row and choice == "online" and row.last_import_body == local:
            counts["skipped"] += 1
            continue
        if row is None:
            row = new_file(tx, path, "bundled", actor)
        else:
            row.edit_rev += 1
        if choice != "online":
            row.draft_body = body
            row.draft_base_version = manifest.get(path)
            row.draft_base_epoch = row.active_epoch
        row.origin = "bundled"
        row.last_import_body = local
        row.last_import_hash = catalog.digest_text(local)
        row.updated_by, row.updated_at = actor, now()
        counts["retained" if choice == "online" else "adopted"] += 1
    return dict(action="import", **counts)


@dataclass
class ReleasePlan:
    files: dict
    manifest: dict
    policy: tuple[str, str]
    previous: PromptGeneration | None
    restore: PromptGeneration | None
    drafts: list
    bodies: dict
    preview: dto.PromptReleasePreview


def changes(before, after, refs):
    return [
        dto.PromptChange(
            path=p,
            old_version=before.get(p),
            new_version=after.get(p),
            usages=sorted(refs.get(p, ())),
        )
        for p in sorted(before.keys() | after.keys())
        if before.get(p) != after.get(p)
    ]


def restore_consolidated(tx, snapshot_manifest, before, files):
    """Restore historical content into the current layout without changing the old snapshot."""
    from compass_agent.promptops.layout import MEMBERS, dumps, replace_member
    from compass_agent.promptops.structured import parse

    manifest = {p: v for p, v in snapshot_manifest.items() if p not in MEMBERS}
    updates = {}
    for path, source in catalog.bundled().items():
        if path in snapshot_manifest:
            continue
        if path not in files:
            raise PromptError("sync_required", "请先同步当前部署的结构化资产。", 409)
        body = source
        if path.endswith(".json"):
            document = parse(source)
            for legacy, member in MEMBERS.items():
                if member["path"] == path and legacy in snapshot_manifest:
                    old = version_row(tx, legacy, snapshot_manifest[legacy])
                    if catalog.digest_text(old.body) != old.sha256:
                        raise PromptError("corrupt_prompt", "历史提示词正文摘要不匹配。", 503)
                    replace_member(document, legacy, old.body)
            body = dumps(document)
        active_body = version_row(tx, path, before[path]).body if path in before else None
        if active_body == body:
            manifest[path] = before[path]
        else:
            manifest[path] = files[path].max_version + 1
            updates[path] = body
    return manifest, updates


def release_plan(tx, pointer, data):
    previous = active(tx, pointer)
    before = previous.manifest if previous else {}
    files = {r.path: r for r in tx.scalars(select(PromptFile))}
    latest = snapshot_row(tx, pointer.last_snapshot_no) if pointer.last_snapshot_no else None
    current = catalog.current_policy()
    policy = (
        current
        if previous is None or data.action == "all"
        else (previous.policy_version, previous.policy_digest)
    )
    restore = None
    manifest = before.copy()
    errors = []
    fingerprint = []
    updates = {}
    if data.action == "restore":
        restore = snapshot_row(tx, data.snapshot_no)
        manifest = restore.manifest.copy()
        policy = (restore.policy_version, restore.policy_digest)
        if any(catalog.retired(path) for path in manifest):
            from compass_agent.context_runtime.pipeline import compatible_policy

            if not compatible_policy(*policy):
                raise PromptError("unsupported_policy", "当前部署不支持此历史策略。", 409)
            manifest, updates = restore_consolidated(tx, manifest, before, files)
            policy = current
        elif policy != current:
            from compass_agent.context_runtime.pipeline import compatible_policy

            if compatible_policy(*policy):
                policy = current
        drafts = []
    elif data.action == "file":
        path = catalog.normalize_path(data.path)
        if catalog.retired(path):
            from compass_agent.promptops.layout import asset_path

            raise PromptError("asset_consolidated", f"此文件已合并，请发布 {asset_path(path)}。")
        if path not in files:
            raise missing("待发布文件")
        if not previous:
            errors.append("请先整体发布初始提示词。")
        if files[path].draft_body is None and path not in before:
            errors.append("文件没有待发布的草稿。")
        drafts = [files[path]] if files[path].draft_body is not None else []
    else:
        drafts = [
            f for f in files.values() if f.draft_body is not None and not catalog.retired(f.path)
        ]
    refs, allowed = catalog.usages()
    if policy != current:
        errors.append("当前部署未提供快照所需的策略版本与摘要。")
    for row in sorted(drafts, key=lambda r: r.path):
        p = row.path
        fingerprint.append(
            (
                p,
                row.edit_rev,
                row.draft_base_version,
                row.draft_base_epoch,
                catalog.digest_text(row.draft_body),
            )
        )
        if row.draft_base_epoch != row.active_epoch or row.draft_base_version != before.get(p):
            errors.append(f"{p} 的草稿基线已变化，请核对并重新保存。")
        if p not in before or version_row(tx, p, before[p]).body != row.draft_body:
            manifest[p] = row.max_version + 1
            updates[p] = row.draft_body
    required = set(refs)
    if previous:
        from compass_agent.promptops.layout import asset_path
        from compass_agent.promptops.starter_catalog import starter_usages

        optional = optional_showcase_paths()
        required_starters = {asset_path(p) for p in starter_usages() if p not in optional}
        required.difference_update({asset_path(p) for p in optional} - required_starters)
    else:
        required.update(catalog.bundled())
    for path in sorted(required - manifest.keys()):
        errors.append(f"缺少必要文件 {path}。")
    # Publication and restore validate the exact candidate versions, including shared paths.
    for p, num in manifest.items():
        try:
            if p in updates:
                body = updates[p]
            else:
                row = version_row(tx, p, num)
                body = row.body
                if catalog.digest_text(body) != row.sha256:
                    raise PromptError("invalid_digest", "正文摘要不匹配。")
            validate_template(
                p, body, catalog.member_usages()[1] if not p.endswith(".json") else allowed
            )
            validate_starter(p, body)
        except (PromptError, ValueError) as exc:
            errors.append(f"{p} · {exc.message if isinstance(exc, PromptError) else str(exc)}")
    baseline = latest.manifest if latest else {}
    if data.action == "file":
        target = catalog.normalize_path(data.path)
        stamp = [
            data.action,
            target,
            files[target].active_epoch,
            before.get(target),
            fingerprint,
            policy,
            sorted(refs.get(target, ())),
        ]
    else:
        stamp = [
            data.action,
            pointer.generation,
            pointer.last_snapshot_no,
            restore.id if restore else None,
            fingerprint,
            manifest,
            policy,
        ]
    preview = dto.PromptReleasePreview(
        generation=pointer.generation,
        snapshot_no=pointer.last_snapshot_no or None,
        changes=changes(before, manifest, refs),
        since_snapshot=changes(baseline, manifest, refs),
        errors=errors,
        confirmation_hash=catalog.canonical_hash(stamp),
    )
    return ReleasePlan(files, manifest, policy, previous, restore, drafts, updates, preview)


def commit_release(tx, pointer, actor, data):
    plan = release_plan(tx, pointer, data)
    if plan.preview.confirmation_hash != data.confirmation_hash:
        raise conflict("发布预览已过期，请重新核对影响范围。")
    if plan.preview.errors:
        raise PromptError("invalid_release", " ".join(plan.preview.errors[:8]))
    before = plan.previous.manifest if plan.previous else {}
    latest = snapshot_row(tx, pointer.last_snapshot_no) if pointer.last_snapshot_no else None
    create = True
    if data.action == "file":
        create = bool(plan.bodies)
    elif data.action == "all" and latest is not None:
        create = (
            latest.manifest != plan.manifest
            or (latest.policy_version, latest.policy_digest) != plan.policy
        )
        # Head may differ from the last snapshot even though the candidate equals it.
        create = (
            create
            or before != plan.manifest
            or (plan.previous.policy_version, plan.previous.policy_digest) != plan.policy
        )
    for path, body in plan.bodies.items():
        row = plan.files[path]
        row.max_version = plan.manifest[path]
        tx.add(
            PromptVersion(
                path=path,
                version=row.max_version,
                body=body,
                byte_count=len(body.encode()),
                sha256=catalog.digest_text(body),
                created_by=actor,
                note=data.note,
            )
        )
    consumed = {r.path for r in plan.drafts}
    changed = {c.path for c in plan.preview.changes}
    for path in consumed | changed:
        row = plan.files[path]
        row.edit_rev += 1
        row.updated_by, row.updated_at = actor, now()
        if path in changed:
            row.active_epoch += 1
        if path in consumed:
            row.draft_body = row.draft_base_version = row.draft_base_epoch = None
    if create:
        snapshot_no = None if data.action == "file" else pointer.last_snapshot_no + 1
        generation = PromptGeneration(
            parent_id=pointer.generation,
            manifest=plan.manifest,
            manifest_digest=catalog.canonical_hash(plan.manifest),
            policy_version=plan.policy[0],
            policy_digest=plan.policy[1],
            action=data.action,
            snapshot_no=snapshot_no,
            restored_from_id=plan.restore.id if plan.restore else None,
            created_by=actor,
            note=data.note,
        )
        # SQLite fixtures use the same head lock protocol with a deterministic integer key.
        if tx.get_bind().dialect.name == "sqlite":
            generation.id = (pointer.generation or 0) + 1
        tx.add(generation)
        tx.flush()
        pointer.generation = generation.id
        if snapshot_no is not None:
            pointer.last_snapshot_no = snapshot_no
    path = catalog.normalize_path(data.path) if data.path else None
    return dict(
        action=data.action,
        generation=pointer.generation,
        snapshot_no=None if data.action == "file" else pointer.last_snapshot_no,
        changed_count=len(changed),
        path=path,
        version=plan.manifest.get(path) if path else None,
    )


def bootstrap(tx, actor_id=None):
    """Explicit initial publication for a fresh deployment or an integration fixture."""
    from uuid import UUID, uuid4

    actor = actor_id or UUID(int=0)
    pointer = head(tx, lock=True)
    if pointer.generation is not None:
        return pointer.generation
    view = dto.PromptImportPreview.model_validate(
        import_preview(tx, dto.PromptImportPreviewInput())
    )
    mutate(
        tx,
        actor,
        "import",
        dto.PromptImport(
            request_id=uuid4(),
            package_digest=view.package_digest,
            confirmation_hash=view.confirmation_hash,
        ),
        import_package,
    )
    target = dto.PromptReleaseTarget(action="all")
    preview = release_plan(tx, pointer, target).preview
    receipt = mutate(
        tx,
        actor,
        "releases",
        dto.PromptRelease(
            action="all",
            note="首次整体发布",
            confirmation_hash=preview.confirmation_hash,
            request_id=uuid4(),
        ),
        commit_release,
    )
    return receipt["generation"]


def tool_governance(path):
    from compass_agent.tools.catalog import TOOLS
    from compass_agent.tools.contracts import ToolPolicy

    name = path.removeprefix("tools/").removesuffix(".json")
    spec = next((s for s in TOOLS if s.name == name), None)
    if spec is None:
        return None
    policy = spec.factory().policy if spec.factory else ToolPolicy()
    if name == "schedule_write":
        from compass_agent.tools.builtin.schedule_write import WRITE_POLICY

        policy = WRITE_POLICY
    return {
        "channel": spec.governance.channel,
        "pack": spec.governance.pack,
        "effect": policy.effect,
        "approval": policy.approval,
    }
