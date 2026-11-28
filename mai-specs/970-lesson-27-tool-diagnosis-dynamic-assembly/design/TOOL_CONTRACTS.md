# 第 27 课｜全量 20 个工具极简 ACI 契约重构、三通道装载分类与 Schema 对照详单

> 状态：**设计示意（与 TECH_DESIGN.md / TECH_DESIGN.html 同源，严格对齐 [TOOL_DIAGNOSIS_REPORT.md](./TOOL_DIAGNOSIS_REPORT.md)）** | 关联主方案：[TECH_DESIGN.md](./TECH_DESIGN.md) | 需求来源：[spec/PRD.md](../spec/PRD.md)

[返回技术方案主文](./TECH_DESIGN.md) · [查看问题诊断报告](./TOOL_DIAGNOSIS_REPORT.md) · [返回需求简报](../BRIEF.md)

本文档依据 [`TOOL_DIAGNOSIS_REPORT.md`](./TOOL_DIAGNOSIS_REPORT.md) 的五条工程标尺，按**三大确定性装载通道（内建默认常驻 `always_on`、场景分流触发 `scene_routed`、前端用户手动选择 `user_selected`）**分组，完整呈现 Compass 仓库现有全部 **20 个工具声明**（19 个静态注册工具 + 子任务专用完成工具 `report_to_parent`）的重构理由与前后 JSON Schema 逐行对比（模型主动 `ToolSearch` 机制明确划分至下一课实现）。

## 三大工具装载通道全景矩阵

| 装载通道 | 触发机制 | 包含工具 / 资产范围 | 解决的核心问题 |
| --- | --- | --- | --- |
| **通道一：内建默认常驻（`always_on`）** | 在 Step 1 硬权限与能力开关允许前提下，所有主任务 `work` 阶段默认常驻（`delivery_only` 仅留 `final_answer`；子任务替换 `final_answer` 为 `report_to_parent` 并剔除 `schedule_write`；定时 Run 剔除 `schedule_write`；纯澄清/拒答 `SC-33/37/39` 直通 `answer_stream`） | ① **控制底座**：`final_answer`、`read_tool_result`（子任务：`report_to_parent`）<br>② **语义与查库底座**：`search_semantics`、`get_metric_definition`、`resolve_semantics`、`query_metric`、`database_query`<br>③ **资料核对底座**：`search_documents`、`read_document`<br>④ **定时任务底座**：`schedule_read`、`schedule_write`（强制 HITL 确认） | 将 DataAgent 高频查口径、查指标、查库、核制度与设定时全部纳入稳定底座（11 工具仅 `~8.0 KB`），消除分类器误判死胡同并最大化跨轮 Prefix Cache 稳定复用 |
| **通道二：场景分流触发（`scene_routed`）** | 联动第 21 课意图路由（`SC-01..40`），在 `identify -> work` 切换时按任务复杂度自动挂载深度分析增强包（`SC-34` 多轮承接自动补挂 `python_analysis`） | **深度分析增强包（`deep_analysis_pack`）**：`todo_write`（L2/L3 或 `plan=required`）、`python_analysis`（归因/预测/绘图/清洗/报告及 `SC-34`）、`sub_agent`（L3 多源诊断/全景报告/复合任务） | 在简单快查与口径解释中隐藏重型规划与子任务派发工具，杜绝模型过度规划或滥用子 Agent 拖慢响应，复杂分析时零配置自动配齐 |
| **通道三：前端用户手动选择（`user_selected`）** | 用户在 Chat 输入框工具栏「工具与扩展」浮层显式勾选（随 `RunConstraints.toolSelection` 提交并自动继承至长任务多轮；特定显式管理场景 `SC-29/30/36/38/40` 或已有未完结状态自动兜底） | ① **长任务模式**：`goal`<br>② **工作流编排**：`create_workflow`<br>③ **指标口径治理组**（仅维护者可见）：`discover_semantic_candidates`、`review_semantic_change`、`submit_semantic_change`<br>④ **个人工作流与外部扩展**：用户勾选的 `workflow_*` 与 `mcp_*` 连接（未勾选外部 MCP 跳过启动期探测） | 将重型跨轮状态机、工作流脚本沉淀、语义治理写操作及数量无上限的自定义 `workflow_*` / `mcp_*` 交由用户显式装载，严控单次 `<= 32` 槽位（未勾选项留作下一课 `ToolSearch` 延迟池） |

## 声明体积与结构精简实测总账

| 对比口径 | 暴露工具数 | 传输紧凑 JSON 字节 (`json_bytes`) | 保守估算 Token (`bytes / 4`) | `$defs` / 参数说明覆盖率 | 相对基线变化 |
| --- | --- | --- | --- | --- | --- |
| **Group A（当前平铺基线）** | 19 个静态工具全平铺 | `42,719 B` | `~10,680 tok` | `43` 个 `$defs` / 仅 `4.0%` 含说明 | 基线对照 |
| **Group B（极简契约 + 全平铺）** | 19 个静态工具全平铺 | `14,980 B` | `~3,745 tok` | **`0` 个 `$defs` / `100%` 含说明** | **`−65%`** |
| **Group C · `SC-01` / `SC-21` 常规问答（11 常驻底座）** | 11 个核心工具（隔离长任务/编排/口径治理与扩展） | `7,938 B` | `~1,985 tok` | **`0` 个 `$defs` / `100%` 含说明** | **`−81%`** |
| **Group C · `SC-03` 深度归因（11 常驻 + 3 深度分析）** | 14 个工具（加挂计划/Python/子任务） | `10,125 B` | `~2,532 tok` | **`0` 个 `$defs` / `100%` 含说明** | **`−76%`** |
| **子 Agent 专用底座（`report_to_parent`）** | 子任务替换 `final_answer` | `3,111 B` → `939 B` | `~778 tok` → `~235 tok` | `4 → 0` 个 `$defs` / `100%` 含说明 | **`−70%`** |

## 按三通道分组的逐工具 ACI 诊断与 Schema 前后对照（01–20）

### 通道一：内建默认常驻（always_on · 全场景底座）

#### 01 · `final_answer`（builtin · `core_control`）

- **装载通道**：**always_on（主 Agent 工作与收口阶段默认常驻）**
- **执行边界与声明开销**：read_only · 免审批 · 工程强制末位调用 · 流式输出后直接 stop ｜ 紧凑声明体积：`4,953 B (~1239 tok)` → `801 B (~201 tok)`，变化 **`−84%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 1/4】原设计要求模型把整篇回答与引用树（9 个 `$defs`、38 个属性、`4,953 B`）全部塞进工具入参 JSON 字符串，既消耗高额声明与转义 Token，又必须等工具 JSON 完整生成校验后才能一次性返回前端，首字延迟高且易触发长度截断。
  2. 【病灶 5】强迫模型手写 `coveredGoalIds`、`unresolved`、`completion`、`claims` 与 `blocks` 多重冗余记账，手抄 36 位 `evidenceId` 极易错配。
- **重构与装载方案**：
  1. 极致收敛为**唯一必填参数 `mode`（`"plain" | "evidence"`）+ 1 个可选扁平参数 `artifactFormat`（`"markdown" | "csv" | "dashboard"`）**：彻底解放最终回答形态，不受工具 JSON 结构束缚；按 `mode` 路由至两套独立 Answer Prompt（`prompts/answer/{plain,evidence}.md`），在 `answerProtocol == 3` 下先执行 `check_access` 权限复核，再向前端实时流式推送 SSE 数据包并在流结束后强制 `stop`。
  2. 循证模式（`mode="evidence"`）由服务端工程层对本轮已验证的检索/查询证据去重、排序、过滤并编号 `[1]..[N]`，直接注入 `{{ordered_evidences}}` 模板变量，正文输出 `^[1]^` 角标；流式结束后由工程层解析精确偏移与完整 `sources[]` 经 `publish_citations` 落盘（扩展 `citations.py` 与 `<CitedAnswer>` 双格式兼容 `^[1]^` 与存量 `[1]`），同时按 `artifactFormat` 复用 `build_artifact` 产出可下载报告/CSV/看板；同批混调其他工具时为抢跑 `final_answer` 返回标准 `role="tool"` 延期回执（禁止静默删除 `tool_call`）；纯澄清/拒答场景（`SC-33/37/39`）支持单跳直出流式文本。

```json [BEFORE] final_answer · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "final_answer",
    "description": "- 提交待校验的最终回答；单独调用。\n- plain 用于概念、澄清、操作说明和无资料状态；evidence 用于查询事实、公司资料及其解释建议。填写原问题已覆盖目标与未解决项。evidence 使用工具回执中的 evidenceId 和原文/结果格定位，claimId 可引用已支持结论。候选会经通用 Verify 再发布；反馈可能要求补查或修订。",
    "parameters": {
      "type": "object",
      "$defs": {
        "AnswerArtifact": {
          "additionalProperties": false,
          "properties": {
            "format": { "enum": ["markdown","csv","dashboard"], "title": "Format", "type": "string" },
            "title": { "maxLength": 160, "minLength": 1, "title": "Title", "type": "string" },
            "evidenceIds": {
              "items": { "type": "string" },
              "maxItems": 24,
              "title": "Evidenceids",
              "type": "array"
            }
          },
          "required": ["format", "title"],
          "title": "AnswerArtifact",
          "type": "object"
        },
        "Block": {
          "additionalProperties": false,
          "properties": {
            "type": {
              "enum": ["paragraph", "list", "comparison_table"],
              "title": "Type",
              "type": "string"
            },
            "claimIds": {
              "items": { "type": "string" },
              "minItems": 1,
              "title": "Claimids",
              "type": "array"
            },
            "text": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Text"
            }
          },
          "required": ["type", "claimIds"],
          "title": "Block",
          "type": "object"
        },
        "Calculation": {
          "additionalProperties": false,
          "properties": {
            "op": {
              "enum": ["sum", "difference", "ratio", "percent_change"],
              "title": "Op",
              "type": "string"
            },
            "inputs": {
              "items": { "$ref": "#/$defs/SourceRef" },
              "maxItems": 32,
              "minItems": 1,
              "title": "Inputs",
              "type": "array"
            },
            "unit": { "default": "", "title": "Unit", "type": "string" }
          },
          "required": ["op", "inputs"],
          "title": "Calculation",
          "type": "object"
        },
        "Claim": {
          "additionalProperties": false,
          "properties": {
            "id": { "minLength": 1, "title": "Id", "type": "string" },
            "kind": {
              "enum": ["fact", "interpretation", "recommendation"],
              "title": "Kind",
              "type": "string"
            },
            "text": { "minLength": 1, "title": "Text", "type": "string" },
            "refs": {
              "items": {
                "anyOf": [
                  { "$ref": "#/$defs/SourceRef" },
                  { "$ref": "#/$defs/ClaimRef" }
                ]
              },
              "minItems": 1,
              "title": "Refs",
              "type": "array"
            },
            "calculation": {
              "anyOf": [
                { "$ref": "#/$defs/Calculation" },
                { "type": "null" }
              ],
              "default": null
            }
          },
          "required": ["id", "kind", "text", "refs"],
          "title": "Claim",
          "type": "object"
        },
        "ClaimRef": {
          "additionalProperties": false,
          "properties": {
            "claimId": { "title": "Claimid", "type": "string" }
          },
          "required": ["claimId"],
          "title": "ClaimRef",
          "type": "object"
        },
        "EvidenceAnswer": {
          "additionalProperties": false,
          "properties": {
            "completion": { "enum": ["complete","partial"], "title": "Completion", "type": "string" },
            "coveredGoalIds": {
              "items": { "type": "string" },
              "title": "Coveredgoalids",
              "type": "array"
            },
            "unresolved": {
              "items": { "$ref": "#/$defs/Gap" },
              "title": "Unresolved",
              "type": "array"
            },
            "artifact": {
              "anyOf": [
                { "$ref": "#/$defs/AnswerArtifact" },
                { "type": "null" }
              ],
              "default": null
            },
            "mode": { "const": "evidence", "title": "Mode", "type": "string" },
            "claims": {
              "items": { "$ref": "#/$defs/Claim" },
              "minItems": 1,
              "title": "Claims",
              "type": "array"
            },
            "blocks": {
              "items": { "$ref": "#/$defs/Block" },
              "minItems": 1,
              "title": "Blocks",
              "type": "array"
            }
          },
          "required": ["completion", "coveredGoalIds", "unresolved", "mode", "claims", "blocks"],
          "title": "EvidenceAnswer",
          "type": "object"
        },
        "Gap": {
          "additionalProperties": false,
          "properties": {
            "goalId": { "minLength": 1, "title": "Goalid", "type": "string" },
            "reason": { "minLength": 1, "title": "Reason", "type": "string" },
            "nextStep": { "minLength": 1, "title": "Nextstep", "type": "string" }
          },
          "required": ["goalId", "reason", "nextStep"],
          "title": "Gap",
          "type": "object"
        },
        "PlainAnswer": {
          "additionalProperties": false,
          "properties": {
            "completion": { "enum": ["complete","partial"], "title": "Completion", "type": "string" },
            "coveredGoalIds": {
              "items": { "type": "string" },
              "title": "Coveredgoalids",
              "type": "array"
            },
            "unresolved": {
              "items": { "$ref": "#/$defs/Gap" },
              "title": "Unresolved",
              "type": "array"
            },
            "artifact": {
              "anyOf": [
                { "$ref": "#/$defs/AnswerArtifact" },
                { "type": "null" }
              ],
              "default": null
            },
            "mode": { "const": "plain", "title": "Mode", "type": "string" },
            "body": { "minLength": 1, "title": "Body", "type": "string" }
          },
          "required": ["completion", "coveredGoalIds", "unresolved", "mode", "body"],
          "title": "PlainAnswer",
          "type": "object"
        },
        "SourceRef": {
          "additionalProperties": false,
          "properties": {
            "evidenceId": { "title": "Evidenceid", "type": "string" },
            "paragraphId": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Paragraphid"
            },
            "start": {
              "anyOf": [
                { "minimum": 0, "type": "integer" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Start"
            },
            "end": {
              "anyOf": [
                { "minimum": 1, "type": "integer" },
                { "type": "null" }
              ],
              "default": null,
              "title": "End"
            },
            "row": {
              "anyOf": [
                { "minimum": 0, "type": "integer" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Row"
            },
            "column": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Column"
            },
            "artifactId": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Artifactid"
            }
          },
          "required": ["evidenceId"],
          "title": "SourceRef",
          "type": "object"
        }
      },
      "discriminator": {
        "mapping": { "evidence": "#/$defs/EvidenceAnswer", "plain": "#/$defs/PlainAnswer" },
        "propertyName": "mode"
      },
      "oneOf": [
        { "$ref": "#/$defs/PlainAnswer" },
        { "$ref": "#/$defs/EvidenceAnswer" }
      ]
    }
  },
  "examples": []
}
```

```json [AFTER] final_answer · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "final_answer",
    "description": "当信息已备齐或需向用户追问澄清时调用，触发最终流式回答生成并在输出完成后直接结束本轮运行（必须作为最后一步单独调用）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "mode": {
          "type": "string",
          "enum": ["plain", "evidence"],
          "description": "回答模式：plain 用于概念解释、操作说明或追问澄清；evidence 用于基于本轮检索/查询结果生成带 ^[1]^ 证据角标的循证回答。"
        },
        "artifactFormat": {
          "type": "string",
          "enum": ["markdown", "csv", "dashboard"],
          "description": "仅当用户明确要求导出可下载文件时填写（markdown 报告 / csv 明细表 / dashboard 看板）；常规问答省略。"
        }
      },
      "required": ["mode"]
    }
  },
  "examples": []
}
```

#### 02 · `read_tool_result`（builtin · `core_control`）

- **装载通道**：**always_on（主 Agent 与子 Agent 工作阶段默认常驻）**
- **执行边界与声明开销**：read_only · 免审批 · 子 Agent 可继承 ｜ 紧凑声明体积：`1,278 B (~320 tok)` → `645 B (~162 tok)`，变化 **`−50%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 3/4】顶层描述花大段解释返回字段 `complete`/`sourceComplete` 与权限内幕，而 4 个输入参数的 `description` 全部为空，且包裹了 3 处 6 行式 `anyOf null`。
  2. 【病灶 6】`cursor` 与 `offset`/`limit` 互斥藏在 Python 层，续页误带残留 `offset` 直接报黑盒 `invalid_parameters`。
