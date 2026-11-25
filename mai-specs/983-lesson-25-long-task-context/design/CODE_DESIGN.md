# 第 25 课｜代码设计附录

[返回技术方案](TECH_DESIGN.md)

本附录是设计示意，未运行、未测试。沿用 `apps/agent/src/compass_agent` 的 Python 3.13、Pydantic 与 SQLAlchemy 风格；路径相对该目录。“现有”指当前工作区已有符号，“拟议”指本次新增或修改。外围样板（导入、日志、指标）省略。已按[设计评审](../review/design_review.md)修订。

## 1. Goal 循环与控制器（拟议 `goal/loop.py`、`goal/controller.py`）

后台循环与副本健康隔离：

```python
async def goal_loop(runtime, controller, interval=GOAL_LOOP_INTERVAL):
    """DurableRuntime.start 中以独立任务启动，不放进 maintain 的 try 块。"""
    while not runtime.closing:
        if runtime.owner_healthy and runtime.store.ready:
            try:
                await asyncio.to_thread(controller.tick)
            except Exception:                          # 只记日志与指标，不修改 ready / owner_healthy
                log.exception("goal.tick_failed")
        await asyncio.sleep(interval)
```

候选查询与逐目标短事务：

```python
class GoalController:
    def __init__(self, store, private):
        self.store, self.private = store, private      # 现有 RuntimeStore 与私有状态封装

    def tick(self, limit=16) -> None:
        for goal_id in self._candidates(limit):        # 无锁读取候选 id
            try:
                self._process(goal_id)
            except ModelError as error:                # 准入错误按目标隔离
                self._on_admission_error(goal_id, error)
            except Exception:
                log.exception("goal.process_failed", goal_id=goal_id)

    def _candidates(self, limit):
        latest = latest_run_per_session_subquery()     # (session_id, ordinal, status, run_id)
        with self.store.transaction() as tx:
            return tx.scalars(
                select(AgentGoal.id)
                .join(latest, latest.c.session_id == AgentGoal.session_id)
                .where(
                    AgentGoal.status.in_(OPEN_STATUSES),
                    latest.c.status.in_(TERMINAL),
                    ~exists_undelivered_outbox(latest.c.run_id),
                    or_(
                        latest.c.ordinal > AgentGoal.last_settled_ordinal,       # 需结算
                        and_(AgentGoal.status == "active",                         # 已结算，等待准入
                             or_(AgentGoal.retry_after.is_(None),
                                 AgentGoal.retry_after <= func.now())),
                    ),
                )
                .limit(limit)
            ).all()

    def _process(self, goal_id):
        with self.store.transaction(admission=True) as tx:
            # 锁顺序：session → goal（与 act、submit、账本写入一致）
            session_id = tx.scalar(select(AgentGoal.session_id).where(AgentGoal.id == goal_id))
            session = tx.scalar(select(AgentSession).where(AgentSession.id == session_id)
                                .with_for_update())
            goal = tx.scalar(select(AgentGoal).where(AgentGoal.id == goal_id)
                             .with_for_update(skip_locked=True))
            if goal is None or goal.status not in OPEN_STATUSES:
                return
            for run in unsettled_terminal_runs(tx, goal):   # 按 ordinal 升序
                self._settle(tx, goal, run)
            if goal.status == "active" and self._admissible(tx, goal, session):
                self._admit(tx, goal, session)
```

结算：

