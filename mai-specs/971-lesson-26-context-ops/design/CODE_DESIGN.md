# 第 26 课｜ContextOps 与上下文安全体系 供评审代码设计

> 关联主方案：[TECH_DESIGN.md](TECH_DESIGN.md) | Feature：971 | 状态：待评审

本文仅包含供架构评审的核心类型、状态不变量与关键决策分支骨架，不直接写入生产源码。

---

## 1. 在线上下文安全拦截引擎（PEP）与派生权限继承

**拟新增文件**：`apps/agent/src/compass_agent/context_security/{policy,detector,redactor,guard}.py`  
**审查重点**：
- 跨租户条目必须在进入模型前被同步剔除，且记录 `tenant_mismatch_rejected`；
- 高危 Indirect Prompt Injection 仅扫描 `observation` 与 `actor`，`managed` 与 `dictionary` 不被误杀；
- 派生数据（Summary / Snapshot）调用 `derive_lineage()` 强制取最低 `trust` 与最严格 `AccessRef` 交集，彻底杜绝“摘要洗白”与“权限扩大”。

```python
from dataclasses import dataclass
from typing import Literal, Protocol

from compass_agent.context_runtime.contracts import AccessRef, ContextItem
from compass_agent.schemas import TrustedContext

TrustLevel = Literal["managed", "dictionary", "actor", "observation"]
TRUST_RANK: dict[TrustLevel, int] = {
    "observation": 0,
    "actor": 1,
    "dictionary": 2,
    "managed": 3,
}


@dataclass(frozen=True, slots=True)
class SecurityEventRecord:
    event_type: Literal[
        "injection_blocked",
        "tenant_mismatch_rejected",
        "pii_redacted",
        "trust_downgraded",
        "snapshot_viewed",
        "mcp_queried",
    ]
    action: Literal["block", "redact", "isolate", "deny", "allow_audit"]
    rule_code: str
    source_kind: str | None
    trust_level: TrustLevel | None
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class DerivedLineage:
    min_trust_level: TrustLevel
    access_scope: tuple[AccessRef, ...]
    data_classification: Literal["public", "internal", "confidential", "restricted"]


class InjectionDetector(Protocol):
    def detect(self, text: str, trust: TrustLevel) -> str | None:
        """命中高危注入返回 rule_code，未命中返回 None。"""


def derive_lineage(items: tuple[ContextItem, ...]) -> DerivedLineage:
    """派生数据强制继承最低信任等级与最严格访问范围。"""
    if not items:
        return DerivedLineage(
            min_trust_level="managed",
            access_scope=(),
            data_classification="internal",
        )
    min_trust: TrustLevel = min(
        (item.trust for item in items),
        key=lambda t: TRUST_RANK[t],
    )
    scoped_sets = [set(item.access) for item in items if item.access]
    if not scoped_sets:
        strict_scope: tuple[AccessRef, ...] = ()
    else:
        # 保留所有受限源的最小交集；若无交集则保留全部约束以便服务端逐条复核
        intersection = set.intersection(*scoped_sets)
        strict_scope = tuple(
            sorted(
                intersection if intersection else set.union(*scoped_sets),
                key=lambda a: (a.tenant_id, a.workspace_id, a.binding, a.revision),
            )
        )
    classification: Literal["public", "internal", "confidential", "restricted"] = (
        "restricted" if strict_scope else "internal"
    )
    return DerivedLineage(
        min_trust_level=min_trust,
        access_scope=strict_scope,
        data_classification=classification,
    )


class ContextSecurityGuard:
    def __init__(self, detector: InjectionDetector, redactor, *, mode: str = "enforce"):
        self._detector = detector
        self._redactor = redactor
        self._mode = mode

    def enforce(
        self,
        items: list[ContextItem],
        ctx: TrustedContext,
    ) -> tuple[list[ContextItem], list[SecurityEventRecord], dict[str, str]]:
        """同步在线 PEP：租户强校验 -> 注入阻断 -> PII 脱敏 -> 不可信边界封装。"""
        accepted: list[ContextItem] = []
        events: list[SecurityEventRecord] = []
        blocked_reasons: dict[str, str] = {}

        for item in items:
            # 1. 多租户与工作区强校验（任何模式下均不可绕过）
            if any(
                acc.tenant_id != str(ctx.tenant_id)
                or acc.workspace_id != str(ctx.workspace_id)
                for acc in item.access
            ):
                blocked_reasons[item.item_id] = "security_tenant_mismatch"
                events.append(
                    SecurityEventRecord(
                        event_type="tenant_mismatch_rejected",
                        action="deny",
                        rule_code="tenant_workspace_mismatch",
                        source_kind=item.source.kind,
                        trust_level=item.trust,
                        item_id=item.item_id,
                    )
                )
                continue

            raw_text = item.message.content or ""

            # 2. 仅对低信任来源（observation / actor）执行 Indirect Prompt Injection 检测
            if item.trust in {"observation", "actor"}:
                rule_code = self._detector.detect(raw_text, item.trust)
                if rule_code is not None:
                    events.append(
                        SecurityEventRecord(
                            event_type="injection_blocked",
                            action="block" if self._mode == "enforce" else "allow_audit",
                            rule_code=rule_code,
                            source_kind=item.source.kind,
                            trust_level=item.trust,
                            item_id=item.item_id,
                        )
                    )
                    if self._mode == "enforce":
                        blocked_reasons[item.item_id] = f"security_injection:{rule_code}"
                        continue

            # 3. 敏感字段检测与脱敏（先脱敏再入窗、再落快照）
            sanitized_text, redacted_rules = self._redactor.redact(raw_text)
            for rule in redacted_rules:
                events.append(
                    SecurityEventRecord(
                        event_type="pii_redacted",
                        action="redact",
                        rule_code=rule,
                        source_kind=item.source.kind,
                        trust_level=item.trust,
                        item_id=item.item_id,
                    )
                )

            # 4. 不可信 observation 强制包裹数据边界，禁止升权为指令
            if item.trust == "observation":
                sanitized_text = (
                    f"<untrusted_context source=\"{item.source.kind}\" trust=\"observation\">\n"
                    f"{sanitized_text}\n"
                    f"</untrusted_context>"
                )

            updated_msg = item.message.model_copy(update={"content": sanitized_text})
            accepted.append(
                ContextItem(
                    item_id=item.item_id,
                    kind=item.kind,
                    message=updated_msg,
                    source=item.source,
                    group=item.group,
                    required=item.required,
                    access=item.access,
                    trust=item.trust,
                    data_as_of=item.data_as_of,
                    byte_bound=len(sanitized_text.encode("utf-8")),
                )
            )

        return accepted, events, blocked_reasons
```

