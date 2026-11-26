# 第 26 课｜ContextOps 与上下文安全体系 供评审代码设计

> 关联主方案：[TECH_DESIGN.md](TECH_DESIGN.md) | Feature：971 | 状态：待评审

本文仅包含供架构评审的核心类型、状态不变量与关键决策分支骨架，不直接写入生产源码。

---

## 1. 在线上下文安全拦截引擎（PEP）与派生权限继承

**拟新增文件**：`apps/agent/src/compass_agent/context_security/{policy,detector,redactor,guard}.py`  
**审查重点（已按 Design Review P0/P1 与防误伤原则修订）**：
- 跨租户条目必须在进入模型前同步剔除，在任何模式下不可降级；
- 分级注入检测：`actor` 仅拦截明确系统提示提取与角色伪造（不误杀“忽略之前口径重算”等业务纠偏）；`observation` 高危确凿注入整条阻断，中危疑似语句局部剥离并隔离保留正常正文；
- 确定性标签转义（`</untrusted_context` → `&lt;/untrusted_context`）+ 固定外层 `<untrusted_context>` 标签，防逃逸同时保住 LLM Prefix Cache 命中率；
- 两道精准脱敏：密钥在进模型与落快照两道强制脱敏；PII 在落快照/审计/Trace/MCP 100% 不可逆脱敏，进模型窗口放行用户主动提供的合法查询主键；
- `derive_lineage()` 取最低 `trust` 与全部受限源 `AccessRef` **并集（Union，逻辑 AND）**，彻底杜绝权限静默放大；同编译步安全事件按 `(event_type, rule_code, source_kind)` 聚合 `hit_count` 批量异步落库。

```python
import re
from collections import Counter
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
        "injection_stripped",
        "tenant_mismatch_rejected",
        "pii_redacted",
        "trust_downgraded",
        "snapshot_viewed",
        "mcp_queried",
    ]
    action: Literal["block", "strip", "redact", "isolate", "deny", "allow_audit"]
    rule_code: str
    source_kind: str | None
    trust_level: TrustLevel | None
    hit_count: int = 1
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class DetectionHit:
    severity: Literal["high", "medium"]
    rule_code: str
    stripped_text: str | None = None


@dataclass(frozen=True, slots=True)
class DerivedLineage:
    min_trust_level: TrustLevel
    access_scope: tuple[AccessRef, ...]
    data_classification: Literal["public", "internal", "confidential", "restricted"]


class InjectionDetector(Protocol):
    def detect(self, text: str, trust: TrustLevel) -> DetectionHit | None:
        """actor 仅检测提示提取与角色伪造；observation 返回 high(阻断) 或 medium(剥离)。"""


def derive_lineage(items: tuple[ContextItem, ...]) -> DerivedLineage:
    """派生数据强制继承最低信任等级与全部受限源 AccessRef 并集（逻辑 AND）。"""
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
    strict_scope: tuple[AccessRef, ...] = (
        tuple(
            sorted(
                set.union(*scoped_sets),
                key=lambda a: (a.tenant_id, a.workspace_id, a.binding, a.revision),
            )
        )
        if scoped_sets
        else ()
    )
    classification: Literal["public", "internal", "confidential", "restricted"] = (
        "restricted" if strict_scope else "internal"
    )
    return DerivedLineage(
        min_trust_level=min_trust,
        access_scope=strict_scope,
        data_classification=classification,
    )


def wrap_untrusted_observation(text: str, source_kind: str) -> str:
    """确定性转义闭合标签，防止标签逃逸且不破坏多轮 Prefix Cache。"""
    escaped = re.sub(r"</?\s*untrusted_context\b", "&lt;/untrusted_context", text, flags=re.IGNORECASE)
    return (
        f'<untrusted_context source="{source_kind}" trust="observation">\n'
        f"{escaped}\n"
        "</untrusted_context>"
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
        """同步在线 PEP：租户强校验 -> 分级注入防护 -> 进窗密钥脱敏 -> 防逃逸边界封装。"""
        accepted: list[ContextItem] = []
        raw_events: list[SecurityEventRecord] = []
        blocked_reasons: dict[str, str] = {}

        for item in items:
            # 1. 多租户与工作区强校验（任何模式下均不可绕过）
            if any(
                acc.tenant_id != str(ctx.tenant_id)
                or acc.workspace_id != str(ctx.workspace_id)
                for acc in item.access
            ):
                blocked_reasons[item.item_id] = "security_tenant_mismatch"
                raw_events.append(
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

            text = item.message.content or ""

            # 2. 分级注入检测：actor 仅扫高危提取/伪造；observation 高危阻断、中危局部剥离
            if item.trust in {"observation", "actor"}:
                hit = self._detector.detect(text, item.trust)
                if hit is not None:
                    if hit.severity == "high":
                        raw_events.append(
                            SecurityEventRecord(
                                event_type="injection_blocked",
                                action="block" if self._mode == "enforce" else "allow_audit",
                                rule_code=hit.rule_code,
                                source_kind=item.source.kind,
                                trust_level=item.trust,
                                item_id=item.item_id,
                            )
                        )
                        if self._mode == "enforce":
                            blocked_reasons[item.item_id] = f"security_injection:{hit.rule_code}"
                            continue
                    elif hit.stripped_text is not None and self._mode == "enforce":
                        text = hit.stripped_text
                        raw_events.append(
                            SecurityEventRecord(
                                event_type="injection_stripped",
                                action="strip",
                                rule_code=hit.rule_code,
                                source_kind=item.source.kind,
                                trust_level=item.trust,
                                item_id=item.item_id,
                            )
                        )

            # 3. 进模型窗口脱敏：所有条目强制脱敏高确定性密钥；observation 额外脱敏非查询主键 PII
            # （落库加密快照时另调用 redactor.redact_for_storage 做 100% 不可逆脱敏）
            sanitized_text, redacted_rules = self._redactor.redact_for_model(text, trust=item.trust)
            for rule in redacted_rules:
                raw_events.append(
                    SecurityEventRecord(
                        event_type="pii_redacted",
                        action="redact",
                        rule_code=rule,
                        source_kind=item.source.kind,
                        trust_level=item.trust,
                    )
                )

            # 4. 不可信 observation 确定性防逃逸封装（保住 Prefix Cache）
            if item.trust == "observation":
                sanitized_text = wrap_untrusted_observation(sanitized_text, item.source.kind)

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

        return accepted, aggregate_security_events(raw_events), blocked_reasons


def aggregate_security_events(events: list[SecurityEventRecord]) -> list[SecurityEventRecord]:
    """按步聚合高频脱敏/拦截计数，避免单次编译触发上百次 DB INSERT。"""
    counts: Counter[tuple] = Counter()
    sample: dict[tuple, SecurityEventRecord] = {}
    for ev in events:
        key = (ev.event_type, ev.action, ev.rule_code, ev.source_kind, ev.trust_level)
        counts[key] += ev.hit_count
        sample.setdefault(key, ev)
    return [
        SecurityEventRecord(
            event_type=sample[k].event_type,
            action=sample[k].action,
            rule_code=sample[k].rule_code,
            source_kind=sample[k].source_kind,
            trust_level=sample[k].trust_level,
            hit_count=cnt,
            item_id=sample[k].item_id if cnt == 1 else None,
        )
        for k, cnt in counts.items()
    ]
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