- **重构与装载方案**：
  1. 作为任意大结果截断后的通用读取原语保持默认常驻；剥离全部 `anyOf null` 与 `title`，逐个参数用一句话写清定位约束。
  2. 服务端宽进严出：当 `cursor` 非空时确定性按游标续读并自动忽略残留 `offset`，零条件 Schema 根除互斥报错。

```json [BEFORE] read_tool_result · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "read_tool_result",
    "description": "- 读取本会话已经保存的工具结果原件，适用于核对预览之外的行、文本或字段。\n- 使用已有工具回执中的 ref。初次读取可设置 offset 与 limit：表格按行定位，文本按字符定位，offset 从 0 开始。\n- 续页时传入返回的 nextCursor 作为 cursor，并沿用同一 ref。cursor 与 offset、limit 两种定位方式互斥。\n- 结果返回实际范围、数据、nextCursor、原件读取进度 complete 和原查询完整性 sourceComplete。长行或大字段可能分片，下一游标会继续该片段。\n- 读取只取得已保存的内容。原查询失败、部分返回、引用失效或当前权限不足时，按返回状态处理。",
    "parameters": {
      "additionalProperties": false,
      "properties": {
        "ref": { "maxLength": 200, "minLength": 1, "title": "Ref", "type": "string" },
        "offset": {
          "anyOf": [
            { "minimum": 0, "type": "integer" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Offset"
        },
        "limit": {
          "anyOf": [
            { "minimum": 1, "type": "integer" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Limit"
        },
        "cursor": {
          "anyOf": [
            { "maxLength": 1024, "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Cursor"
        }
      },
      "required": ["ref"],
      "title": "ReadToolResultInput",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] read_tool_result · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "read_tool_result",
    "description": "当工具回执截断（truncated=true）且预览不足时，分页读取已保存的结果原件。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "ref": { "type": "string", "description": "上游回执返回的 resultId 或 ref。" },
        "cursor": { "type": "string", "description": "续页传上一页 nextCursor；首次省略。" },
        "offset": { "type": "integer", "description": "首次读取 0 起始行号或字符偏移；传 cursor 时忽略。" },
        "limit": { "type": "integer", "description": "单次条数上限（1..200，默认 50）。" }
      },
      "required": ["ref"]
    }
  },
  "examples": []
}
```

#### 03 · `report_to_parent`（subagent_only（子 Agent 专用底座） · `core_control`）

- **装载通道**：**always_on（仅子 Agent 运行时替代 final_answer 默认常驻）**
- **执行边界与声明开销**：read_only · 免审批 · 子 Agent 专属完成工具 ｜ 紧凑声明体积：`3,111 B (~778 tok)` → `939 B (~235 tok)`，变化 **`−70%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 1/3/4】子 Agent 仅有 3–8 步预算，却把消耗整步推理的 `kind="progress"` 心跳与 `kind="final"` 终止交付做成复合工具（4 个 `$defs`、16 个属性零说明、`3,111 B`），顶层塞入整张 Markdown 表格并对 `factKey` 强卡英文正则。
- **重构与装载方案**：
  1. 作为子 Agent 的唯一常驻完成出口：砍掉消耗步数的 `kind="progress"` 分支（中间进度由 Runtime 工具事件确定性推送），清零 `$defs` 与 `factKey` 正则，收敛为结束单次调用的 **4 个扁平参数**（`summary`、`evidenceIds`、`gaps`、`status`）。
  2. 采用**外简内全适配层**：服务端将扁平入参无损翻译为既有 `ChildReturn`（自动将 `summary + evidenceIds` 组装为 `findings`，缺省 `status` 按 `gaps` 自动推导），保证 `goal/handoff.py` 的证据归属校验与长任务 `Ledger` 归并 100% 兼容。

```json [BEFORE] report_to_parent · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "report_to_parent",
    "description": "**用途与调用规则**\n\n- 向主 Agent 汇报。用 kind 选择：\n\n| kind | 什么时候用 | 结果 |\n| --- | --- | --- |\n| progress | 完成一个有意义的中间步骤，主 Agent 和用户需要知道进展时 | 更新任务卡片摘要，继续执行 |\n| final | 任务完成、只能部分完成或被阻塞时 | 交回结果并结束任务 |\n\n- progress 只写一句进展，60 字以内，不写数值明细。\n- final 只调用一次，调用后任务结束。\n- final.status：completed 表示 expectations 都已回答；partial 表示部分回答，未回答的写进 gaps；blocked 表示需要主 Agent 或用户处理，原因写进 blockers。\n- findings 每条写一个可核对的结论，引用本任务查询得到的 evidenceIds；可比较的数值写 factKey、value、unit。\n- 没有证据支撑的推测写进 gaps，不写进 findings。",
    "parameters": {
      "type": "object",
      "$defs": {
        "ChildReturn": {
          "additionalProperties": false,
          "properties": {
            "status": { "enum": ["completed","partial","blocked"], "title": "Status", "type": "string" },
            "summary": { "maxLength": 1000, "minLength": 1, "title": "Summary", "type": "string" },
            "findings": {
              "items": { "$ref": "#/$defs/Finding" },
              "maxItems": 20,
              "title": "Findings",
              "type": "array"
            },
            "artifactRefs": {
              "items": { "type": "string" },
              "maxItems": 8,
              "title": "Artifactrefs",
              "type": "array"
            },
            "gaps": {
              "items": { "maxLength": 500, "type": "string" },
              "maxItems": 10,
              "title": "Gaps",
              "type": "array"
            },
            "blockers": {
              "items": { "maxLength": 500, "type": "string" },
              "maxItems": 5,
              "title": "Blockers",
              "type": "array"
            },
            "nextActions": {
              "items": { "maxLength": 500, "type": "string" },
              "maxItems": 5,
              "title": "Nextactions",
              "type": "array"
            }
          },
          "required": ["status", "summary"],
          "title": "ChildReturn",
          "type": "object"
        },
        "FinalReport": {
          "additionalProperties": false,
          "properties": {
            "kind": { "const": "final", "title": "Kind", "type": "string" },
            "result": { "$ref": "#/$defs/ChildReturn" }
          },
          "required": ["kind", "result"],
          "title": "FinalReport",
          "type": "object"
        },
        "Finding": {
          "additionalProperties": false,
          "properties": {
            "statement": { "maxLength": 500, "minLength": 1, "title": "Statement", "type": "string" },
            "factKey": {
              "anyOf": [
                { "pattern": "^[a-z0-9_.:-]{1,80}$", "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Factkey"
            },
            "value": {
              "anyOf": [
                { "type": "number" },
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Value"
            },
            "unit": {
              "anyOf": [
                { "maxLength": 20, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Unit"
            },
            "evidenceIds": {
              "items": { "type": "string" },
              "maxItems": 8,
              "title": "Evidenceids",
              "type": "array"
            }
          },
          "required": ["statement"],
          "title": "Finding",
          "type": "object"
        },
        "ProgressReport": {
          "additionalProperties": false,
          "properties": {
            "kind": { "const": "progress", "title": "Kind", "type": "string" },
            "summary": { "maxLength": 120, "minLength": 1, "title": "Summary", "type": "string" }
          },
          "required": ["kind", "summary"],
          "title": "ProgressReport",
          "type": "object"
        }
      },
      "discriminator": {
        "mapping": { "final": "#/$defs/FinalReport", "progress": "#/$defs/ProgressReport" },
        "propertyName": "kind"
      },
      "oneOf": [
        { "$ref": "#/$defs/ProgressReport" },
        { "$ref": "#/$defs/FinalReport" }
      ],
      "title": "ReportToParentInput"
    }
  }
}
```

```json [AFTER] report_to_parent · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "report_to_parent",
    "description": "子任务调查结束时向主 Agent 交回最终结论并终止当前子任务（仅在完成或确认受阻时调用一次）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "summary": { "type": "string", "description": "面向主 Agent 的结构化调查结论与关键数值摘要（Markdown）。" },
        "evidenceIds": {
          "type": "array",
          "items": { "type": "string" },
          "description": "支撑上述结论的本子任务工具回执 evidenceId 列表。"
        },
        "gaps": {
          "type": "array",
          "items": { "type": "string" },
          "description": "受权限或数据限制未能核实的缺口说明；全部完成时省略。"
        },
        "status": {
          "type": "string",
          "enum": ["completed", "partial", "blocked"],
          "description": "子任务完成态：全部完成传 completed，存在缺口传 partial，权限或数据受阻传 blocked；省略时按 gaps 自动推导。"
        }
      },
      "required": ["summary"]
    }
  },
  "examples": []
}
```

### 通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）

#### 04 · `search_semantics`（business · `semantic_read`）

- **装载通道**：**always_on（启用语义层时所有主/子任务工作阶段默认常驻）**
- **执行边界与声明开销**：read_only · 免审批 · 子 Agent 可继承 ｜ 紧凑声明体积：`1,005 B (~252 tok)` → `636 B (~159 tok)`，变化 **`−37%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 2/4】暴露组织权限范围 UUID `scope_filter`、冗余 `domain` 及含 `{const: 0}` 内部补丁的 `cursor`；6 个参数 `description` 全空。
  2. 【病灶 7】未声明预注入上下文短路规则，模型即使已在 `publishedSemanticRegistry` 看到目标指标仍惯性重复搜索。
