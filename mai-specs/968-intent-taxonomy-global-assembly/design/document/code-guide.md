## 1. 正交意图契约与只读字典

公开 DTO 彻底移除 `scene_id`、`category_id` 与 `deliverable` 双轨字段，收敛为唯一 `Intent` 与确定性派生的 `RoutingFeatures`：

{{code:schemas}}

`routing/dictionary.py` 负责随包固定属性加载、动态 SHA-256 版本计算、边界意图强制清域与工作区先验兜底：

{{code:dictionary}}

## 2. 两级正交分类器与选模逻辑

JEV 单次请求同时评估 `task_intent`、`primary_domain`、`secondary_domain` 与 `is_compound`；仅 `task_intent` 置信度不足时降级 Flash，次域不确定直接忽略：

{{code:intent_classifier}}

`routing/service.py` 清除全部旧冲突覆写补丁，选模逻辑收口为四条清晰分支：

{{code:routing_select}}

## 3. 动态工具四通道全局装配器

`ToolGovernance` 去除 `default_scenes` / `default_tasks`，引入 `default_intents`、`domains` 与 `compound_enabled`：

{{code:tool_governance}}

`tools/assembler.py` 清除全部 5 组 `SC-*` 常量，实现权限前置 + 四通道确定性组装（`always_on`、`user_selected`、`intent_routed`、`domain_routed`）：

{{code:tool_assembler}}

## 4. Context Runtime 三层装配与 PromptOps 校验

`SkillSpec` 支持按 `intents`、`domains` 与 `compound_also` 精确匹配：

{{code:skill_spec}}

`context_runtime/pipeline.py` 按本 Run 固定的 `domains.json` 注入业务域口径指引并写入回执：

{{code:pipeline_guidance}}

`promptops/structured.py` 校验 `tasks.json` 与 `domains.json`，锁定代码控制的固定属性，放行在线可编辑描述、示例与域指引：

{{code:promptops_validate}}

## 5. Web 语义同域加权与工作区先验透传

`apps/web/src/server/modules/semantic/runtime.ts` 以 `domains` 替换空转 `scene_hint`，在全量精确匹配保护跨业务域查询的前提下，对候选与首屏补齐执行同域加权：

{{code:semantic_boost}}

`apps/web/src/server/composition.ts` 通过 `runEnvironment` 将工作区默认业务域透传给 Agent：

{{code:web_composition}}

## 6. 双库迁移 DDL（`agent/main 0021` & `web/main 0011`）

`agent/main` Alembic 迁移（替换 `scene_id`/`task` 列、新增双索引、清理预上线过期 PromptOps 记录并重置执行策略指纹）：

{{code:agent_migration}}

`web/main` 原生 SQL 迁移（为 `web.workspace` 增加带白名单约束的 `default_domains` 列）：

{{code:web_migration}}