```python
class GoalController:  # 续
    def _settle(self, tx, goal, run) -> None:
        goal.last_settled_ordinal = run.ordinal
        meta = run.config_snapshot.get("goal") or {}
        if meta.get("goalId") != str(goal.id):         # 绑定其他目标或无目标的 Run，仅推进序号
            return
        usage = accepted_attempt_usage(tx, run.id)     # SUM(agent_attempt.budget_receipt) WHERE accepted，含子任务 step
        goal.tokens_used += usage.tokens
        goal.cost_used_microusd += usage.cost_microusd
        round_no = meta.get("round")
        is_round = round_no is not None
        executed = usage.attempts > 0
        rt = dict(goal.runtime_state)

        # 未执行过模型调用就结束的续轮（排队中被插话、暂停或 goal_inactive）：回退轮数，不计进展与失败。
        if is_round and not executed and round_no == goal.rounds_started:
            goal.rounds_started -= 1
            return

        if goal.status == "paused" and goal.reason_code == "user_interrupted":
            if not is_round:                           # 插话的用户 Run 结束：自动恢复
                self._transition(goal, "active", None)
            return                                     # 被插话停止的续轮：不计无进展
        if goal.status != "active":
            return

        rt["failureRounds"] = rt.get("failureRounds", 0) + 1 if run.status == "failed" else 0
        if run.status == "canceled" and run.reason_code not in {"goal_inactive"}:
            goal.runtime_state = rt
            self._transition(goal, "paused", "run_canceled")
            return
        if run.reason_code in {"token_budget", "cost_budget"}:   # 锚点触发的目标预算硬停
            goal.runtime_state = rt
            self._transition(goal, "budget_limited", run.reason_code)
            return

        started_rev = meta.get("ledgerRevision")
        rt["noProgressRounds"] = (
            rt.get("noProgressRounds", 0) + 1
            if started_rev is not None and started_rev == goal.ledger_revision
            else 0
        )
        goal.runtime_state = rt
        if stop := self._stop_reason(goal):
            self._transition(goal, *stop)

    def _stop_reason(self, goal):
        budget, rt = goal.budget, goal.runtime_state
        if rt.get("failureRounds", 0) >= FAILURE_LIMIT:
            return "blocked", "runtime_failures"
        if rt.get("noProgressRounds", 0) >= NO_PROGRESS_LIMIT:
            return "blocked", "no_progress"
        if goal.rounds_started >= budget["maxRounds"]:
            return "budget_limited", "max_rounds"
        if goal.tokens_used >= budget["tokens"]:
            return "budget_limited", "token_budget"
        if goal.cost_used_microusd >= budget["costMicrousd"]:
            return "budget_limited", "cost_budget"
        return None
```

准入：

```python
class GoalController:  # 续
    def _admissible(self, tx, goal, session) -> bool:
        if self._stop_reason(goal) or session.deleted_at is not None:
            return False
        if goal.retry_after and goal.retry_after > self.store.now():
            return False
        latest = latest_run(tx, session.id)
        # 最新 Run 必须已结算；用户 Run 也绑定目标并被结算，因此插话后可以继续。
        return latest is not None and latest.status in TERMINAL \
            and latest.ordinal == goal.last_settled_ordinal

    def _admit(self, tx, goal, session) -> None:
        previous = latest_run(tx, session.id)
        round_no = goal.rounds_started + 1
        command = SessionSubmit(
            request_id=uuid5(goal.id, f"round:{round_no}"),   # 重复准入命中同一命令
            session_id=session.id,
            question=continuation_marker(round_no),             # 公开文本：继续推进目标 · 第 N 轮
            constraints=previous.config_snapshot.get("constraints"),
        )
        self.store.submit_in_transaction(
            tx, command, owner_context(session),
            run_environment=previous.config_snapshot.get("promptEnvironment"),
            goal_round=round_no,
        )
        goal.rounds_started = round_no                 # 准入即计数，同一事务提交
        goal.retry_after = None

    def _on_admission_error(self, goal_id, error):
        with self.store.transaction() as tx:
            goal = lock_goal_after_session(tx, goal_id)
            if goal is None:
                return
            if error.status == 429:                    # 租户容量
                goal.retry_after = self.store.now() + timedelta(seconds=30)
            elif error.status == 404:                  # 会话已删除
                self._transition(goal, "canceled", "session_deleted")
            # 409 session_busy、503：不改状态，下个周期重判


def owner_context(session) -> TrustedContext:
    return TrustedContext(
        actor_user_id=session.owner_user_id,
        scope=TenantScope(kind="tenant", tenant_id=session.tenant_id,
                          workspace_id=session.workspace_id),
        purpose="chat",
    )
```

`RuntimeStore.submit_in_transaction` 的拟议改动：

```diff
     def submit_in_transaction(
-        self, tx, command, context, *, imported_history=None, schedule=None, run_environment=None
+        self, tx, command, context, *, imported_history=None, schedule=None,
+        run_environment=None, goal_round=None,
     ):
         payload = self.submission_payload(command)
-        kind = "schedule_submit" if schedule else "submit"
+        kind = "schedule_submit" if schedule else ("goal_submit" if goal_round else "submit")
         ...
+        goal = open_goal_for_session(tx, session.id)     # 已锁 session 之后读取
+        if goal_round is not None and goal is None:
+            raise ModelError("goal_not_found", 409)
+        if goal:
+            config["goal"] = {                        # 不改动 agent_run 表结构
+                "goalId": str(goal.id),
+                "round": goal_round,
+                "ledgerRevision": goal.ledger_revision,
+            }
         run = AgentRun(..., config_snapshot=config)
```