- **重构与装载方案**：
  1. 删除确定性范围字段 `scope_filter`、合并 `domain` 入 `query`、固化单页大小，收敛为 **3 个简单参数**并补齐参数约束说明；顶层置顶预注入命中禁调规则。

```json [BEFORE] search_semantics · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "search_semantics",
    "description": "**用途与调用规则**\n\n- 搜索当前发布及授权范围内的术语、指标和实体，按名称、别名和业务域检索。\n- 下一页沿用本次检索条件及返回的 cursor。\n- 更改条件时从第一页开始。",
    "parameters": {
      "additionalProperties": false,
      "properties": {
        "query": { "maxLength": 256, "title": "Query", "type": "string" },
        "kinds": {
          "anyOf": [
            {
              "items": { "type": "string" },
              "type": "array"
            },
            { "type": "null" }
          ],
          "default": null,
          "title": "Kinds"
        },
        "domain": {
          "anyOf": [
            { "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Domain"
        },
        "scope_filter": {
          "anyOf": [
            { "format": "uuid", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Scope Filter"
        },
        "cursor": {
          "anyOf": [
            { "format": "uuid", "type": "string" },
            { "const": 0, "type": "integer" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Cursor"
        },
        "limit": { "default": 12, "maximum": 50, "minimum": 1, "title": "Limit", "type": "integer" }
      },
      "required": ["query"],
      "title": "Input",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] search_semantics · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "search_semantics",
    "description": "搜索已发布的指标、维度、实体与术语（预注入 publishedSemanticRegistry 已含目标指标时禁调，直接用 resolve_semantics 或 query_metric）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "query": { "type": "string", "description": "名称、别名或业务域关键词。" },
        "kind": {
          "type": "string",
          "enum": ["metric", "dimension", "entity", "term"],
          "description": "资产类别；省略查全部。"
        },
        "cursor": { "type": "string", "description": "翻页传上一页 next_cursor；首页省略。" }
      },
      "required": ["query"]
    }
  },
  "examples": []
}
```

#### 05 · `get_metric_definition`（business · `semantic_read`）

- **装载通道**：**always_on（启用语义层时默认常驻，SC-21 口径解释主力工具）**
- **执行边界与声明开销**：read_only · 免审批 · 子 Agent 可继承 ｜ 紧凑声明体积：`488 B (~122 tok)` → `378 B (~95 tok)`，变化 **`−23%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 2/3/7】暴露日常问答无需模型指定的 `version_id`；顶层仅 1 句笼统说明，未与 `resolve_semantics` 划清触发边界，导致常规查数多跑一轮定义读取。
- **重构与装载方案**：
  1. 删除 `version_id`（服务端确定性读取当前生效发布版），仅保留**唯一必填参数 `asset_id`**；顶层明确仅用于纯口径问答或核查例外规则。

```json [BEFORE] get_metric_definition · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "get_metric_definition",
    "description": "**用途与调用规则**\n\n- 读取已发布定义、统计粒度、期间、单位、适用维度与可见关系。",
    "parameters": {
      "additionalProperties": false,
      "properties": {
        "asset_id": { "format": "uuid", "title": "Asset Id", "type": "string" },
        "version_id": {
          "anyOf": [
            { "format": "uuid", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Version Id"
        }
      },
      "required": ["asset_id"],
      "title": "Input",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] get_metric_definition · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "get_metric_definition",
    "description": "读取指标计算公式与排除规则（仅口径解释或核对例外时用；常规查数直接调 resolve_semantics）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "asset_id": { "type": "string", "description": "已发布指标资产 ID。" }
      },
      "required": ["asset_id"]
    }
  },
  "examples": []
}
```

#### 06 · `resolve_semantics`（business · `semantic_query`）

- **装载通道**：**always_on（启用语义层时默认常驻；SC-21 纯定义场景由 SOP 约束免多余取数）**
- **执行边界与声明开销**：read_only · 歧义时触发口径选择卡 · 子 Agent 可继承（无澄清时） ｜ 紧凑声明体积：`2,310 B (~578 tok)` → `1,034 B (~259 tok)`，变化 **`−55%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 2/5】嵌套 `$defs.Parameters` 暴露了服务端自动填写的 `as_of`，且顶层 `dimensions` 与嵌套 `group_by`、`historical_as_of` 与 `as_of` 双重重复（`2,310 B`，10 个属性仅 1 个英文说明）。
- **重构与装载方案**：
  1. 删除 `$defs.Parameters`、删除确定性 `as_of` 与重复 `dimensions`，拍平为直观顶层参数并逐项写明日期与筛选格式；明确预注入已有 `query_ref` 时直接跳至 `query_metric`。

```json [BEFORE] resolve_semantics · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "resolve_semantics",
    "description": "**用途与调用规则**\n\n- 解析本次提问指向的正式指标。\n- 根据前文或本次要求确定指标后可直接传可见 metric_id。\n- 仍有歧义时省略 ID，让用户选择或补充计算方法。\n- 无绑定时返回可见定义，按当前授权继续分析。\n- 用签发的 query_ref 计算。\n- 正式取数需在 parameters 中提交 start 和 end，并指定需要的分组及过滤。\n- 服务端按 Run 的受理时刻填写 as_of，仅在用户明确指定历史数据截止时提交 historical_as_of。\n- query_metric 复用返回的规范化参数。\n- previous_equal_window 提供紧邻本期且天数相同的上一业务日期区间，使用相同分组、筛选和截止时刻。\n- 若收到 invalid_parameters，请按 field、filter_types 和 group_by_options 修正。\n- 筛选值使用绑定要求的 JSON 类型，编号型维度填整数。",
    "parameters": {
      "$defs": {
        "Parameters": {
          "additionalProperties": false,
          "properties": {
            "start": { "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "title": "Start", "type": "string" },
            "end": { "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "title": "End", "type": "string" },
            "as_of": {
              "anyOf": [
                { "maxLength": 40, "minLength": 16, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "As Of"
            },
            "group_by": {
              "anyOf": [
                { "maxLength": 120, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Group By"
            },
            "filters": {
              "additionalProperties": {
                "anyOf": [
                  { "type": "string" },
                  { "type": "integer" },
                  { "type": "boolean" }
                ]
              },
              "maxProperties": 32,
              "title": "Filters",
              "type": "object"
            }
          },
          "required": ["start", "end"],
          "title": "Parameters",
          "type": "object"
        }
      },
      "additionalProperties": false,
      "properties": {
        "mention": { "maxLength": 160, "minLength": 1, "title": "Mention", "type": "string" },
        "metric_id": {
          "anyOf": [
            { "format": "uuid", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Metric Id"
        },
        "dimensions": {
          "items": { "type": "string" },
          "maxItems": 8,
          "title": "Dimensions",
          "type": "array"
        },
        "parameters": {
          "anyOf": [
            { "$ref": "#/$defs/Parameters" },
            { "type": "null" }
          ],
          "default": null
        },
        "historical_as_of": {
          "anyOf": [
            { "maxLength": 40, "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "description": "User-specified historical data cutoff; omit to use the Run start.",
          "title": "Historical As Of"
        }
      },
      "required": ["mention"],
      "title": "Input",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] resolve_semantics · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "resolve_semantics",
    "description": "将用户提问的指标、期间与维度条件绑定为受控查询引用 query_ref。若预注入 authorized_resolution 已含匹配 query_ref，禁止重复调用，直接调 query_metric。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "mention": { "type": "string", "description": "用户问题中的原始指标名称（如 '净销售额'）。" },
        "metric_id": { "type": "string", "description": "已确认的正式指标资产 ID；存在同名多口径歧义时省略以触发用户选择卡。" },
        "start": { "type": "string", "description": "统计起始日期（YYYY-MM-DD，含当日）。" },
        "end": { "type": "string", "description": "统计结束日期（YYYY-MM-DD，含当日）。" },
        "group_by": { "type": "string", "description": "分组拆解维度编码（如 region、channel）；总体汇总省略。" },
        "filters": { "type": "object", "description": "维度等值筛选键值对（如 {\"channel\": \"online\"}）。" }
      },
      "required": ["mention"]
    }
  },
  "examples": []
}
```

#### 07 · `query_metric`（business · `semantic_query`）

