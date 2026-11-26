# Feature 971 安全方案评审报告（Design Review）

> 评审对象：Feature 971（第 26 课｜ContextOps 与上下文安全体系）
> 评审材料：[BRIEF.md](../BRIEF.md)、[PRD.md](../spec/PRD.md)、[TECH_DESIGN.md](../design/TECH_DESIGN.md)、[CODE_DESIGN.md](../design/CODE_DESIGN.md)
> 核对代码基线：`apps/agent/src/compass_agent/context_runtime/source_access.py`、`observability/privacy.py`
> 评审日期：2026-11-25
> **评审结论：修订复核通过（全部 P0、P1、P2 及 6 项防误伤与流畅性优化已合入 PRD / TECH_DESIGN / CODE_DESIGN）**

---

## 一、总体结论与依据

本方案在整体架构边界上方向正确：坚持原版 Langfuse 零二开、沿用第 14 课 OTel 零正文红线、将安全控制前置到在线 PEP 同步执行，并通过 HKDF 派生子密钥加密存储上下文快照。

对照真实代码基线与攻击路径推演后，发现 **2 项 P0 阻断级安全缺陷**（派生权限集合逻辑反转、边界标签逃逸与细粒度源权限复核缺失）、**3 项 P1 重要风险**（快照查看审计 fail-open、正则误脱敏破坏业务 ID、快照 TTL 清理与撤权失效未闭环）及 **1 项 P2 建议**。须先修正 P0 与明确 P1 处置方案后再进入实现。

---

## 二、关键发现（按优先级排序）

### [P0-1] `derive_lineage()` 的权限集合逻辑反转：取交集会导致派生摘要权限被静默放大