用户操作（锁顺序 `session → run → goal`）：

```python
class GoalController:  # 续
    def act(self, context, session_id, action, expected_revision, request_id):
        with self.store.transaction() as tx:
            session = tx.scalar(select(AgentSession)
                                .where(AgentSession.id == session_id, *owner_filter(context))
                                .with_for_update())
            if session is None:
                raise ModelError("not_found", 404)
            run = latest_run(tx, session.id, for_update=True)
            goal = open_goal_for_session(tx, session.id, for_update=True)
            if goal is None:
                raise ModelError("not_found", 404)
            if goal.revision != expected_revision:
                raise ModelError("goal_stale_revision", 409)
            active = run is not None and run.status not in TERMINAL
            match action, goal.status:
                case "pause", "active":
                    self._transition(goal, "paused", "user_paused")
                    if active:
                        request_pause_or_cancel(tx, run, context)       # 复用 commands.py
                case "interrupt", "active":
                    self._transition(goal, "paused", "user_interrupted")
                    if active and (run.config_snapshot.get("goal") or {}).get("round") is not None:
                        request_cancel(tx, run, context)                # 排队中立即终态，执行中在边界停止
                case "resume", ("paused" | "blocked"):
                    goal.revision += 1
                    goal.runtime_state = {**goal.runtime_state, "noProgressRounds": 0, "failureRounds": 0}
                    self._transition(goal, "active", None)
                    if active and run.status == "paused":
                        request_resume(tx, run, context)                # 锚点会读取新版本
                case "cancel", _:
                    self._transition(goal, "canceled", "user_canceled")
                    if active:
                        request_cancel(tx, run, context)
                case _:
                    raise ModelError("goal_invalid_transition", 409)
            return goal_view(tx, goal), (run.id if active else None)
```

Review 问题：

- 所有终态路径都由 `tick` 统一结算，结算判据是“最新 Run 已终态、已投递、序号大于 `last_settled_ordinal`”。
- 回退 `rounds_started` 只针对“未执行任何模型调用的最后一个续轮”。

## 2. 契约类型

目标契约与视图（拟议 `goal/contracts.py` 与 `schemas.py`）：

```python
class AcceptanceItem(RunContractModel):
    id: str = Field(pattern=r"^a[0-9]{1,2}$")
    text: str = Field(min_length=1, max_length=300)


class GoalContract(RunContractModel):
    objective: str = Field(min_length=1, max_length=2000)
    acceptance: list[AcceptanceItem] = Field(min_length=1, max_length=10)
    constraints: list[str] = Field(default_factory=list, max_length=10)


class GoalBudget(RunContractModel):
    max_rounds: int = Field(alias="maxRounds", ge=1, le=32)
    tokens: int = Field(ge=10_000, le=10_000_000)
    cost_microusd: int = Field(alias="costMicrousd", ge=1)        # 上限取 AutonomyLimits 默认值，运行时校验


class GoalProposal(RunContractModel):              # ToolApprovalProposal.goal
    goal_id: RunUUID | None = Field(default=None, alias="goalId")
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=1)
    contract: GoalContract
    budget: GoalBudget


class GoalView(RunContractModel):                  # GET /sessions/{id}/goal 与 actions 响应
    goal_id: RunUUID = Field(alias="goalId")
    revision: int = Field(ge=1)
    status: Literal["active", "paused", "blocked", "budget_limited", "completed", "canceled"]
    reason_code: str | None = Field(default=None, alias="reasonCode", max_length=40)
    reason: str | None = Field(default=None, max_length=60)        # 可见原因，C-05
    contract: GoalContract
    satisfied: list[str] = Field(default_factory=list)             # 已有证据支撑的验收项 id
    budget: GoalBudget
    usage: GoalUsage                                # rounds、tokensPct、costPct、overrun
    counts: GoalCounts                              # openTodos、conflicts、stale、reusedCalls
```

委派契约：