- **装载通道**：**always_on（启用语义层时默认常驻，正式指标取数首选）**
- **执行边界与声明开销**：read_only · 免审批 · 子 Agent 可继承 ｜ 紧凑声明体积：`2,507 B (~627 tok)` → `811 B (~203 tok)`，变化 **`−68%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 1/3/5】把 `query`/`compare`/`drilldown` 做成复合模式，同时挂着与 `query_ref` 重复的 `parameters`、`as_of` 与复杂联合 `parent_group`（`2,507 B`）；顶层描述用近半篇幅解释返回字段正负号和加总算法。
- **重构与装载方案**：
  1. 彻底去复合化：删除 `operation` 枚举、删除重复 `parameters`/`$defs`、清空顶层返回值算法长文，收敛为 **3 个正交参数**（`query_ref`、`compare_to`、`drilldown_from`）——只传 `query_ref` 即查数，传 `compare_to` 即对比，传 `drilldown_from` 即下钻。

```json [BEFORE] query_metric · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "query_metric",
    "description": "**用途与调用规则**\n\n- 通过已发布执行绑定按期间、维度和过滤查询正式指标。\n- 比较时传两个可比期间。\n- 返回受信结果引用、口径、单位与数据截止。\n- 只传 query_ref 即可执行已解析的查询。\n- 对比时传 operation=\"compare\" 和明确的 comparison。\n- 用户要求紧邻的前一等长期间时可采用解析结果中的 previous_equal_window。\n- 新的本期或维度需要先重新解析。\n- 对比结果的 current_minus_comparison 表示本期减对照期，由执行器对齐两期分组，负值表示下降。\n- 完整可加金额中仅一期出现的分组按另一侧零参与差额；缺报或没有可比值时差额为空。\n- period_totals 给出完整可加分组的各期合计。\n- 下钻时，把子分组和父级筛选写进解析参数，再将相同筛选作为 parent_group。\n- 从全体授权数据下钻时填写 parent_group=\"total\"。",
    "parameters": {
      "$defs": {
        "Parameters": {
          "additionalProperties": false,
          "properties": {
            "start": { "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "title": "Start", "type": "string" },
            "end": { "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "title": "End", "type": "string" },
            "as_of": {
              "anyOf": [
                { "maxLength": 40, "minLength": 16, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "As Of"
            },
            "group_by": {
              "anyOf": [
                { "maxLength": 120, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Group By"
            },
            "filters": {
              "additionalProperties": {
                "anyOf": [
                  { "type": "string" },
                  { "type": "integer" },
                  { "type": "boolean" }
                ]
              },
              "maxProperties": 32,
              "title": "Filters",
              "type": "object"
            }
          },
          "required": ["start", "end"],
          "title": "Parameters",
          "type": "object"
        }
      },
      "additionalProperties": false,
      "properties": {
        "query_ref": { "format": "uuid", "title": "Query Ref", "type": "string" },
        "operation": {
          "default": "query",
          "enum": ["query", "compare", "drilldown"],
          "title": "Operation",
          "type": "string"
        },
        "parameters": {
          "anyOf": [
            { "$ref": "#/$defs/Parameters" },
            { "type": "null" }
          ],
          "default": null
        },
        "comparison": {
          "anyOf": [
            { "$ref": "#/$defs/Parameters" },
            { "type": "null" }
          ],
          "default": null,
          "description": "比较时填写用户所指的对照期。"
        },
        "parent_group": {
          "anyOf": [
            {
              "additionalProperties": {
                "anyOf": [
                  { "type": "string" },
                  { "type": "integer" },
                  { "type": "boolean" }
                ]
              },
              "type": "object"
            },
            { "const": "total", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "description": "下钻起点。填写 total 或已绑定的父级筛选条件。",
          "title": "Parent Group"
        }
      },
      "required": ["query_ref"],
      "title": "Input",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] query_metric · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "query_metric",
    "description": "正式指标取数首选工具：凭 query_ref 执行受控指标查询、同环比对比或维度下钻（有已发布指标绑定时必须优先使用，禁止手写 SQL 替代）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "query_ref": { "type": "string", "description": "预注入上下文或 resolve_semantics 签发的查询引用 ID。" },
        "compare_to": {
          "type": "string",
          "description": "期间对比配置：传 'previous' 自动对比紧邻等长上期，或传 'YYYY-MM-DD..YYYY-MM-DD' 指定对照区间；普通查询省略。"
        },
        "drilldown_from": { "type": "object", "description": "维度下钻的父级筛选键值（从总体汇总下钻传空对象 {}）；不下钻时省略。" }
      },
      "required": ["query_ref"]
    }
  },
  "examples": []
}
```

#### 08 · `database_query`（builtin · `data_explore`）

- **装载通道**：**always_on（存在授权数据库连接时所有工作阶段默认常驻）**
- **执行边界与声明开销**：read_only · 免审批（pglast 只读 AST + 确定性连接路由） · 子 Agent 可继承 ｜ 紧凑声明体积：`2,391 B (~598 tok)` → `648 B (~162 tok)`，变化 **`−73%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 1】人为封禁 `information_schema` 后包装出 `list_connections`/`list_tables`/`describe_table`/`query` 四个复合子命令（4 个 `$defs`、12 个属性、`2,391 B`、参数说明全空），把 1 条元数据 SQL 能解决的事拆成 4 轮对话。
  2. 【病灶 2/6】把确定性数据库连接路由 `connectionId` 甩给模型并诱导先跑无意义的 `list_connections`；未登记函数、越权表与列名错误全部吞成裸词 `invalid_parameters` / `database_query_failed`。
- **重构与装载方案**：
  1. 极致单一原语化：删除全部 `op` 子命令、`schema`、`table`、`offset` 与 `connectionId`，**仅保留唯一 1 个必填参数 `sql`**；后端放开受权限过滤的只读 `information_schema.tables` / `columns` 查询（多连接工作区并行聚合全部授权连接的元数据并附带连接标识，单表业务查询按 `schema.table` 确定性路由连接，冲突时返回结构化引导）。
  2. 补齐可靠性与兼容守卫：① 纯 `information_schema` 元数据查询显式标记 `source_ref.kind = "metadata"`（避免误判 `transient` 阻塞附件保存）；② SQL 错列（`42703`）通过 `SAVEPOINT` 回滚后再查可见列候选（杜绝 PostgreSQL `25P02` 事务中止）；③ 回执双写 `resultId` 与 `sourceQueryId` 并更新 `fingerprint()` 兼容单参数 `sql`，保持 `python_analysis` 血缘与长任务查询复用；④ 内部适配层同步兼容旧检查点 `op` 回放。

```json [BEFORE] database_query · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "database_query",
    "description": "- 列出可查询的表、查看字段，或执行一条只读 PostgreSQL SELECT。\n- 先用 list_connections 发现连接，多个连接时指定 connectionId。SQL 使用 schema.table。\n- 查询按当前授权、超时和执行资源边界运行。小结果直接返回，大结果返回实际规模、字段、少量样本、完整性和原件引用。\n- 需要核对预览之外的数据时，使用 read_tool_result 读取已有原件。返回量与完整性以本次工具结果为准。",
    "parameters": {
      "type": "object",
      "$defs": {
        "DescribeTable": {
          "additionalProperties": false,
          "properties": {
            "connectionId": {
              "anyOf": [
                { "maxLength": 36, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Connectionid"
            },
            "op": { "const": "describe_table", "title": "Op", "type": "string" },
            "schema": { "maxLength": 63, "minLength": 1, "title": "Schema", "type": "string" },
            "table": { "maxLength": 63, "minLength": 1, "title": "Table", "type": "string" },
            "offset": {
              "default": 0,
              "maximum": 10000,
              "minimum": 0,
              "title": "Offset",
              "type": "integer"
            }
          },
          "required": ["op", "schema", "table"],
          "title": "DescribeTable",
          "type": "object"
        },
        "ListConnections": {
          "additionalProperties": false,
          "properties": {
            "op": { "const": "list_connections", "title": "Op", "type": "string" }
          },
          "required": ["op"],
          "title": "ListConnections",
          "type": "object"
        },
        "ListTables": {
          "additionalProperties": false,
          "properties": {
            "connectionId": {
              "anyOf": [
                { "maxLength": 36, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Connectionid"
            },
            "op": { "const": "list_tables", "title": "Op", "type": "string" },
            "offset": {
              "default": 0,
              "maximum": 10000,
              "minimum": 0,
              "title": "Offset",
              "type": "integer"
            }
          },
          "required": ["op"],
          "title": "ListTables",
          "type": "object"
        },
        "ReadSql": {
          "additionalProperties": false,
          "properties": {
            "connectionId": {
              "anyOf": [
                { "maxLength": 36, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Connectionid"
            },
            "op": { "const": "query", "title": "Op", "type": "string" },
            "sql": { "maxLength": 8192, "minLength": 1, "title": "Sql", "type": "string" }
          },
          "required": ["op", "sql"],
          "title": "ReadSql",
          "type": "object"
        }
      },
      "discriminator": {
        "mapping": {
          "describe_table": "#/$defs/DescribeTable",
          "list_connections": "#/$defs/ListConnections",
          "list_tables": "#/$defs/ListTables",
          "query": "#/$defs/ReadSql"
        },
        "propertyName": "op"
      },
      "oneOf": [
        { "$ref": "#/$defs/ListConnections" },
        { "$ref": "#/$defs/ListTables" },
        { "$ref": "#/$defs/DescribeTable" },
        { "$ref": "#/$defs/ReadSql" }
      ]
    }
  },
  "examples": []
}
```

```json [AFTER] database_query · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "database_query",
    "description": "执行只读 PostgreSQL 查询（仅在无 query_metric 正式指标绑定、排查底层异常明细流水或自定义临时口径时使用）。最佳实践：先查 information_schema.tables / information_schema.columns 核实可见表名与字段，再查业务表（所有业务表须带 schema.table 前缀）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "sql": {
          "type": "string",
          "description": "单条只读 PostgreSQL SELECT 语句（支持查表清单、查字段结构及带 LIMIT/聚合的业务明细查询）。"
        }
      },
      "required": ["sql"]
    }
  },
  "examples": []
}
```

#### 09 · `search_documents`（business · `knowledge_doc`）

- **装载通道**：**always_on（启用文档检索时默认常驻，随时支持制度与历史复盘核对）**
- **执行边界与声明开销**：read_only · 免审批 · 子 Agent 可继承 ｜ 紧凑声明体积：`1,219 B (~305 tok)` → `824 B (~206 tok)`，变化 **`−32%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 3/6】拆分布尔开关 `historical` 与 `businessDate` 制造出“`historical=true` 必传 `businessDate`”的隐藏条件；`entity` 与 `query` 重叠；顶层混入封版过滤内幕而 5 个参数全无说明。
- **重构与装载方案**：
  1. 删除 `historical` 与 `entity`，归并为 **3 个自明参数**（`query`、`kind`、`businessDate`），在 `businessDate` 参数说明中直接写明“传值即查该历史日生效版本，省略查现行版本”，彻底清零条件逻辑。

```json [BEFORE] search_documents · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "search_documents",
    "description": "**用途与调用规则**\n\n- 检索当前工作区已发布、当前可阅的制度知识、历史产物或外部资料。\n- query 可用作者原词、文号、实体和同义词，kind 分别为 knowledge、history、external。\n- 查过去的活动或政策时，businessDate 填业务发生日。\n- 系统另按资料封版时点过滤可见版本。\n- historical=true 时必须给出 businessDate；不确定资料类别时可省略 kind。\n- 最多返回前三段线索与 ref，核实条款或报价前须读取原段。",
    "parameters": {
      "additionalProperties": false,
      "properties": {
        "query": { "maxLength": 256, "minLength": 1, "title": "Query", "type": "string" },
        "kind": {
          "anyOf": [
            { "enum": ["knowledge","history","external"], "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Kind"
        },
        "entity": {
          "anyOf": [
            { "maxLength": 240, "minLength": 1, "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Entity"
        },
        "businessDate": {
          "anyOf": [
            { "format": "date", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Businessdate"
        },
        "historical": { "default": false, "title": "Historical", "type": "boolean" }
      },
      "required": ["query"],
      "title": "Input",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] search_documents · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "search_documents",
    "description": "检索工作区经营制度、历史复盘报告或外部参考资料，返回最相关段落摘要与 ref（正式引用条款或阈值前须调 read_document 核实原文）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "query": { "type": "string", "description": "检索关键词（支持政策名、活动代号、实体名或业务术语）。" },
        "kind": {
          "type": "string",
          "enum": ["knowledge", "history", "external"],
          "description": "资料类型：制度规范(knowledge)、历史复盘(history)、外部资料(external)；不确定时省略。"
        },
        "businessDate": { "type": "string", "description": "排查历史活动或旧版制度时填业务发生日（YYYY-MM-DD）；查现行有效版本时省略。" }
      },
      "required": ["query"]
    }
  },
  "examples": []
}
```

#### 10 · `read_document`（business · `knowledge_doc`）

- **装载通道**：**always_on（与 search_documents 成对默认常驻）**
- **执行边界与声明开销**：read_only · 免审批 · 子 Agent 可继承 ｜ 紧凑声明体积：`589 B (~148 tok)` → `539 B (~135 tok)`，变化 **`−8%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 3/4】顶层混入权限复核内部声明，2 个参数无 `description` 且携带冗余 `title`。
- **重构与装载方案**：
  1. 删去后端权限复核废话与 `title`，为 `ref` 与 `offset` 补齐精准参数约束说明，未知 `ref` 运行时引导先调 `search_documents`。

```json [BEFORE] read_document · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "read_document",
    "description": "**用途与调用规则**\n\n- 用本 Run 此前检索或阅读取得的 ref 获取原文。\n- offset 为 -1、0 或 1，可读取同一版本的相邻段。\n- 核实适用条款、表头与单位时按需打开邻段。\n- 资料权限和有效期在读取时复核。",
    "parameters": {
      "additionalProperties": false,
      "properties": {
        "ref": { "format": "uuid", "title": "Ref", "type": "string" },
        "offset": { "default": 0, "maximum": 1, "minimum": -1, "title": "Offset", "type": "integer" }
      },
      "required": ["ref"],
      "title": "Input",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] read_document · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "read_document",
    "description": "读取本轮 search_documents 命中的文档段落全文或展开相邻上下文段落，用于正式引用前核实完整条款、表头与单位。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "ref": { "type": "string", "description": "本轮 search_documents 返回的段落引用 ID。" },
        "offset": {
          "type": "integer",
          "enum": [-1, 0, 1],
          "description": "段落偏移：0 当前段（默认），-1 上一段，1 下一段。"
        }
      },
      "required": ["ref"]
    }
  },
  "examples": []
}
```

#### 11 · `schedule_read`（builtin · `automation`）

- **装载通道**：**always_on（启用定时任务服务时工作阶段默认常驻）**
- **执行边界与声明开销**：read_only · 免审批 · 子 Agent 可继承 ｜ 紧凑声明体积：`1,779 B (~445 tok)` → `575 B (~144 tok)`，变化 **`−68%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 1/4】拆成 `list`/`get`/`history` 三个子命令 `$defs`（9 个属性零说明、`1,779 B`），且 `list` 代码人为执行 `item.pop("question")` 删掉分析问题，逼模型查完列表再调一次 `get`。
- **重构与装载方案**：
  1. 归入 `always_on` 定时核心底座，方便用户在任意分析会话中随时查看与追问定时计划；取消 `operation` 子命令分支，收敛为 **2 个可选筛选参数**，一次调用返回完整配置与近期记录。

```json [BEFORE] schedule_read · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "schedule_read",
    "description": "**用途与调用规则**\n\n- 列出、搜索当前用户的定时任务，或按 ID 查看详情及执行记录。\n- 按名称修改或删除前先查询，遇到同名任务请用户选择。",
    "parameters": {
      "type": "object",
      "$defs": {
        "ScheduleGetArgs": {
          "additionalProperties": false,
          "properties": {
            "operation": { "const": "get", "title": "Operation", "type": "string" },
            "scheduleId": { "format": "uuid", "title": "Scheduleid", "type": "string" }
          },
          "required": ["operation", "scheduleId"],
          "title": "ScheduleGetArgs",
          "type": "object"
        },
        "ScheduleHistoryArgs": {
          "additionalProperties": false,
          "properties": {
            "operation": { "const": "history", "title": "Operation", "type": "string" },
            "scheduleId": { "format": "uuid", "title": "Scheduleid", "type": "string" },
            "cursor": {
              "anyOf": [
                { "format": "uuid", "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Cursor"
            }
          },
          "required": ["operation", "scheduleId"],
          "title": "ScheduleHistoryArgs",
          "type": "object"
        },
        "ScheduleListArgs": {
          "additionalProperties": false,
          "properties": {
            "operation": { "const": "list", "title": "Operation", "type": "string" },
            "q": {
              "anyOf": [
                { "maxLength": 120, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Q"
            },
            "status": {
              "anyOf": [
                { "enum": ["enabled","disabled","deleted"], "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Status"
            },
            "cursor": {
              "anyOf": [
                { "format": "uuid", "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Cursor"
            }
          },
          "required": ["operation"],
          "title": "ScheduleListArgs",
          "type": "object"
        }
      },
      "discriminator": {
        "mapping": {
          "get": "#/$defs/ScheduleGetArgs",
          "history": "#/$defs/ScheduleHistoryArgs",
          "list": "#/$defs/ScheduleListArgs"
        },
        "propertyName": "operation"
      },
      "oneOf": [
        { "$ref": "#/$defs/ScheduleListArgs" },
        { "$ref": "#/$defs/ScheduleGetArgs" },
        { "$ref": "#/$defs/ScheduleHistoryArgs" }
      ]
    }
  },
  "examples": []
}
```

```json [AFTER] schedule_read · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "schedule_read",
    "description": "查询当前用户的定时分析任务（一次返回完整配置、分析问题与近期触发记录）。调用 schedule_write 修改或删除前须先调用本工具确认目标 scheduleId。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "query": { "type": "string", "description": "按任务名称或分析问题关键词筛选；省略则列出全部未删除任务。" },
        "scheduleId": { "type": "string", "description": "按任务 ID 精确查看单个任务及执行历史。" }
      }
    }
  },
  "examples": []
}
```

#### 12 · `schedule_write`（builtin · `automation`）

- **装载通道**：**always_on（启用定时服务时主任务交互会话默认常驻；定时 Run 与子 Agent 由 Step 1 硬过滤永久剔除）**
- **执行边界与声明开销**：external_write · 强制所有者人工确认（HITL） · 定时 Run 与子 Agent 永久禁用 ｜ 紧凑声明体积：`4,821 B (~1206 tok)` → `1,047 B (~262 tok)`，变化 **`−78%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 1/2/4】包含 8 个 `$defs` 共 35 个属性（`4,821 B`，全部零说明）：`Create` 与 `Patch` 全字段镜像重复，且强制模型生成内部模型路由 `MainModelSelection`、`timezone`、`RunConstraints`、`WorkflowAssetRef` 与 `expectedRevision`。
- **重构与装载方案**：
  1. 归入主任务交互会话 `always_on` 底座（用户可在查数后直接自然语言设定时计划，零手动开关摩擦），通过三层安全防线护航：① `ToolPolicy(external_write, approval=required)` 强制弹出人工确认卡；② `ToolGovernance(subagent_allowed=False, scheduler_allowed=False)` 在 Step 1 对子 Agent 与无人值守定时 Run 永久剔除；③ 顶层 `description` 明确仅用户明确请求时调用。
  2. 采用**外简内全适配层与并发锁保护**：清零环境配置与镜像 `$defs` 合并为 **5 个直观参数**；服务端在 `prepare_approval` 阶段确定性继承工作区时区与当前会话模型，优先绑定本轮 `schedule_read` 已读 `revision` 冻结入 `PreparedApproval`，恢复执行时严格校验乐观锁防止并发修改静默覆盖。

```json [BEFORE] schedule_write · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "schedule_write",
    "description": "**用途与调用规则**\n\n- 创建、修改、启停或软删除定时任务，执行前由任务所有者确认。\n- 单独调用。\n- 修改与删除先查询并提供 ID 和当前版本；时间或目标含糊时先询问。\n- 创建需要名称、分析问题、模型选择、五段 Cron 和 IANA 时区。\n- 启停和删除不会取消已经受理的运行。",
    "parameters": {
      "type": "object",
      "$defs": {
        "MainModelSelection": {
          "additionalProperties": false,
          "properties": {
            "mode": { "enum": ["fixed","auto"], "title": "Mode", "type": "string" },
            "provider_id": {
              "anyOf": [
                { "format": "uuid", "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Provider Id"
            }
          },
          "required": ["mode"],
          "title": "MainModelSelection",
          "type": "object"
        },
        "RunConstraints": {
          "additionalProperties": false,
          "properties": {
            "plan": {
              "anyOf": [
                { "const": "required", "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Plan"
            },
            "maxSteps": {
              "anyOf": [
                { "maximum": 100, "minimum": 1, "type": "integer" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Maxsteps"
            },
            "timeoutSeconds": {
              "anyOf": [
                { "maximum": 600, "minimum": 1, "type": "integer" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Timeoutseconds"
            }
          },
          "title": "RunConstraints",
          "type": "object"
        },
        "RunPeriod": {
          "additionalProperties": false,
          "properties": {
            "start": { "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "title": "Start", "type": "string" },
            "end": { "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "title": "End", "type": "string" }
          },
          "required": ["start", "end"],
          "title": "RunPeriod",
          "type": "object"
        },
        "ScheduleCreateArgs": {
          "additionalProperties": false,
          "properties": {
            "question": { "maxLength": 8000, "minLength": 1, "title": "Question", "type": "string" },
            "selection": { "$ref": "#/$defs/MainModelSelection" },
            "workflowAssetRef": {
              "anyOf": [
                { "$ref": "#/$defs/WorkflowAssetRef" },
                { "type": "null" }
              ],
              "default": null
            },
            "constraints": {
              "anyOf": [
                { "$ref": "#/$defs/RunConstraints" },
                { "type": "null" }
              ],
              "default": null
            },
            "name": { "maxLength": 120, "minLength": 1, "title": "Name", "type": "string" },
            "cron": { "maxLength": 120, "minLength": 1, "title": "Cron", "type": "string" },
            "timezone": { "maxLength": 128, "minLength": 1, "title": "Timezone", "type": "string" },
            "enabled": { "default": true, "title": "Enabled", "type": "boolean" },
            "operation": { "const": "create", "title": "Operation", "type": "string" }
          },
          "required": ["question", "selection", "name", "cron", "timezone", "operation"],
          "title": "ScheduleCreateArgs",
          "type": "object"
        },
        "ScheduleDeleteArgs": {
          "additionalProperties": false,
          "properties": {
            "operation": { "const": "delete", "title": "Operation", "type": "string" },
            "scheduleId": { "format": "uuid", "title": "Scheduleid", "type": "string" },
            "expectedRevision": { "minimum": 1, "title": "Expectedrevision", "type": "integer" }
          },
          "required": ["operation", "scheduleId", "expectedRevision"],
          "title": "ScheduleDeleteArgs",
          "type": "object"
        },
        "SchedulePatch": {
          "additionalProperties": false,
          "properties": {
            "name": {
              "anyOf": [
                { "maxLength": 120, "minLength": 1, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Name"
            },
            "question": {
              "anyOf": [
                { "maxLength": 8000, "minLength": 1, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Question"
            },
            "selection": {
              "anyOf": [
                { "$ref": "#/$defs/MainModelSelection" },
                { "type": "null" }
              ],
              "default": null
            },
            "cron": {
              "anyOf": [
                { "maxLength": 120, "minLength": 1, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Cron"
            },
            "timezone": {
              "anyOf": [
                { "maxLength": 128, "minLength": 1, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Timezone"
            },
            "enabled": {
              "anyOf": [
                { "type": "boolean" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Enabled"
            },
            "workflowAssetRef": {
              "anyOf": [
                { "$ref": "#/$defs/WorkflowAssetRef" },
                { "type": "null" }
              ],
              "default": null
            },
            "constraints": {
              "anyOf": [
                { "$ref": "#/$defs/RunConstraints" },
                { "type": "null" }
              ],
              "default": null
            }
          },
          "title": "SchedulePatch",
          "type": "object"
        },
        "ScheduleUpdateArgs": {
          "additionalProperties": false,
          "properties": {
            "operation": { "const": "update", "title": "Operation", "type": "string" },
            "scheduleId": { "format": "uuid", "title": "Scheduleid", "type": "string" },
            "expectedRevision": { "minimum": 1, "title": "Expectedrevision", "type": "integer" },
            "patch": { "$ref": "#/$defs/SchedulePatch" }
          },
          "required": ["operation", "scheduleId", "expectedRevision", "patch"],
          "title": "ScheduleUpdateArgs",
          "type": "object"
        },
        "WorkflowAssetRef": {
          "additionalProperties": false,
          "properties": {
            "baseline": { "$ref": "#/$defs/RunPeriod" },
            "current": { "$ref": "#/$defs/RunPeriod" },
            "assetId": { "format": "uuid", "title": "Assetid", "type": "string" },
            "definitionVersion": { "const": 1, "title": "Definitionversion", "type": "integer" }
          },
          "required": ["baseline", "current", "assetId", "definitionVersion"],
          "title": "WorkflowAssetRef",
          "type": "object"
        }
      },
      "discriminator": {
        "mapping": {
          "create": "#/$defs/ScheduleCreateArgs",
          "delete": "#/$defs/ScheduleDeleteArgs",
          "update": "#/$defs/ScheduleUpdateArgs"
        },
        "propertyName": "operation"
      },
      "oneOf": [
        { "$ref": "#/$defs/ScheduleCreateArgs" },
        { "$ref": "#/$defs/ScheduleUpdateArgs" },
        { "$ref": "#/$defs/ScheduleDeleteArgs" }
      ]
    }
  },
  "examples": []
}
```

```json [AFTER] schedule_write · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "schedule_write",
    "description": "仅当用户明确要求创建、修改、启停或删除定时分析计划时调用（常规查数分析禁调；独占调用批次，经所有者确认卡生效，定时运行中禁用）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "scheduleId": {
          "type": "string",
          "description": "修改、启停或删除已有任务时必填目标 ID（先经 schedule_read 确认）；新建任务时省略。"
        },
        "name": { "type": "string", "description": "定时任务名称（新建时必填，修改时可选）。" },
        "question": { "type": "string", "description": "每次定时触发执行的分析问题（新建时必填，修改时可选）。" },
        "schedule": {
          "type": "string",
          "description": "触发周期：支持简写 'daily 09:00'、'weekly MON 09:00'（MON..SUN）或五段 Cron（新建时必填）。"
        },
        "status": {
          "type": "string",
          "enum": ["enabled", "paused", "deleted"],
          "description": "任务状态：默认 enabled；暂停传 paused；软删除传 deleted。"
        }
      }
    }
  },
  "examples": []
}
```

### 通道二：场景分流自动触发（scene_routed · 深度分析增强包）

#### 13 · `todo_write`（builtin · `planning`）

- **装载通道**：**scene_routed（L2/L3 对比拆解/异常诊断/报告场景或 plan=required 时自动装载）**
- **执行边界与声明开销**：read_only · 免审批 · 主 Agent 专属 ｜ 紧凑声明体积：`2,837 B (~710 tok)` → `702 B (~176 tok)`，变化 **`−75%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 1/2/4】为 3–6 行单轮待办设计了 `replace`/`select`/`complete`/`skip` 四个子命令 + DAG 拓扑依赖状态机（6 个 `$defs`、19 个属性、`2,837 B`，含空定义 `SkipReason: {}`），19 个参数零说明。
  2. 【病灶 2/4】强制每步手算 `baseVersion` 并对 `todoId` 强卡 `^t[1-9]...` 正则，频繁触发状态迁移报错。
- **重构与装载方案**：
  1. 彻底去复合化：砍掉全部 `op` 子命令、`baseVersion`、`todoId` 正则与 DAG 状态机，收敛为**唯一 1 个幂等全量数组参数 `todos`**；留在 `scene_routed` 避免 L1 单步快查过度规划。
  2. 采用**外简内全适配层**：服务端将 `todos[]` 无损映射回既有 `ExecutionPlan`（自动分配稳定 ID `t1..tn`、`done→verified`、首个 `in_progress→selectedTodoId`、自动递增版本并发送 `RunAutonomyAction`），前端「分析计划」进度环与历史 `op` 检查点回放 100% 兼容。

```json [BEFORE] todo_write · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "todo_write",
    "description": "**用途与调用规则**\n\n- 登记、选择、完成或跳过任务事项，提交当前 baseVersion。",
    "parameters": {
      "type": "object",
      "$defs": {
        "CompleteTodo": {
          "additionalProperties": false,
          "properties": {
            "baseVersion": { "maximum": 400, "minimum": 0, "title": "Baseversion", "type": "integer" },
            "op": { "const": "complete", "title": "Op", "type": "string" },
            "todoId": { "pattern": "^t[1-9][0-9]{0,2}$", "title": "Todoid", "type": "string" },
            "selectTodoId": {
              "anyOf": [
                { "pattern": "^t[1-9][0-9]{0,2}$", "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Selecttodoid"
            }
          },
          "required": ["baseVersion", "op", "todoId"],
          "title": "CompleteTodo",
          "type": "object"
        },
        "ReplaceTodo_TodoDefinition_": {
          "additionalProperties": false,
          "properties": {
            "baseVersion": { "maximum": 400, "minimum": 0, "title": "Baseversion", "type": "integer" },
            "op": { "const": "replace", "title": "Op", "type": "string" },
            "items": {
              "items": { "$ref": "#/$defs/TodoDefinition" },
              "maxItems": 6,
              "minItems": 1,
              "title": "Items",
              "type": "array"
            },
            "selectTodoId": {
              "anyOf": [
                { "pattern": "^t[1-9][0-9]{0,2}$", "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Selecttodoid"
            }
          },
          "required": ["baseVersion", "op", "items"],
          "title": "ReplaceTodo[TodoDefinition]",
          "type": "object"
        },
        "SelectTodo": {
          "additionalProperties": false,
          "properties": {
            "baseVersion": { "maximum": 400, "minimum": 0, "title": "Baseversion", "type": "integer" },
            "op": { "const": "select", "title": "Op", "type": "string" },
            "todoId": { "pattern": "^t[1-9][0-9]{0,2}$", "title": "Todoid", "type": "string" }
          },
          "required": ["baseVersion", "op", "todoId"],
          "title": "SelectTodo",
          "type": "object"
        },
        "SkipReason": { "enum": ["out_of_scope","duplicate_optional"], "type": "string" },
        "SkipTodo": {
          "additionalProperties": false,
          "properties": {
            "baseVersion": { "maximum": 400, "minimum": 0, "title": "Baseversion", "type": "integer" },
            "op": { "const": "skip", "title": "Op", "type": "string" },
            "todoId": { "pattern": "^t[1-9][0-9]{0,2}$", "title": "Todoid", "type": "string" },
            "reason": { "$ref": "#/$defs/SkipReason" }
          },
          "required": ["baseVersion", "op", "todoId", "reason"],
          "title": "SkipTodo",
          "type": "object"
        },
        "TodoDefinition": {
          "additionalProperties": false,
          "properties": {
            "id": { "pattern": "^t[1-9][0-9]{0,2}$", "title": "Id", "type": "string" },
            "goal": { "maxLength": 200, "minLength": 1, "title": "Goal", "type": "string" },
            "dependsOn": {
              "items": { "pattern": "^t[1-9][0-9]{0,2}$", "type": "string" },
              "maxItems": 5,
              "title": "Dependson",
              "type": "array"
            },
            "optional": { "default": false, "title": "Optional", "type": "boolean" }
          },
          "required": ["id", "goal"],
          "title": "TodoDefinition",
          "type": "object"
        }
      },
      "discriminator": {
        "mapping": {
          "complete": "#/$defs/CompleteTodo",
          "replace": "#/$defs/ReplaceTodo_TodoDefinition_",
          "select": "#/$defs/SelectTodo",
          "skip": "#/$defs/SkipTodo"
        },
        "propertyName": "op"
      },
      "oneOf": [
        { "$ref": "#/$defs/ReplaceTodo_TodoDefinition_" },
        { "$ref": "#/$defs/SelectTodo" },
        { "$ref": "#/$defs/CompleteTodo" },
        { "$ref": "#/$defs/SkipTodo" }
      ]
    }
  },
  "examples": []
}
```

```json [AFTER] todo_write · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "todo_write",
    "description": "维护单轮多步分析清单（3..6 步复杂分析时用；1..2 步快查勿调，跨轮长任务用 goal）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "todos": {
          "type": "array",
          "description": "完整步骤列表（2..6 项），每次全量覆盖最新状态。",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["step", "status"],
            "properties": {
              "step": { "type": "string", "description": "本步具体子问题或动作。" },
              "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "done", "skipped"],
                "description": "步骤状态（保持 1 项 in_progress）。"
              }
            }
          }
        }
      },
      "required": ["todos"]
    }
  },
  "examples": []
}
```

#### 14 · `python_analysis`（builtin · `compute_chart`）

- **装载通道**：**scene_routed（启用沙箱时在诊断/预测/清洗/图表/实验/报告与 SC-34 多轮承接场景装载，亦支持前端手动勾选）**
- **执行边界与声明开销**：local_artifact（生成 PNG 附件） · 免审批 · 主 Agent 专属 ｜ 紧凑声明体积：`491 B (~123 tok)` → `533 B (~134 tok)`，变化 **`+9%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 3/7】2 个参数均无 `description`，未说明 `source_query_id` 与上游查询回执 `resultId` 的对应关系，也未交代预置 `data` 字典结构与 `128 KiB` 输入上限。
- **重构与装载方案**：
  1. 顶层说明聚焦何时使用 Python 沙箱，将 `resultId` 来源与 `data={"columns":[],"rows":[[]]}`、`result`、`/workspace/chart.png`、`<=128KiB` 约束精准落入两个参数的 `description` 中。

```json [BEFORE] python_analysis · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "python_analysis",
    "description": "**用途与调用规则**\n\n- 在独立 Python 环境中分析本轮已授权的查询结果。\n- data 是输入数据；将 JSON 结果赋给 result，可写 /workspace/chart.png 生成 PNG。",
    "parameters": {
      "type": "object",
      "properties": {
        "source_query_id": { "type": "string", "maxLength": 128 },
        "code": { "type": "string", "minLength": 1, "maxLength": 16384 }
      },
      "required": ["source_query_id", "code"],
      "additionalProperties": false
    }
  },
  "examples": []
}
```

```json [AFTER] python_analysis · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "python_analysis",
    "description": "在隔离沙箱做统计检验或绘图（简单加总优先用 query_metric 或 SQL）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "source_query_id": { "type": "string", "description": "上游查询回执结果 ID（数据 <=128KiB）。" },
        "code": {
          "type": "string",
          "description": "Python 代码：读 data={\"columns\":[],\"rows\":[[]]}，结果赋给 result，图存 /workspace/chart.png。"
        }
      },
      "required": ["source_query_id", "code"]
    }
  },
  "examples": []
}
```

#### 15 · `sub_agent`（builtin · `orchestration`）

- **装载通道**：**scene_routed（L3 多源异常诊断 SC-03/04/09/19/20、全景报告 SC-26 与复合任务 SC-40 装载）**
- **执行边界与声明开销**：read_only · 免审批（子进程只读隔离 + 继承父级授权） · 主 Agent 专属 ｜ 紧凑声明体积：`3,291 B (~823 tok)` → `952 B (~238 tok)`，变化 **`−71%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 2/5/6】顶层混放旧 `prompt` 与 `tasks` 且无 `required`；把确定性继承的数据库连接 `connectionIds` UUID 数组、只读 `tools` 白名单、步数 `maxSteps` 和 `InputRef.kind` 分类全甩给模型填写（`3,291 B`，12 个属性零说明）。
- **重构与装载方案**：
  1. 彻底剥离 `connectionIds`、`tools`、`maxSteps`、`InputRef.kind` 与遗留 `prompt`，仅保留**唯一必填数组 `tasks`**，每项仅 `name`、`objective`、`inputRefs` 三个纯业务语义字段，权限与只读工具由 Runtime 100% 确定性继承；留在 `scene_routed` 避免简单提问过度派发子任务。
  2. 服务端外简内全适配：对无 `kind` 的 `inputRefs` 按 `parent_evidence → ledger → artifact` 顺序确定性反查并组装标准 `InputRef` 供 `goal/handoff.py` 权限核验与导入，同时兼容历史检查点及存量工作流脚本的旧 `prompt` / `inputs` 载荷。

```json [BEFORE] sub_agent · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "sub_agent",
    "description": "**用途与调用规则**\n\n- 把独立的只读调查分支交给子 Agent，在独立上下文中执行，完成后交回结构化结果。是否委派、是否并行按系统提示中的“委派规则”判断。\n\n- 同一个工具批次最多调用一次 sub_agent；所有互不依赖分支合并进这一次的 tasks 数组。\n- 一个分支：tasks 中放 1 个任务。\n- 互不依赖的多个分支：在同一次调用的 tasks 中一起提交（最多 4 个），系统同时运行其中 2 个，全部结束后一起返回。\n- 后一个分支需要前一个分支的结果时，分两次调用：先等第一个结果写入账本，再把对应账本条目放进下一次的 inputs。\n- 每个任务写清 name（卡片标题，10 字左右）、objective（要回答的问题与范围）、expectations（要交回哪些结论或数值）、constraints（口径与禁止事项）。\n- 已有结论、待办或产物用 inputs 按引用传入，不要把大段原文抄进 objective。子 Agent 看不到你的对话历史。\n- tools 只列分支需要的只读工具；省略表示可用全部只读工具。\n- maxSteps：单表或单指标核对用 3；需要多次探查和交叉核对用 5–8。\n- 返回每个任务的 status、summary、findings（带 evidenceIds）、gaps 和 blockers。partial 或 failed 时根据 gaps 决定自己补查、再委派一次，或记录失败；同一任务不要原样重复委派。\n- 子 Agent 不能请求用户确认、不能写入、不能再委派。需要这些动作的部分留给自己处理。\n- 旧写法 prompt 仍可用，等同于一个只有 objective 的任务。",
    "parameters": {
      "$defs": {
        "DelegationTask": {
          "additionalProperties": false,
          "properties": {
            "name": { "maxLength": 40, "minLength": 1, "title": "Name", "type": "string" },
            "objective": { "maxLength": 8000, "minLength": 1, "title": "Objective", "type": "string" },
            "expectations": {
              "items": { "maxLength": 500, "type": "string" },
              "maxItems": 8,
              "title": "Expectations",
              "type": "array"
            },
            "constraints": {
              "items": { "maxLength": 500, "type": "string" },
              "maxItems": 10,
              "title": "Constraints",
              "type": "array"
            },
            "inputs": {
              "items": { "$ref": "#/$defs/InputRef" },
              "maxItems": 12,
              "title": "Inputs",
              "type": "array"
            },
            "tools": {
              "items": { "type": "string" },
              "maxItems": 8,
              "title": "Tools",
              "type": "array"
            },
            "connectionIds": {
              "items": { "pattern": "^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$", "type": "string" },
              "maxItems": 4,
              "title": "Connectionids",
              "type": "array"
            },
            "maxSteps": { "default": 3, "maximum": 8, "minimum": 3, "title": "Maxsteps", "type": "integer" }
          },
          "required": ["name", "objective"],
          "title": "DelegationTask",
          "type": "object"
        },
        "InputRef": {
          "additionalProperties": false,
          "properties": {
            "kind": { "enum": ["ledger","artifact","evidence"], "title": "Kind", "type": "string" },
            "id": { "maxLength": 200, "minLength": 1, "title": "Id", "type": "string" }
          },
          "required": ["kind", "id"],
          "title": "InputRef",
          "type": "object"
        }
      },
      "additionalProperties": false,
      "properties": {
        "prompt": {
          "anyOf": [
            { "maxLength": 8000, "minLength": 1, "pattern": "\\S", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Prompt"
        },
        "tasks": {
          "anyOf": [
            {
              "items": { "$ref": "#/$defs/DelegationTask" },
              "maxItems": 4,
              "minItems": 1,
              "type": "array"
            },
            { "type": "null" }
          ],
          "default": null,
          "title": "Tasks"
        }
      },
      "title": "SubAgentInput",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] sub_agent · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "sub_agent",
    "description": "将互不依赖的只读调查子问题委派给独立子 Agent 并行探查（同一工具批次仅调用一次，子任务无对话历史且无权执行写操作）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "tasks": {
          "type": "array",
          "description": "并行执行的独立只读子任务列表（1..4 项，name 互不重复）。",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["name", "objective"],
            "properties": {
              "name": { "type": "string", "description": "子任务短标题（用于界面卡片展示，<=16 字）。" },
              "objective": { "type": "string", "description": "独立可执行的调查问题、统计期间、口径约束与期望交回的指标结论。" },
              "inputRefs": {
                "type": "array",
                "items": { "type": "string" },
                "description": "按引用传入父任务已取得的 evidenceId、resultId 或账本条目 ID。"
              }
            }
          }
        }
      },
      "required": ["tasks"]
    }
  },
  "examples": []
}
```

### 通道三：前端用户手动选择装载（user_selected · 高阶模式、口径治理与个人扩展）

#### 16 · `goal`（builtin · `long_task`）

- **装载通道**：**user_selected（前端开启「长任务目标」模式或会话已有活动 Goal / SC-40 时装载）**
- **执行边界与声明开销**：read_only（propose 需所有者确认） · 主 Agent 专属 ｜ 紧凑声明体积：`6,044 B (~1511 tok)` → `1,280 B (~320 tok)`，变化 **`−79%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 1/2/3/4】全目录最重型双层复合工具（`6,044 B`、8 个 `$defs`、31 个属性全部零说明、顶层内嵌整张 Markdown 表）：混入微美元 `costMicrousd`、`goalId`、`expectedRevision`、英文正则 `key`/`factKey`，且在日常短问答全程陪跑。
- **重构与装载方案**：
  1. 纳入前端「长任务目标」开关控制（有未完结活动 Goal 或 `SC-40` 时自动续挂）；砍掉内部计费/版本号/键名正则及重复的待办子类型，拍平为 **6 个清晰参数**（增加轻量整型数组 `acceptanceIndexes` 标注达标验收项序号）。
  2. 采用**外简内全适配层**：服务端自动为 `propose` 的验收标准编号 `a1..aN` 并补齐默认预算与版本，将 `record` 的 `acceptanceIndexes`（如 `[1,2]`）映射为 `LedgerItem.acceptanceIds` 与合法 `key`，`complete` 自动带入当前 `goal.revision`；既有 `GoalController`、`Ledger`、`gate.py` 程序化完成门禁与前端目标面板 100% 兼容。