---

## 2. OTel + Langfuse 上下文 Span 映射（零二开）

**拟新增/修改文件**：`apps/agent/src/compass_agent/observability/context_spans.py`、`privacy.py`  
**审查重点**：
- 直接挂载在既有 `Observation` 父句柄下，不新建全局 TracerProvider；
- 显式声明 Langfuse 原生 `retriever`、`guardrail`、`chain` 类型，全部属性经 `privacy.fields()` 白名单过滤，零正文泄露。

```python
from compass_agent.context_security.policy import SecurityEventRecord
from compass_agent.observability.observer import Observation


def emit_context_spans(
    parent: Observation | None,
    *,
    snapshot_ref: str | None,
    policy_digest: str,
    candidate_count: int,
    selected_count: int,
    omitted_count: int,
    tokens_before: int,
    tokens_after: int,
    token_limit: int,
    security_events: list[SecurityEventRecord],
) -> None:
    if parent is None:
        return

    # 1. 安全拦截 Span -> Langfuse guardrail 类型
    blocked = sum(1 for e in security_events if e.action in {"block", "deny"})
    redacted = sum(1 for e in security_events if e.action == "redact")
    with parent.child(
        "compass.context.guardrail",
        snapshot_ref=snapshot_ref,
        security_blocked_count=blocked,
        redacted_count=redacted,
    ) as guard_span:
        guard_span.native("type", "guardrail")
        for ev in security_events[:16]:  # 严守 L14 每 Span 最多 16 条事件限制
            guard_span.event(
                "context.guardrail",
                guardrail_action=ev.action,
                guardrail_rule=ev.rule_code,
                trust_level=ev.trust_level,
            )

    # 2. 上下文装配 Span -> Langfuse chain 类型
    ratio = round(tokens_after / tokens_before, 4) if tokens_before > 0 else 1.0
    with parent.child(
        "compass.context.compile",
        snapshot_ref=snapshot_ref,
        policy_version=policy_digest[:16],
        candidate_count=candidate_count,
        selected_count=selected_count,
        omitted_count=omitted_count,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        token_limit=token_limit,
        compaction_ratio=ratio,
    ) as compile_span:
        compile_span.native("type", "chain")
```