```python
class InputRef(RunContractModel):
    kind: Literal["ledger", "artifact", "evidence"]
    id: str = Field(min_length=1, max_length=200)


class DelegationTask(RunContractModel):
    name: str = Field(min_length=1, max_length=40)
    objective: str = Field(min_length=1, max_length=2000)
    expectations: list[str] = Field(default_factory=list, max_length=8)
    constraints: list[str] = Field(default_factory=list, max_length=10)
    inputs: list[InputRef] = Field(default_factory=list, max_length=12)
    tools: list[str] = Field(default_factory=list, max_length=8)      # 空表示父可见只读工具全集
    connection_ids: list[RunUUID] = Field(default_factory=list, alias="connectionIds", max_length=4)
    max_steps: int = Field(default=3, alias="maxSteps", ge=3, le=8)  # 8 = 子进程 MAX_STEPS


class SubAgentInput(RunContractModel):              # 修改 tools/builtin/sub_agent
    prompt: str | None = Field(default=None, min_length=1, max_length=8000, pattern=r"\S",
                               description="旧写法：等同于只有 objective 的单个任务")
    tasks: list[DelegationTask] | None = Field(
        default=None, min_length=1, max_length=MAX_TASKS,              # MAX_TASKS = 4
        description="一个或多个互不依赖的分支；同一调用内同时运行 MAX_PARALLEL（2）个",
    )

    @model_validator(mode="after")
    def one_of(self):
        if (self.prompt is None) == (self.tasks is None):
            raise ValueError("provide exactly one of prompt or tasks")
        if self.tasks and len({t.name for t in self.tasks}) != len(self.tasks):
            raise ValueError("task names must be unique")
        return self
```

回传契约：

```python
class Finding(RunContractModel):
    statement: str = Field(min_length=1, max_length=500)
    fact_key: str | None = Field(default=None, alias="factKey", pattern=r"^[a-z0-9_.:-]{1,80}$")
    value: Decimal | None = None
    unit: str | None = Field(default=None, max_length=20)
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds", max_length=8)


class ChildReturn(RunContractModel):                # report_to_parent(kind="final") 的结果体
    status: Literal["completed", "partial", "blocked"]
    summary: str = Field(min_length=1, max_length=1000)
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    artifact_refs: list[str] = Field(default_factory=list, alias="artifactRefs", max_length=8)
    gaps: list[str] = Field(default_factory=list, max_length=10)
    blockers: list[str] = Field(default_factory=list, max_length=5)
    next_actions: list[str] = Field(default_factory=list, alias="nextActions", max_length=5)


class ChildDelivery(RunContractModel):              # 父工具观察与账本 delivery 条目
    delivery_id: RunUUID = Field(alias="deliveryId")
    child_run_id: RunUUID = Field(alias="childRunId")
    reason: Literal["returned", "natural_end", "step_limit", "budget", "error"]
    status: Literal["completed", "partial", "blocked", "failed"]
    result: ChildReturn | None
    rejected_evidence_ids: list[str] = Field(default_factory=list, alias="rejectedEvidenceIds")
    answer: str                                     # 结构化时为 summary，未结构化时为完整最终回答（兼容 977）
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds")  # 子任务路径下全部合法证据
```

子 Agent 汇报工具（宿主拦截，合并“进度”和“交回结果”）：

```python
class ProgressReport(RunContractModel):
    kind: Literal["progress"]
    summary: str = Field(min_length=1, max_length=120, description="一句进展，不写数值明细")


class FinalReport(RunContractModel):
    kind: Literal["final"]
    result: ChildReturn


class ReportToParentInput(RootModel[Annotated[ProgressReport | FinalReport,
                                             Field(discriminator="kind")]]):
    pass
```

`goal` 工具入参（单工具，`op` 判别联合；写法与 `todo_write` 的 `op` 一致）：