```json [BEFORE] goal · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "goal",
    "description": "**用途与调用规则**\n\n- 管理本会话的长任务目标与进度账本。用 op 选择操作：\n\n| op | 什么时候用 | 结果 |\n| --- | --- | --- |\n| propose | 用户交代多阶段、需要多轮推进或明确要求“持续做完”的任务，且本会话没有进行中的目标；或用户要求修改目标 | 弹出目标卡等待用户确认，确认后目标生效 |\n| record | 取得带证据的阶段结论、规划或完成待办、某条路径失败时 | 写入进度账本，后续轮次从账本继续 |\n| complete | 认为每个验收项都有结论支撑、必需待办已完成时 | 系统核验；未通过时返回缺口并生成补救待办，继续推进 |\n| block | 只有用户才能提供的决定、数据或权限阻止继续推进时 | 目标转为受阻，等待用户处理 |\n\n- 对明确持续完成的多阶段任务，首个工具批次只提交 propose；连接探查和数据查询安排在用户确认之后。\n- 简单问答、一次查询能回答的问题不要 propose。\n- propose 的验收项写成可核对的陈述，每项能由一条带证据的结论支撑；约束写口径、范围和禁止事项；预算不超过默认值，除非用户指定。\n- 修改已有目标时 propose 需带 goalId 与 expectedRevision（取自目标锚点）。\n- record 的结论必须引用本轮 evidenceIds；同一事实使用稳定的 factKey，便于发现冲突。数值写 value 与 unit。\n- 待办用于跨轮推进：每个阶段开始前登记，完成后标记 done。必需待办未完成时无法完成目标。\n- 失败也要 record，写明尝试过什么、为什么失败，避免后续轮次重复。\n- complete 被打回时，按返回的 gaps 补齐，不要立即重复申请。\n- block 不用于普通的工具错误或空结果；可以换方法继续时继续。\n- 目标的创建、修改、恢复以用户确认为准；预算和状态以目标锚点为准。",
    "parameters": {
      "type": "object",
      "$defs": {
        "BlockGoal": {
          "additionalProperties": false,
          "properties": {
            "op": { "const": "block", "title": "Op", "type": "string" },
            "reason": { "maxLength": 60, "minLength": 1, "title": "Reason", "type": "string" }
          },
          "required": ["op", "reason"],
          "title": "BlockGoal",
          "type": "object"
        },
        "CompleteGoal": {
          "additionalProperties": false,
          "properties": {
            "op": { "const": "complete", "title": "Op", "type": "string" },
            "expectedRevision": { "minimum": 1, "title": "Expectedrevision", "type": "integer" }
          },
          "required": ["op", "expectedRevision"],
          "title": "CompleteGoal",
          "type": "object"
        },
        "GoalAcceptance": {
          "additionalProperties": false,
          "properties": {
            "id": { "pattern": "^a[0-9]{1,2}$", "title": "Id", "type": "string" },
            "text": { "maxLength": 300, "minLength": 1, "title": "Text", "type": "string" }
          },
          "required": ["id", "text"],
          "title": "GoalAcceptance",
          "type": "object"
        },
        "GoalBudget": {
          "additionalProperties": false,
          "properties": {
            "maxRounds": {
              "default": 12,
              "maximum": 32,
              "minimum": 1,
              "title": "Maxrounds",
              "type": "integer"
            },
            "tokens": {
              "default": 2000000,
              "maximum": 10000000,
              "minimum": 10000,
              "title": "Tokens",
              "type": "integer"
            },
            "costMicrousd": {
              "default": 20000000,
              "maximum": 20000000,
              "minimum": 1,
              "title": "Costmicrousd",
              "type": "integer"
            }
          },
          "title": "GoalBudget",
          "type": "object"
        },
        "GoalContract": {
          "additionalProperties": false,
          "properties": {
            "objective": { "maxLength": 2000, "minLength": 1, "title": "Objective", "type": "string" },
            "acceptance": {
              "items": { "$ref": "#/$defs/GoalAcceptance" },
              "maxItems": 10,
              "minItems": 1,
              "title": "Acceptance",
              "type": "array"
            },
            "constraints": {
              "items": { "maxLength": 500, "minLength": 1, "type": "string" },
              "maxItems": 10,
              "title": "Constraints",
              "type": "array"
            }
          },
          "required": ["objective", "acceptance"],
          "title": "GoalContract",
          "type": "object"
        },
        "LedgerItem": {
          "additionalProperties": false,
          "properties": {
            "type": {
              "enum": ["conclusion", "todo", "todo_done", "failure"],
              "title": "Type",
              "type": "string"
            },
            "key": { "pattern": "^[a-z0-9_.:-]{1,80}$", "title": "Key", "type": "string" },
            "title": { "maxLength": 200, "minLength": 1, "title": "Title", "type": "string" },
            "text": {
              "anyOf": [
                { "maxLength": 2000, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Text"
            },
            "evidenceIds": {
              "items": { "type": "string" },
              "maxItems": 8,
              "title": "Evidenceids",
              "type": "array"
            },
            "factKey": {
              "anyOf": [
                { "pattern": "^[a-z0-9_.:-]{1,80}$", "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Factkey"
            },
            "value": {
              "anyOf": [
                { "type": "number" },
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Value"
            },
            "unit": {
              "anyOf": [
                { "maxLength": 20, "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Unit"
            },
            "acceptanceIds": {
              "items": { "type": "string" },
              "maxItems": 10,
              "title": "Acceptanceids",
              "type": "array"
            },
            "dependsOn": {
              "items": { "type": "string" },
              "maxItems": 16,
              "title": "Dependson",
              "type": "array"
            },
            "resolves": {
              "items": { "type": "string" },
              "maxItems": 10,
              "title": "Resolves",
              "type": "array"
            },
            "required": { "default": true, "title": "Required", "type": "boolean" }
          },
          "required": ["type", "key", "title"],
          "title": "LedgerItem",
          "type": "object"
        },
        "ProposeGoal": {
          "additionalProperties": false,
          "properties": {
            "op": { "const": "propose", "title": "Op", "type": "string" },
            "contract": { "$ref": "#/$defs/GoalContract" },
            "budget": { "$ref": "#/$defs/GoalBudget" },
            "goalId": {
              "anyOf": [
                { "pattern": "^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$", "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Goalid"
            },
            "expectedRevision": {
              "anyOf": [
                { "minimum": 1, "type": "integer" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Expectedrevision"
            }
          },
          "required": ["op", "contract"],
          "title": "ProposeGoal",
          "type": "object"
        },
        "RecordProgress": {
          "additionalProperties": false,
          "properties": {
            "op": { "const": "record", "title": "Op", "type": "string" },
            "items": {
              "items": { "$ref": "#/$defs/LedgerItem" },
              "maxItems": 10,
              "minItems": 1,
              "title": "Items",
              "type": "array"
            }
          },
          "required": ["op", "items"],
          "title": "RecordProgress",
          "type": "object"
        }
      },
      "discriminator": {
        "mapping": {
          "block": "#/$defs/BlockGoal",
          "complete": "#/$defs/CompleteGoal",
          "propose": "#/$defs/ProposeGoal",
          "record": "#/$defs/RecordProgress"
        },
        "propertyName": "op"
      },
      "oneOf": [
        { "$ref": "#/$defs/ProposeGoal" },
        { "$ref": "#/$defs/RecordProgress" },
        { "$ref": "#/$defs/CompleteGoal" },
        { "$ref": "#/$defs/BlockGoal" }
      ],
      "title": "GoalInput"
    }
  },
  "examples": []
}
```