---

## 3. 加密快照与故障隔离读模型投影

**拟新增文件**：`apps/agent/src/compass_agent/context_ops/{crypto,recorder}.py`  
**审查重点**：
- 使用 HKDF 从既有 `compass_model_encryption_key` 派生独立子密钥，Fernet 加密前先 gzip 压缩；
- 解密前强制复核 `tenant_id`、`workspace_id` 与 `access_scope`，并先落 `snapshot_viewed` 审计；
- 读模型投影写入失败仅递增本地计数，绝不阻断业务主链路。

```python
import base64
import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SnapshotCipher:
    def __init__(self, master_secret: str):
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"compass-context-snapshot-v1",
        )
        derived = hkdf.derive(master_secret.encode("utf-8"))
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt_json(self, payload: dict) -> tuple[bytes, str]:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        compressed = gzip.compress(raw)
        return self._fernet.encrypt(compressed), digest

    def decrypt_json(self, ciphertext: bytes) -> dict:
        compressed = self._fernet.decrypt(ciphertext)
        return json.loads(gzip.decompress(compressed).decode("utf-8"))


def build_snapshot_ref() -> str:
    return f"csnp_{uuid4().hex}"


def snapshot_expiry(ttl_days: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=max(1, ttl_days))
```

---

## 4. 确定性 Context-only Replay 引擎

**拟新增文件**：`apps/agent/src/compass_agent/context_ops/replay.py`  
**审查重点**：
- 仅消费 `context_snapshot` 中冻结的候选上下文，不触发任何外部数据库/工具调用，不调用真实 LLM；
- 相同参数下保证 100% 确定性复现原装配结果，并输出清晰的前后差异供界面对比。