```python
class ProposeGoal(RunContractModel):
    op: Literal["propose"]
    contract: GoalContract
    budget: GoalBudget | None = None                 # 省略取默认值
    goal_id: RunUUID | None = Field(default=None, alias="goalId")
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=1)


class LedgerItem(RunContractModel):
    type: Literal["conclusion", "todo", "todo_done", "failure"]
    key: str = Field(pattern=r"^[a-z0-9_.:-]{1,80}$")
    title: str = Field(min_length=1, max_length=200)
    text: str | None = Field(default=None, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds", max_length=8)
    fact_key: str | None = Field(default=None, alias="factKey", pattern=r"^[a-z0-9_.:-]{1,80}$")
    value: Decimal | None = None
    unit: str | None = Field(default=None, max_length=20)
    acceptance_ids: list[str] = Field(default_factory=list, alias="acceptanceIds", max_length=10)
    required: bool = True

    @model_validator(mode="after")
    def conclusion_needs_evidence(self):
        if self.type == "conclusion" and not self.evidence_ids:
            raise ValueError("conclusion requires evidenceIds")
        return self


class RecordProgress(RunContractModel):
    op: Literal["record"]
    items: list[LedgerItem] = Field(min_length=1, max_length=10)


class CompleteGoal(RunContractModel):
    op: Literal["complete"]
    expected_revision: int = Field(alias="expectedRevision", ge=1)


class BlockGoal(RunContractModel):
    op: Literal["block"]
    reason: str = Field(min_length=1, max_length=60)


class GoalInput(RootModel[Annotated[ProposeGoal | RecordProgress | CompleteGoal | BlockGoal,
                                   Field(discriminator="op")]]):
    pass
```

## 3. 进度账本（拟议 `goal/ledger.py`）

```python
class Ledger:
    def __init__(self, store, private):
        self.store, self.private = store, private

    def upsert(self, tx, goal, key, *, kind, status, body) -> bool:
        """调用方已在 store.lock + require_epoch 事务内按 run → goal 加锁。
        body 含正文及 sourceRefs / dependsOn / acceptanceIds / required / dataVersion / runId / childStepId。"""
        entry = tx.get(AgentGoalEntry, (goal.id, key), with_for_update=True)   # 联合主键点查
        if entry is not None and (entry.kind, entry.status, self.private.open(goal.id, "ledger", entry.private_body)) == (kind, status, body):
            return False                              # 重放或重复写：不推进 ledger_revision
        if entry is None:
            if count_entries(tx, goal.id) >= LEDGER_LIMIT:
                raise ModelError("ledger_full", 409)
            entry = AgentGoalEntry(goal_id=goal.id, entry_key=key)
            tx.add(entry)
        entry.kind = kind
        entry.status = status
        entry.private_body = self.private.seal(goal.id, "ledger", body)
        entry.updated_at = self.store.now()
        goal.ledger_revision += 1
        return True

    def record_conclusion(self, tx, goal, key, finding, *, journal, run_id, child_step_id=None):
        # 结论来源取本 Run 证据记录中的 sourceRefs，格式与 message.source_refs 一致。
        records = [journal.evidence_record(e) for e in finding.evidence_ids]
        if not records or None in records:
            raise ModelError("evidence_required", 422)
        body = {
            **finding.model_dump(mode="json"),
            "sourceRefs": dedupe(ref for r in records for ref in r["sourceRefs"]),
            "acceptanceIds": finding_acceptance(finding),
            "dependsOn": [],
            "runId": str(run_id),
            **({"childStepId": str(child_step_id)} if child_step_id else {}),
        }
        self.upsert(tx, goal, key, kind="conclusion", status="done", body=body)
        if finding.fact_key and finding.value is not None:
            self._mark_conflicts(tx, goal, key, finding)

    def _mark_conflicts(self, tx, goal, key, finding):
        for other, prior in opened_entries(tx, goal.id, self.private, kind="conclusion"):
            if other.entry_key == key or prior.get("factKey") != finding.fact_key:
                continue
            if (Decimal(prior["value"]), prior.get("unit")) == (finding.value, finding.unit):
                continue
            self._set_status(tx, goal, other.entry_key, "conflict", add_dep=key)
            self._set_status(tx, goal, key, "conflict", add_dep=other.entry_key)
```

复用判定与跨 Run 证据复制：