- **证据**：[CODE_DESIGN.md](../design/CODE_DESIGN.md#L78-L85)
  ```python
  intersection = set.intersection(*scoped_sets)
  strict_scope = tuple(sorted(intersection if intersection else set.union(*scoped_sets), ...))
  ```
- **触发条件与影响**：当一份派生 Summary 或 Encrypted Snapshot 由两个受限源合并生成时（例如源 A 绑定 `{财务成本, 华东区域}`，源 B 绑定 `{人事薪酬, 华东区域}`），`set.intersection` 会只保留 `{华东区域}`，直接丢弃「财务成本」和「人事薪酬」两条更严格的权限约束。任何仅具备「华东区域」权限的普通成员都将绕过细粒度权限读取该派生数据。
- **最小可执行修正**：在必要权限模型（读取者必须同时满足所有来源绑定）下，派生权限约束应取**全部受限源 `AccessRef` 的并集（Union，逻辑 AND）**；且无受限绑定的条目不得稀释受限源。仅当所有输入条目均无受限绑定时，`access_scope` 才为空。

---

### [P0-2] `<untrusted_context>` 字符串拼接存在标签逃逸风险，且 PEP 未对接既有的细粒度源权限复核

- **证据**：
  1. [CODE_DESIGN.md](../design/CODE_DESIGN.md#L167-L172) 直接用字符串拼接 `<untrusted_context ...>\n{sanitized_text}\n</untrusted_context>`，未对正文内出现的闭合标签做转义或随机边界隔离。
  2. [CODE_DESIGN.md](../design/CODE_DESIGN.md#L114-L118) 仅比对了 `acc.tenant_id` 与 `acc.workspace_id`，未校验 `acc.binding` / `acc.revision` 及细粒度数据权限。核对现网代码，[source_access.py](../../../apps/agent/src/compass_agent/context_runtime/source_access.py#L1-L100) 已实现基于 `DataConnections` 与 `retail_access_refs` 的实时源权限复核。
- **触发条件与影响**：
  1. 恶意文档或工具结果内嵌 `</untrusted_context>` 即可闭合边界标签，实现结构逃逸。
  2. 仅校验 `tenant_id + workspace_id` 会把**同租户内已被撤权、或无成本/区域权限**的历史缓存、快照或 Badcase 放行给无权用户。
- **最小可执行修正**：
  1. 包裹不可信内容前转义或剔除正文中的 `</untrusted_context>` 标记（或使用每次请求随机生成的边界 nonce），同时在方案中明确：标签包裹仅作模型侧软隔离，真正的权限硬边界由下游工具执行前鉴权与只读受限连接兜底。
  2. 在 `GET /v1/contextops/snapshots/{snapshot_ref}` 与 `POST /v1/contextops/badcases/{id}/replay` 读取前，强制复用既有 `source_access.py` 复核源数据绑定权限，避免在 971 中重写一套较弱的租户校验。

---

### [P1-1] 加密快照解密查看的审计日志采用 fail-open，存在“无痕查看敏感数据”风险

- **证据**：[TECH_DESIGN.md](../design/TECH_DESIGN.md#L115) 将普通观测投影与安全审计事件统一归入故障隔离通道，写入失败仅记告警计数。
- **触发条件与影响**：普通上下文摘要漏记允许降级；若数据库写入异常时 `GET /v1/contextops/snapshots/{snapshot_ref}` 仍返回解密后的业务快照，将导致高敏操作缺乏审计留痕。
- **最小可执行修正**：明确区分写入语义——
  - 普通 `context_decision_summary` 投影保持 **fail-open**（异常降级不阻塞主链路）；
  - 加密快照解密查看（`snapshot_viewed`）采用 **fail-closed**（审计写入失败直接返回 503 拒绝返回快照）；
  - 在线拦截事件（`injection_blocked` / `tenant_mismatch_rejected`）落库失败时，同步输出结构化安全日志（JSON stderr）兜底。

---

### [P1-2] 宽泛正则 PII 脱敏可能误伤订单号 / SKU 编码，破坏 DataAgent 计算正确性

- **证据**：[CODE_DESIGN.md](../design/CODE_DESIGN.md#L152-L164) 对所有候选条目统一执行手机号、身份证、邮箱与密钥脱敏后再入窗。
- **触发条件与影响**：电商业务数据中常见 11 位或 18 位数字编码（订单号、运单号、SKU ID），宽泛正则会将其误替换为 `[REDACTED:PHONE]`，导致后续 SQL 生成与指标聚合直接出错。
- **最小可执行修正**：
  1. 手机号正则增加严格数字边界并排除明确 ID 字段名上下文；身份证号增加 GB 11643 校验位验证；密钥仅匹配高确定性前缀（如 `Bearer `、`sk-`、`ghp_`、`postgres://user:pass@`）。
  2. `managed` 与 `dictionary` 条目仅扫描密钥类哨兵，跳过通用手机号/身份证扫描，避免破坏指标公式与规则文本。

---

### [P1-3] 加密快照 TTL 清理与源数据撤权失效缺少执行闭环

- **证据**：[TECH_DESIGN.md](../design/TECH_DESIGN.md#L96) 在 `context_snapshot` 定义了 `expires_at`（默认 14 天），但未登记定时清理入口，也未定义 `context_badcase` 引用已过期快照或源文档被删除/撤权时的处理行为。
- **触发条件与影响**：过期快照长期滞留导致敏感数据超期留存；若后续手动清理快照，则关联 Badcase 回放会触发 500 报错。
- **最小可执行修正**：
  1. 复用 Compass 同镜像 Scheduler 增加每日定时清理任务，仅删除 `expires_at < now()` 且未被 `context_badcase` 引用的快照；
  2. Badcase 回放与展示前先核对关联快照及 `source_access` 状态，快照已失效或源数据撤权时返回明确状态 `snapshot_expired_or_revoked`，不报 500。

---

### [P2-1] `COMPASS_CONTEXT_SECURITY_MODE=audit_only` 缺少环境与不可降级范围说明

- **证据**：[TECH_DESIGN.md](../design/TECH_DESIGN.md#L151) 提供 `enforce` / `audit_only` 开关。
- **建议**：在配置说明中明确该开关仅用于本地压测与规则调优，严禁通过租户界面动态关闭；同时明确多租户校验与 `source_access` 权限复核在任何模式下均为强制生效，不受 `audit_only` 影响。

---

## 三、待修正清单与保持现状项

| 类别 | 事项 | 处置动作 |
| --- | --- | --- |
| **本次必须修正（P0）** | P0-1 派生权限集合改为 Union（逻辑 AND） | 更新 `TECH_DESIGN.md` 与 `CODE_DESIGN.md` |
| **本次必须修正（P0）** | P0-2 不可信标签防逃逸 + 快照/回放接入 `source_access.py` 细粒度复核 | 更新 `TECH_DESIGN.md` 与 `CODE_DESIGN.md` |
| **本次建议同步补齐（P1）** | P1-1 快照查看审计 fail-closed；P1-2 精确脱敏规则；P1-3 Scheduler 过期清理与撤权态 | 更新 `TECH_DESIGN.md`、任务分解表及对应 AC |
| **保持现状（无需加码）** | Langfuse 零二开、OTel 闭集白名单零正文、默认确定性规则 PEP（不强绑商业云 Guardrail）、`web/main` 零迁移 | 符合 MVP 与成本边界，保持不变 |