```python
from dataclasses import dataclass

from compass_agent.context_runtime.contracts import ContextItem


@dataclass(frozen=True, slots=True)
class ReplayOverride:
    max_input_tokens: int | None = None
    protect_dictionary: bool = True
    compaction_threshold: float | None = None
    max_observation_items: int | None = None


@dataclass(frozen=True, slots=True)
class ReplayDiff:
    baseline_selected_ids: list[str]
    candidate_selected_ids: list[str]
    newly_included_ids: list[str]
    newly_omitted_ids: list[str]
    baseline_tokens: int
    candidate_tokens: int
    token_delta: int
    dictionary_preserved_baseline: bool
    dictionary_preserved_candidate: bool


class ContextOnlyReplayEngine:
    def __init__(self, compiler_fn):
        self._compiler_fn = compiler_fn

    def compare(
        self,
        frozen_candidates: list[ContextItem],
        baseline_budget: int,
        override: ReplayOverride,
    ) -> ReplayDiff:
        base_out = self._compiler_fn(
            frozen_candidates,
            max_input_tokens=baseline_budget,
            protect_dictionary=False,
            compaction_threshold=None,
            max_observation_items=None,
        )
        cand_out = self._compiler_fn(
            frozen_candidates,
            max_input_tokens=override.max_input_tokens or baseline_budget,
            protect_dictionary=override.protect_dictionary,
            compaction_threshold=override.compaction_threshold,
            max_observation_items=override.max_observation_items,
        )
        base_ids = [s.item for s in base_out.selections if s.decision == "selected"]
        cand_ids = [s.item for s in cand_out.selections if s.decision == "selected"]
        base_set, cand_set = set(base_ids), set(cand_ids)

        dict_ids = {c.item_id for c in frozen_candidates if c.trust == "dictionary"}
        return ReplayDiff(
            baseline_selected_ids=base_ids,
            candidate_selected_ids=cand_ids,
            newly_included_ids=sorted(cand_set - base_set),
            newly_omitted_ids=sorted(base_set - cand_set),
            baseline_tokens=base_out.tokens_after,
            candidate_tokens=cand_out.tokens_after,
            token_delta=cand_out.tokens_after - base_out.tokens_after,
            dictionary_preserved_baseline=dict_ids.issubset(base_set),
            dictionary_preserved_candidate=dict_ids.issubset(cand_set),
        )
```

---

## 5. 只读 ContextOps MCP 服务接口

**拟新增文件**：`apps/agent/src/compass_agent/context_ops/mcp_server.py`  
**审查重点**：
- 硬编码 4 个只读工具白名单，拒绝任何未登记方法与写操作；
- 所有查询强制绑定鉴权后的 `TrustedContext`（租户/工作区），不返回未脱敏快照全文，每次调用写入 `mcp_queried` 审计事件。

```python
from compass_agent.schemas import TrustedContext

READONLY_TOOLS: dict[str, dict] = {
    "contextops_list_runs": {
        "description": "按租户与筛选条件查询脱敏后的 Run 摘要（步数、耗时、Token、淘汰率与安全拦截数）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "enum": ["24h", "7d", "30d"], "default": "24h"},
                "scene_id": {"type": "string"},
                "status": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        },
    },
    "contextops_get_run_flow": {
        "description": "查询指定 Run 的步骤流、上下文漏斗计数与逐条脱敏决策原因（不含明文快照）。",
        "inputSchema": {
            "type": "object",
            "required": ["run_id"],
            "properties": {"run_id": {"type": "string"}},
        },
    },
    "contextops_get_stats": {
        "description": "查询四维上下文聚合统计指标与高频失败环节 Top-N。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "enum": ["24h", "7d", "30d"], "default": "7d"},
                "scene_id": {"type": "string"},
            },
        },
    },
    "contextops_list_badcases": {
        "description": "查询已沉淀的脱敏上下文 Badcase 与根因分类。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_cause_category": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        },
    },
}


def dispatch_mcp_call(
    method: str,
    params: dict,
    ctx: TrustedContext,
    analytics_service,
    audit_recorder,
) -> dict:
    if method == "tools/list":
        return {
            "tools": [{"name": k, **v} for k, v in READONLY_TOOLS.items()]
        }
    if method != "tools/call":
        raise PermissionError("method_not_allowed")

    tool_name = params.get("name")
    if tool_name not in READONLY_TOOLS:
        raise PermissionError("tool_not_allowed")

    args = params.get("arguments") or {}
    audit_recorder.record_mcp_query(ctx=ctx, tool_name=tool_name)

    if tool_name == "contextops_list_runs":
        return analytics_service.list_runs(ctx, **args)
    if tool_name == "contextops_get_run_flow":
        return analytics_service.get_run_flow(ctx, run_id=args["run_id"])
    if tool_name == "contextops_get_stats":
        return analytics_service.get_overview_stats(ctx, **args)
    return analytics_service.list_badcases(ctx, **args)
```