```python
    def reusable(self, tx, goal, spec, args, binding, *, journal, access):
        """派发器 admission 前调用；按联合主键 (goal_id, f"{kind}:{fp}") 点查。"""
        if spec.fingerprint is None or goal is None or goal.status != "active":
            return None
        fp, kind = spec.fingerprint(args, binding)
        entry = tx.get(AgentGoalEntry, (goal.id, f"{kind}:{fp}"))
        if entry is None or entry.status != "done":
            return None
        body = self.private.open(goal.id, "ledger", entry.private_body)
        refs = body.get("sourceRefs", [])
        if any(r.get("kind") == "transient" for r in refs):
            return None                                 # transient 来源只在原 Run 有效
        if kind == "query" and body.get("dataVersion") != str(binding.config_revision):
            return None                                 # 数据版本变化：照常执行
        if not access.allows(refs):
            return None                                 # 来源失权：照常执行，工具自身鉴权会拒绝
        # 与 importEvidenceId 同规则：sourceRefs 必须全部在本 Run 可用来源内，再以新 id 写入本 Run。
        evidence_id = journal.import_foreign_evidence(body["evidence"], origin=entry.entry_key)
        if evidence_id is None:
            return None
        goal.runtime_state = {**goal.runtime_state, "reusedCalls": goal.runtime_state.get("reusedCalls", 0) + 1}
        return {"reused": True, "result": body["result"], "evidenceIds": [evidence_id]}

    def drop_unauthorized(self, tx, goal, access) -> None:
        """恢复核对：单目标上限 400 条，一次性加载解密后沿 dependsOn 反向传播。"""
        rows = opened_entries(tx, goal.id, self.private)
        dropped = {e.entry_key for e, b in rows
                   if e.status == "done" and b.get("sourceRefs") and not access.allows(b["sourceRefs"])}
        frontier = set(dropped)
        while frontier:
            frontier = {e.entry_key for e, b in rows if frontier.intersection(b.get("dependsOn", []))} - dropped
            dropped |= frontier
        for key in sorted(dropped):
            self._set_status(tx, goal, key, "dropped")
```

`journal.import_foreign_evidence` 是拟议新方法，T4 先以集成测试验证“上一轮查询 → 本轮复用 → 最终回答引用”；不通过时 `reusable` 恒返回 `None`，退化为锚点提示。

`database_query` 的指纹适配器示意（位于 `tools/builtin/database_query/`，与定义同目录）：

```python
def fingerprint(args, binding):
    sql = " ".join(sqlparse.format(args["sql"], keyword_case="lower", strip_comments=True).split())
    material = {"tool": "database_query", "connection": str(binding.connection_id), "sql": sql}
    return sha256(json_bytes(material)).hexdigest(), "query"
```

## 4. 子任务并发预算（修改 `harness/autonomy.py`、`runtime/subagent.py`）

```diff
     @contextmanager
-    def child_budget(self):
+    def child_budget(self, steps: int | None = None):
         self.require_continue()
         available = AutonomyLimits(...)                    # 现有：父剩余减去已有切片
         if (available.steps < 3 or available.tools < 3 or available.tokens < 1
                 or len(self.children) >= 2):
             raise AutonomyStopped(...)                     # 现有；委派路径在进入前已用 can_slice 预检
+        wanted = 3 if steps is None else steps
         lanes = 3 - len(self.children)
         child = AutonomyBudget(
             self.profiles,
             AutonomyLimits(
-                steps=min(3, available.steps - 1),
-                tools=min(4, available.tools - 1),
+                steps=min(wanted, available.steps - 1),
+                tools=min(wanted + 1, available.tools - 1),
                 tokens=available.tokens // lanes,
                 cost_microusd=available.cost_microusd // lanes,
             ),
         )
         self.children.add(child)
         try:
             yield child
         finally:                                           # 现有：并回父预算，不新增 absorb
             ...
+
+    def can_slice(self, steps: int) -> bool:
+        """admit_delegation 预检：失败时返回 ToolRejected，而非抛出会停止父 Run 的 AutonomyStopped。"""
+        ...
```

```diff
 class SubAgent:
     def __init__(self, bindings):
         self.bindings = bindings
-        self.model_lock = asyncio.Lock()

     async def run(self, request, context, *, group=None, identifier=None):
+        tasks = admit_delegation(request, context, self.bindings)       # 整批校验；越权或超额：ToolRejected
+        parent = self.bindings.budget if self.bindings.controlled else None
+        gate = asyncio.Semaphore(MAX_PARALLEL)                           # MAX_PARALLEL = 2
+
+        async def one(index, task):
+            async with gate:
+                with (parent.child_budget(steps=task.max_steps) if parent else nullcontext()) as budget:
+                    session = ChildSession(task=task, budget=budget)     # 每个任务独立
+                    return await self._run_task(index, session, context, group)
+
+        # 任一任务被取消时 gather 传播 PhaseCanceled；其余任务各自形成交付。
+        deliveries = await asyncio.gather(*(one(i, t) for i, t in enumerate(tasks)))
+        return deliveries[0] if request.prompt is not None else {"deliveries": deliveries}

     async def _model(self, index, request, context, node_id, session):
-        async with self.model_lock:
-            budget = bindings.budget if bindings.controlled else None
-            if budget:
-                budget.require_step()
-                budget.generation = step.model_call_id
+        budget = session.budget
+        if budget:
+            budget.require_step()
+            budget.generation = step.model_call_id        # 子预算独立对象，无共享写
```