```json [AFTER] goal · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "goal",
    "description": "管理跨多轮持续推进的复杂长任务目标与结论账本（单轮内多步分析用 todo_write，常规短问答勿调）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "action": {
          "type": "string",
          "enum": ["propose", "record", "complete", "block"],
          "description": "动作：propose 立项待用户确认；record 记阶段结论或受阻尝试；complete 全部达标后申请核验；block 缺外部决策或权限时挂起。"
        },
        "objective": { "type": "string", "description": "action=propose 时的长任务总目标；action=block 时填阻塞原因。" },
        "acceptance": {
          "type": "array",
          "items": { "type": "string" },
          "description": "action=propose 时的可核验完成标准列表（1..5 项）。"
        },
        "finding": { "type": "string", "description": "action=record 时写入跨轮账本的阶段结论或失败原因摘要。" },
        "evidenceIds": {
          "type": "array",
          "items": { "type": "string" },
          "description": "action=record 支撑该结论的本轮工具 evidenceId 列表。"
        },
        "acceptanceIndexes": {
          "type": "array",
          "items": { "type": "integer" },
          "description": "action=record 时本结论已满足的验收项序号（1 起始，对应立项列表顺序）；未满足任何验收项时省略。"
        }
      },
      "required": ["action"]
    }
  },
  "examples": []
}
```