Review 问题：两个并发切片的上限之和不超过父剩余额度（`child_budget` 现有计算），去锁后不会超支。实现时需核对 `bindings.executor._invoke_model` 内部没有其他依赖 `bindings.budget` 的共享可变字段。

## 5. ChildSession 与回传结算（修改 `runtime/subagent.py`）

```python
@dataclass
class ChildSession:
    task: DelegationTask
    budget: AutonomyBudget | None
    returned: ChildReturn | None = None
    final_answer: str | None = None
    end: str = "natural_end"            # returned | natural_end | step_limit | budget | error


async def _process(self, prepared, child_run_id, context, node_id, session):
    try:
        await self._drive_child(context, node_id, session)   # 原帧循环；report_to_parent(final) 后宿主结束子进程
    except AutonomyStopped:
        session.end = "budget"
    except ToolRejected as error:
        session.end = "step_limit" if error.code == "subagent_step_limit" else "error"
    # PhaseCanceled / CancelledError 继续向上抛，不形成交付
    return await self._settle(prepared, child_run_id, session)


async def _settle(self, prepared, child_run_id, session):
    if session.returned is not None:
        status, result, answer, reason = (session.returned.status, session.returned,
                                          session.returned.summary, "returned")
    elif session.final_answer and session.end == "natural_end":
        result = ChildReturn(status="partial", summary=session.final_answer[:1000],
                             gaps=["子任务未按契约返回结构化结果"])
        status, answer, reason = "partial", session.final_answer, "natural_end"
    else:
        result, status, answer, reason = None, "failed", "子任务未完成", session.end

    # 在子任务 execution_scope 内读取：只保留子任务执行路径产生的证据。
    owned = set(current_execution.get().answer_evidence.get(execution_path.get(), {}))
    rejected = []
    if result:
        for finding in result.findings:
            rejected += [e for e in finding.evidence_ids if e not in owned]
            finding.evidence_ids = [e for e in finding.evidence_ids if e in owned]

    delivery = ChildDelivery(
        delivery_id=uuid5(child_run_id, "delivery"), child_run_id=child_run_id,
        reason=reason, status=status, result=result, rejected_evidence_ids=rejected,
        answer=answer, evidence_ids=sorted(owned),
    )
    value = delivery.model_dump(mode="json", by_alias=True)
    # 现有 host_result：首次写入；重放时比对摘要，不一致抛 workflow_replay_mismatch。
    await current_execution.get().host_result(prepared, value, notify=False)
    await self.goal_bridge.merge_delivery(delivery)  # 账本 delivery:<childRunId> 与发现，幂等
    return value
```

宿主拦截工具（`_tools` 内，不进入 Dispatcher）：

```python
if call.function.name == "report_to_parent":
    report = ReportToParentInput.model_validate_json(call.function.arguments).root
    if report.kind == "progress":
        trace.update(node_id, summary=report.summary)   # 覆盖写同一节点，不作为交付
        results.append(ToolResult(call.id, "ok", '{"accepted":true}'))
    else:
        session.returned = report.result
        session.end = "returned"
        raise ChildReturned()                           # _drive_child 捕获后结束子进程并进入结算
```

## 5a. `goal` 工具定义（拟议 `tools/builtin/goal/__init__.py`）

```python
NAME = "goal"


def definition(_context):
    return ToolDefinition(function=ToolFunction(
        name=NAME, description=tool_text("goal"), parameters=GoalInput.model_json_schema()))


async def prepare_approval(args, context):
    """只有 propose 需要用户确认；其余 op 返回 None，不进入审批等待。"""
    request = GoalInput.model_validate(args).root
    if request.op != "propose":
        return None
    proposal = ToolApprovalProposal(
        proposal_id=uuid5(context.step_id, f"goal:{context.call_id}"),
        tool_name=NAME, tool_call_id=context.call_id,
        arguments_digest=sha256(json_bytes(args)).hexdigest(),
        summary=request.contract.objective[:2000],
        goal=GoalProposal(goal_id=request.goal_id, expected_revision=request.expected_revision,
                          contract=request.contract, budget=request.budget or default_budget()),
    )
    return PreparedApproval(proposal=proposal, private={})


def authorize(args, context):
    request = GoalInput.model_validate(args).root
    goal = context.goal                               # Run 绑定的目标快照，可能为空
    if request.op == "propose":
        if goal and goal.status in OPEN_STATUSES and request.goal_id != goal.id:
            raise ToolRejected("goal_exists")
    elif goal is None or goal.status != "active":
        raise ToolRejected("goal_not_active")


SPEC = ToolSpec(
    NAME, "builtin", "goal", definition,
    available=lambda c: c.goal_enabled,               # 调度模式 + 受控自主 + 回答协议 2，与目标状态无关
    context_factory=goal_tool,                        # ToolPolicy("local_artifact", "none")，prepare_approval、authorize 如上
)
```

Review 问题：`ToolPolicy.approval="none"` 加逐次 `prepare_approval` 已有先例（`dispatch_batch` 对 `item.approval is not None` 的调用要求单独成批）；`effect="local_artifact"` 使 `child_tools()` 的 `read_only` 过滤自动排除本工具，`child_tools` 另做显式排除。

## 6. 完成核验（拟议 `goal/gate.py`）

```python
def request_completion(tx, goal, ledger, expected_revision, access, run_id):
    if goal.revision != expected_revision:
        return {"accepted": False, "code": "goal_stale_revision"}
    rows = opened_entries(tx, goal.id, ledger.private)  # [(entry, body), ...]
    gaps = []
    for item in goal.contract["acceptance"]:
        backing = [e for e, b in rows if item["id"] in b.get("acceptanceIds", [])
                   and e.kind == "conclusion" and e.status == "done" and access.allows(b.get("sourceRefs", []))]
        if not backing:
            gaps.append(("acceptance", item["id"], item["text"]))
    gaps += [("todo", e.entry_key, b["title"]) for e, b in rows
             if e.kind == "todo" and b.get("required", True) and e.status == "open"]
    gaps += [("conflict", e.entry_key, b["title"]) for e, b in rows if e.status == "conflict"]
    used = {d for e, b in rows if e.status == "done" for d in b.get("dependsOn", [])}
    gaps += [("stale", e.entry_key, b["title"]) for e, b in rows
             if e.status == "stale" and e.entry_key in used]

    if not gaps:
        goal.status, goal.ended_at = "completed", now()
        return {"accepted": True}
    for kind, key, text in gaps:
        if kind != "todo":                           # 已有待办不重复建
            ledger.upsert(tx, goal, f"remedy:{key}", kind="todo", status="open",
                          body={"title": f"补齐：{text}", "required": True, "dependsOn": [key], "runId": str(run_id)})
    return {"accepted": False, "gaps": [{"kind": k, "key": key} for k, key, _ in gaps]}
```

## 7. 子任务子树快照（修改 `api/sessions.py`）

```python
@router.get("/v1/analysis/runs/{run_id}/children/{child_id}")
async def child_snapshot(run_id: UUID, child_id: UUID, context=Depends(trusted), service=Depends(...)):
    run = await asyncio.to_thread(service.store.read_run, run_id, context)   # owner_filter
    if run is None:
        raise ModelError("not_found", 404)
    nodes = execution_nodes(run.public_view)            # id → RunExecutionNode（payload.kind == "execution.node"）
    root = next((n for n in nodes.values()
                 if n.kind == "subagent" and n.subagent and n.subagent.child_run_id == child_id), None)
    if root is None:
        raise ModelError("not_found", 404)              # 父子错配、跨租户、不存在同一响应
    subtree = [n for n in nodes.values() if n.id == root.id or descends_from(nodes, n.id, root.id)]
    response = snapshot_response(run, subtree)          # AgentResponse(mode="snapshot")，cursors 为当前 projected_seq
    visibility = await service.access.visibility(context)
    if run.config_snapshot.get("contextRuntime"):
        response = project_sources(response, service.store, context)
    return no_store(visible_response(response, details=visibility.details))
```

Review 问题：快照与主流使用同一 `project_sources` 与 `visible_response`，因此字段可见性一致；快照附带的游标仅作基线，后续更新来自主事件流。