#### 17 · `create_workflow`（builtin · `orchestration`）

- **装载通道**：**user_selected（前端勾选「工作流编排」能力组或命中 SC-29/30 自动化场景装载）**
- **执行边界与声明开销**：read_only（试跑通过后弹出保存邀请卡） · 主 Agent 专属 ｜ 紧凑声明体积：`1,638 B (~410 tok)` → `1,084 B (~271 tok)`，变化 **`−34%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 6/7】跨工具文档腐化：描述仍在教模型写已淘汰的 `agent("sub_agent", {prompt})` 与 `{answer}`；5 个参数中 3 个无说明，失败统一报笼统 `invalid_workflow_script`；普通查数全程暴露。
- **重构与装载方案**：
  1. 归入「工作流编排」手动选择包（`SC-29/30` 自动兜底开启）；修正子任务调用约定为最新 `{tasks: [...]}` 协议，并**同步升级 `workflows/engine.py`** 双向兼容 `{tasks: [...]}` 与历史存量工作流脚本的 `{prompt: "..."}` 调用，服务端拆分四段定位错误码。

```json [BEFORE] create_workflow · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "create_workflow",
    "description": "**用途与调用规则**\n\n- 用户要求沉淀已验证的分析方法时，编写可复用的 JavaScript 编排并试跑。\n- scriptSource 填写异步函数体，第一行可直接写 const 参数 = args.字段。\n- 用 await agent(\"sub_agent\", {prompt: \"完整子任务\"}) 调用子 Agent，返回 {answer}。\n- 用 await parallel([() => agent(...), () => agent(...)]) 并行独立任务。\n- 每次最多 3 个子任务，同时最多 2 个，单个子任务入参最多 4 KiB。\n- 以 return {status: 'ok', result: ...} 提交可复用结果，数据不足时返回 {status: 'needs_data', result: ...}。\n- 将会变化的数据范围作为参数，并给子任务提供所需的表、口径和范围。\n- 收到 invalid_workflow_script 时，修正 JavaScript 语法后再试跑。\n- 试跑成功后由用户确认保存。",
    "parameters": {
      "additionalProperties": false,
      "properties": {
        "name": { "maxLength": 80, "minLength": 1, "title": "Name", "type": "string" },
        "description": { "maxLength": 500, "minLength": 1, "title": "Description", "type": "string" },
        "parameters": {
          "additionalProperties": true,
          "description": "调用此工作流的 JSON Schema。顶层 type 必须为 object。",
          "title": "Parameters",
          "type": "object"
        },
        "scriptSource": { "maxLength": 16384, "minLength": 1, "title": "Scriptsource", "type": "string" },
        "args": {
          "additionalProperties": true,
          "description": "本次用来试跑的实际参数，符合 parameters。",
          "title": "Args",
          "type": "object"
        }
      },
      "required": ["name", "description", "parameters", "scriptSource", "args"],
      "title": "CreateWorkflowInput",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] create_workflow · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "create_workflow",
    "description": "当用户明确要求将已验证的分析流程沉淀为可复用工作流模板时，提交入参定义与异步编排脚本并用本次参数即时试跑（试跑通过后向用户弹出保存确认卡）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "name": { "type": "string", "description": "工作流模板名称（<=80 字）。" },
        "description": { "type": "string", "description": "工作流适用场景与输出摘要说明。" },
        "parameters": {
          "type": "object",
          "description": "工作流入参 JSON Schema（顶层须包含 type='object' 与 properties 字段定义）。"
        },
        "scriptSource": {
          "type": "string",
          "description": "JS 异步函数体：从 args.<字段> 读入参，调用 await agent('sub_agent', {tasks:[...]}) 或 await parallel([...])，以 return {status:'ok', result} 结束。"
        },
        "args": { "type": "object", "description": "用于本次即时试跑验证的实参对象（须匹配 parameters 定义）。" }
      },
      "required": ["name", "description", "parameters", "scriptSource", "args"]
    }
  },
  "examples": []
}
```

#### 18 · `discover_semantic_candidates`（business · `semantic_govern`）

- **装载通道**：**user_selected（仅维护者可勾选「指标口径治理」能力组，或 SC-36/38 自动装载）**
- **执行边界与声明开销**：local_effect · 保存/修订需人工确认卡 · 主 Agent 专属 ｜ 紧凑声明体积：`3,931 B (~983 tok)` → `1,473 B (~369 tok)`，变化 **`−63%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 1/2/6】把主动推荐、保存草稿、修订存量指标混入 19 个平铺属性（`3,931 B`，18 处 `anyOf null`），泄漏 `owner_scope_id`、`usage_scope_id`、`timezone` 等确定性范围参数，且在普通只读查数会话全程暴露。
- **重构与装载方案**：
  1. 物理隔离至「指标口径治理」手动选择包（普通查数 100% 不暴露）；砍掉全部组织范围与时区参数，收敛为 **4 个正交扁平参数**（`mode`、`spec`、`resultRef`、`targetMetric`）。
  2. 采用**外简内全适配层**：显式保留极简枚举 `mode`（`"suggest" | "save_draft" | "revise"`）无损映射到既有 `semantic/tool.py` 三条路径——`suggest` 走免阻塞推荐，`save_draft` 与 `revise` 分别弹出既有 `semantic_draft` / `semantic_result_choice` 与 `semantic_revision` 人工确认卡。

```json [BEFORE] discover_semantic_candidates · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "discover_semantic_candidates",
    "description": "**用途与调用规则**\n\n- 主动建议留存仅用于本轮实际算出、目录尚未覆盖且有明确重复决策用途的新指标。\n- 先用 search_semantics 比较相关名称、别名和业务含义。\n- 符合时单独调用 trigger=suggest，传本轮 database_query 回执的 semantic_result_ref 作为 result_ref，fields 填业务名称、业务域、统计口径、计量单位和一行可读计算公式。\n- 公式用业务名称及四则运算表达，例如「符合条件的订单数 ÷ 参与统计的订单数 × 100%」。\n- 筛选、时间边界和去重规则写入统计口径；表名、SQL 和实现细节留在查询回执，reuse_reason 说明具体复用场景。\n- 成功后会在回答下方展示候选，由用户决定是否保存。\n- 逐条推荐，最多三项，可以零项。\n- 明细、探查、一次性筛选或已有指标在新时段的结果直接回答。\n- 用户主动要求将此前的计算存为草稿时，用 requested 和 previous_results 中的 result_ref，按需提供建议字段。\n- 用户说“刚才的结果”可省略引用。\n- 保存前由用户核对。\n- 修订已保存指标时传 revise_metric（名称或代码）及本次改动的 fields。\n- 从授权素材提取时传 source_refs 和归属范围。",
    "parameters": {
      "$defs": {
        "SuggestedDraftFields": {
          "properties": {
            "name": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Name"
            },
            "code": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Code"
            },
            "domain": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Domain"
            },
            "summary": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Summary"
            },
            "responsible": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Responsible"
            },
            "grain": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Grain"
            },
            "unit": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Unit"
            },
            "formula": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Formula"
            },
            "event_time": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Event Time"
            },
            "timezone": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Timezone"
            },
            "aggregation": {
              "anyOf": [
                { "type": "string" },
                { "type": "null" }
              ],
              "default": null,
              "title": "Aggregation"
            }
          },
          "title": "SuggestedDraftFields",
          "type": "object"
        }
      },
      "additionalProperties": false,
      "properties": {
        "revise_metric": {
          "anyOf": [
            { "maxLength": 160, "minLength": 1, "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "description": "修订已保存的指标时填现有名称、唯一代码或资产 ID，fields 只填本次修改。",
          "title": "Revise Metric"
        },
        "source_refs": {
          "anyOf": [
            {
              "items": { "format": "uuid", "type": "string" },
              "maxItems": 8,
              "type": "array"
            },
            { "type": "null" }
          ],
          "default": null,
          "title": "Source Refs"
        },
        "owner_scope_id": {
          "anyOf": [
            { "format": "uuid", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Owner Scope Id"
        },
        "usage_scope_id": {
          "anyOf": [
            { "format": "uuid", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Usage Scope Id"
        },
        "result_ref": {
          "anyOf": [
            { "format": "uuid", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "description": "可选。用户指的是刚才的查询时可以省略，系统会在当前会话查找并让用户选择。",
          "title": "Result Ref"
        },
        "trigger": {
          "default": "requested",
          "description": "用户要求保存或选择时为 requested。只有主动推荐时填 suggest。",
          "enum": ["requested", "suggest"],
          "title": "Trigger",
          "type": "string"
        },
        "reuse_reason": {
          "anyOf": [
            { "maxLength": 400, "minLength": 1, "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "description": "主动推荐时说明哪些经营决策会跨期重复使用这项计算。",
          "title": "Reuse Reason"
        },
        "fields": {
          "anyOf": [
            { "$ref": "#/$defs/SuggestedDraftFields" },
            { "type": "null" }
          ],
          "default": null
        }
      },
      "title": "Input",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] discover_semantic_candidates · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "discover_semantic_candidates",
    "description": "提报新的可复用经营指标草稿、顺带推荐候选指标或修订存量指标定义（仅在维护口径或沉淀高复用指标时调用）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "mode": {
          "type": "string",
          "enum": ["suggest", "save_draft", "revise"],
          "description": "动作模式：suggest 为回答后免弹窗推荐候选；save_draft 弹窗保存新草稿（默认）；revise 弹窗修订存量指标。"
        },
        "spec": {
          "type": "object",
          "description": "拟保存或修订的业务口径定义（修订存量指标时仅填变更字段）。",
          "additionalProperties": false,
          "properties": {
            "name": { "type": "string", "description": "指标中文业务名称。" },
            "summary": { "type": "string", "description": "统计口径说明（含筛选、时间边界与剔除规则）。" },
            "formula": { "type": "string", "description": "可读业务计算公式（如 '净退货额 / 签收净销售额'）。" },
            "unit": { "type": "string", "description": "计量单位（如 元、%、单）。" },
            "grain": { "type": "string", "description": "统计粒度（如 订单、门店×日）。" }
          }
        },
        "resultRef": { "type": "string", "description": "提报新指标草稿时关联的本轮查询结果 ID；指代刚才的查询可省略。" },
        "targetMetric": { "type": "string", "description": "mode=revise 时填写目标指标名称、编码或 ID；新建草稿或推荐时省略。" }
      },
      "required": ["spec"]
    }
  },
  "examples": []
}
```

#### 19 · `review_semantic_change`（business · `semantic_govern`）

- **装载通道**：**user_selected（随「指标口径治理」开关、会话已有草稿版本或 SC-36/38 装载）**
- **执行边界与声明开销**：read_only · 免审批 · 主 Agent 专属 ｜ 紧凑声明体积：`396 B (~99 tok)` → `331 B (~83 tok)`，变化 **`−16%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 3/4/7】原参数零 `description` 且携带冗余 `title`，顶层未交代输出 `digest` 如何衔接下游送审，且在只读查数会话全程陪跑。
- **重构与装载方案**：
  1. 随「指标口径治理」包成组挂载（或本会话已生成草稿后自动续挂）；剥离 `title` 并为唯一参数 `version_id` 补齐来源说明。

```json [BEFORE] review_semantic_change · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "review_semantic_change",
    "description": "**用途与调用规则**\n\n- 检查草稿来源、声明、依赖、冲突与已登记影响，说明阻断项及修正建议。",
    "parameters": {
      "additionalProperties": false,
      "properties": {
        "version_id": { "format": "uuid", "title": "Version Id", "type": "string" }
      },
      "required": ["version_id"],
      "title": "Input",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] review_semantic_change · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "review_semantic_change",
    "description": "送审前校验语义草稿公式与冲突（通过后取回执 digest 送审）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "version_id": { "type": "string", "description": "草稿版本 ID。" }
      },
      "required": ["version_id"]
    }
  },
  "examples": []
}
```

#### 20 · `submit_semantic_change`（business · `semantic_govern`）

- **装载通道**：**user_selected（仅维护者勾选「指标口径治理」、草稿已校验通过或 SC-36/38 装载）**
- **执行边界与声明开销**：local_effect · 强制维护者人工确认（HITL） · 主 Agent 专属 ｜ 紧凑声明体积：`751 B (~188 tok)` → `687 B (~172 tok)`，变化 **`−9%`**
- **诊断报告命中的原始病灶**：
  1. 【病灶 2/3/4】强制模型手写随机幂等键 `request_key`（UUID 必填）并暴露底层 `binding_id`，5 个参数全部零 `description`。
- **重构与装载方案**：
  1. 随「指标口径治理」包隔离保护；彻底删除 `request_key`（服务端用 `uuid5` 确定性生成）与 `binding_id`，仅保留 **3 个必填业务参数**。

```json [BEFORE] submit_semantic_change · 改造前原始声明
{
  "type": "function",
  "function": {
    "name": "submit_semantic_change",
    "description": "**用途与调用规则**\n\n- 把已保存和校验的版本提交双人评审，需要维护者确认。",
    "parameters": {
      "additionalProperties": false,
      "properties": {
        "version_id": { "format": "uuid", "title": "Version Id", "type": "string" },
        "expected_digest": { "pattern": "^[0-9a-f]{64}$", "title": "Expected Digest", "type": "string" },
        "note": { "maxLength": 160, "minLength": 1, "title": "Note", "type": "string" },
        "request_key": { "format": "uuid", "title": "Request Key", "type": "string" },
        "binding_id": {
          "anyOf": [
            { "format": "uuid", "type": "string" },
            { "type": "null" }
          ],
          "default": null,
          "title": "Binding Id"
        }
      },
      "required": ["version_id", "expected_digest", "note", "request_key"],
      "title": "Input",
      "type": "object"
    }
  },
  "examples": []
}
```

```json [AFTER] submit_semantic_change · 改造后极简单一职责契约
{
  "type": "function",
  "function": {
    "name": "submit_semantic_change",
    "description": "将已通过 review_semantic_change 校验无阻断错误的语义草稿提交双人专家评审（生成确认卡经维护者确认后正式送审）。",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "version_id": { "type": "string", "description": "已通过校验的语义草稿版本 ID。" },
        "expected_digest": { "type": "string", "description": "review_semantic_change 校验通过回执返回的 digest 摘要串。" },
        "note": { "type": "string", "description": "提交专家评审的变更原因与业务影响简述（<=160 字）。" }
      },
      "required": ["version_id", "expected_digest", "note"]
    }
  },
  "examples": []
}
```
