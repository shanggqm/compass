import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const COMPASS_ROOT = path.resolve(__dirname, "../../../..");
const PROMPTS_DIR = path.join(
  COMPASS_ROOT,
  "apps/agent/src/compass_agent/prompts",
);
const PROMPTS_TOOLS_DIR = path.join(PROMPTS_DIR, "tools");

/**
 * Deterministic compact-leaf JSON formatter:
 * Keeps primitive leaf descriptors on a single readable line when short, while
 * keeping named parameter entries under "properties" expanded cleanly for line-by-line diffs.
 */
export function formatSchemaJson(value, indent = 0, parentKey = "") {
  const pad = "  ".repeat(indent);
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    const allPrimitive = value.every(
      (item) => item === null || typeof item !== "object",
    );
    if (allPrimitive) {
      const inline = `[${value.map((v) => JSON.stringify(v)).join(", ")}]`;
      if (inline.length <= 76) return inline;
    }
    const items = value.map(
      (item) =>
        `${"  ".repeat(indent + 1)}${formatSchemaJson(item, indent + 1, "[]")}`,
    );
    return `[\n${items.join(",\n")}\n${pad}]`;
  }

  const keys = Object.keys(value);
  if (keys.length === 0) return "{}";

  const structuralKeys = new Set([
    "function",
    "parameters",
    "properties",
    "$defs",
    "discriminator",
    "mapping",
    "examples",
    "arguments",
  ]);
  const canInline =
    indent >= 3 &&
    parentKey !== "properties" &&
    parentKey !== "$defs" &&
    !keys.some((k) => structuralKeys.has(k)) &&
    keys.every((k) => {
      const v = value[k];
      if (v === null || typeof v !== "object") return true;
      if (
        Array.isArray(v) &&
        v.every((x) => x === null || typeof x !== "object")
      )
        return true;
      return false;
    });

  if (canInline) {
    const inline = `{ ${keys.map((k) => `${JSON.stringify(k)}: ${JSON.stringify(value[k])}`).join(", ")} }`;
    if (inline.length <= 84) return inline;
  }

  const entries = keys.map(
    (k) =>
      `${"  ".repeat(indent + 1)}${JSON.stringify(k)}: ${formatSchemaJson(value[k], indent + 1, k)}`,
  );
  return `{\n${entries.join(",\n")}\n${pad}}`;
}

/**
 * Exact compact UTF-8 wire bytes sent to the LLM in GenerateInput.tools
 * (matches compass_agent.tools.contracts.json_bytes).
 */
export function wireBytes(toolDoc) {
  if (!toolDoc) return 0;
  const cloneDoc = structuredClone(toolDoc);
  const examples = cloneDoc.examples;
  delete cloneDoc.examples;
  if (Array.isArray(examples) && examples.length > 0) {
    cloneDoc.function.description += `\n调用示例：\n${JSON.stringify(examples)}`;
  }
  return Buffer.byteLength(JSON.stringify(cloneDoc), "utf8");
}

function slimTool(name, description, required, properties) {
  const params = {
    type: "object",
    additionalProperties: false,
    properties,
  };
  if (required && required.length > 0) {
    params.required = required;
  }
  return {
    type: "function",
    function: {
      name,
      description,
      parameters: params,
    },
    examples: [],
  };
}

async function readOriginal(name) {
  const raw = await fs.readFile(
    path.join(PROMPTS_TOOLS_DIR, `${name}.json`),
    "utf8",
  );
  return JSON.parse(raw);
}

export async function loadAllToolSpecs() {
  const originals = {};
  const baselineNames = [
    "final_answer",
    "read_tool_result",
    "todo_write",
    "database_query",
    "python_analysis",
    "sub_agent",
    "create_workflow",
    "schedule_read",
    "schedule_write",
    "goal",
    "search_semantics",
    "get_metric_definition",
    "resolve_semantics",
    "query_metric",
    "discover_semantic_candidates",
    "review_semantic_change",
    "submit_semantic_change",
    "search_documents",
    "read_document",
  ];
  for (const name of baselineNames) {
    originals[name] = await readOriginal(name);
  }
  originals.report_to_parent = JSON.parse(
    await fs.readFile(
      path.join(PROMPTS_DIR, "support/subagent/report-to-parent.json"),
      "utf8",
    ),
  );

  const specs = [
    // ==================== CHANNEL 1: ALWAYS ON ====================
    {
      index: "01",
      group: "通道一：内建默认常驻（always_on · 全场景底座）",
      name: "final_answer",
      isStaticCatalog: true,
      category: "builtin",
      domain: "core_control",
      channel: "always_on（主 Agent 工作与收口阶段默认常驻）",
      policy: "read_only · 免审批 · 工程强制末位调用 · 流式输出后直接 stop",
      problems: [
        "【病灶 1/4】原设计要求模型把整篇回答与引用树（9 个 `$defs`、38 个属性、`4,953 B`）全部塞进工具入参 JSON 字符串，既消耗高额声明与转义 Token，又必须等工具 JSON 完整生成校验后才能一次性返回前端，首字延迟高且易触发长度截断。",
        "【病灶 5】强迫模型手写 `coveredGoalIds`、`unresolved`、`completion`、`claims` 与 `blocks` 多重冗余记账，手抄 36 位 `evidenceId` 极易错配。",
      ],
      solution: [
        "极致收敛为**唯一必填参数 `mode`（`\"plain\" | \"evidence\"`）+ 1 个可选扁平参数 `artifactFormat`（`\"markdown\" | \"csv\" | \"dashboard\"`）**：彻底解放最终回答形态，不受工具 JSON 结构束缚；按 `mode` 路由至两套独立 Answer Prompt（`prompts/answer/{plain,evidence}.md`），在 `answerProtocol == 3` 下先执行 `check_access` 权限复核，再向前端实时流式推送 SSE 数据包并在流结束后强制 `stop`。",
        "循证模式（`mode=\"evidence\"`）由服务端工程层对本轮已验证的检索/查询证据去重、排序、过滤并编号 `[1]..[N]`，直接注入 `{{ordered_evidences}}` 模板变量，正文输出 `^[1]^` 角标；流式结束后由工程层解析精确偏移与完整 `sources[]` 经 `publish_citations` 落盘（扩展 `citations.py` 与 `<CitedAnswer>` 双格式兼容 `^[1]^` 与存量 `[1]`），同时按 `artifactFormat` 复用 `build_artifact` 产出可下载报告/CSV/看板；同批混调其他工具时为抢跑 `final_answer` 返回标准 `role=\"tool\"` 延期回执（禁止静默删除 `tool_call`）；纯澄清/拒答场景（`SC-33/37/39`）支持单跳直出流式文本。",
      ],
      before: originals.final_answer,
      buildAfter() {
        return slimTool(
          "final_answer",
          "当信息已备齐或需向用户追问澄清时调用，触发最终流式回答生成并在输出完成后直接结束本轮运行（必须作为最后一步单独调用）。",
          ["mode"],
          {
            mode: {
              type: "string",
              enum: ["plain", "evidence"],
              description:
                "回答模式：plain 用于概念解释、操作说明或追问澄清；evidence 用于基于本轮检索/查询结果生成带 ^[1]^ 证据角标的循证回答。",
            },
            artifactFormat: {
              type: "string",
              enum: ["markdown", "csv", "dashboard"],
              description:
                "仅当用户明确要求导出可下载文件时填写（markdown 报告 / csv 明细表 / dashboard 看板）；常规问答省略。",
            },
          },
        );
      },
    },
    {
      index: "02",
      group: "通道一：内建默认常驻（always_on · 全场景底座）",
      name: "read_tool_result",
      isStaticCatalog: true,
      category: "builtin",
      domain: "core_control",
      channel: "always_on（主 Agent 与子 Agent 工作阶段默认常驻）",
      policy: "read_only · 免审批 · 子 Agent 可继承",
      problems: [
        "【病灶 3/4】顶层描述花大段解释返回字段 `complete`/`sourceComplete` 与权限内幕，而 4 个输入参数的 `description` 全部为空，且包裹了 3 处 6 行式 `anyOf null`。",
        "【病灶 6】`cursor` 与 `offset`/`limit` 互斥藏在 Python 层，续页误带残留 `offset` 直接报黑盒 `invalid_parameters`。",
      ],
      solution: [
        "作为任意大结果截断后的通用读取原语保持默认常驻；剥离全部 `anyOf null` 与 `title`，逐个参数用一句话写清定位约束。",
        "服务端宽进严出：当 `cursor` 非空时确定性按游标续读并自动忽略残留 `offset`，零条件 Schema 根除互斥报错。",
      ],
      before: originals.read_tool_result,
      buildAfter() {
        return slimTool(
          "read_tool_result",
          "当工具回执截断（truncated=true）且预览不足时，分页读取已保存的结果原件。",
          ["ref"],
          {
            ref: {
              type: "string",
              description: "上游回执返回的 resultId 或 ref。",
            },
            cursor: {
              type: "string",
              description: "续页传上一页 nextCursor；首次省略。",
            },
            offset: {
              type: "integer",
              description: "首次读取 0 起始行号或字符偏移；传 cursor 时忽略。",
            },
            limit: {
              type: "integer",
              description: "单次条数上限（1..200，默认 50）。",
            },
          },
        );
      },
    },
    {
      index: "03",
      group: "通道一：内建默认常驻（always_on · 全场景底座）",
      name: "report_to_parent",
      isStaticCatalog: false,
      category: "subagent_only（子 Agent 专用底座）",
      domain: "core_control",
      channel: "always_on（仅子 Agent 运行时替代 final_answer 默认常驻）",
      policy: "read_only · 免审批 · 子 Agent 专属完成工具",
      problems: [
        "【病灶 1/3/4】子 Agent 仅有 3–8 步预算，却把消耗整步推理的 `kind=\"progress\"` 心跳与 `kind=\"final\"` 终止交付做成复合工具（4 个 `$defs`、16 个属性零说明、`3,111 B`），顶层塞入整张 Markdown 表格并对 `factKey` 强卡英文正则。",
      ],
      solution: [
        "作为子 Agent 的唯一常驻完成出口：砍掉消耗步数的 `kind=\"progress\"` 分支（中间进度由 Runtime 工具事件确定性推送），清零 `$defs` 与 `factKey` 正则，收敛为结束单次调用的 **4 个扁平参数**（`summary`、`evidenceIds`、`gaps`、`status`）。",
        "采用**外简内全适配层**：服务端将扁平入参无损翻译为既有 `ChildReturn`（自动将 `summary + evidenceIds` 组装为 `findings`，缺省 `status` 按 `gaps` 自动推导），保证 `goal/handoff.py` 的证据归属校验与长任务 `Ledger` 归并 100% 兼容。",
      ],
      before: originals.report_to_parent,
      buildAfter() {
        return slimTool(
          "report_to_parent",
          "子任务调查结束时向主 Agent 交回最终结论并终止当前子任务（仅在完成或确认受阻时调用一次）。",
          ["summary"],
          {
            summary: {
              type: "string",
              description: "面向主 Agent 的结构化调查结论与关键数值摘要（Markdown）。",
            },
            evidenceIds: {
              type: "array",
              items: { type: "string" },
              description: "支撑上述结论的本子任务工具回执 evidenceId 列表。",
            },
            gaps: {
              type: "array",
              items: { type: "string" },
              description: "受权限或数据限制未能核实的缺口说明；全部完成时省略。",
            },
            status: {
              type: "string",
              enum: ["completed", "partial", "blocked"],
              description:
                "子任务完成态：全部完成传 completed，存在缺口传 partial，权限或数据受阻传 blocked；省略时按 gaps 自动推导。",
            },
          },
        );
      },
    },

    // ==================== CHANNEL 1B: ALWAYS ON (DATAAGENT CORE: DATA, DOCS, SCHEDULES) ====================
    {
      index: "04",
      group: "通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）",
      name: "search_semantics",
      isStaticCatalog: true,
      category: "business",
      domain: "semantic_read",
      channel: "always_on（启用语义层时所有主/子任务工作阶段默认常驻）",
      policy: "read_only · 免审批 · 子 Agent 可继承",
      problems: [
        "【病灶 2/4】暴露组织权限范围 UUID `scope_filter`、冗余 `domain` 及含 `{const: 0}` 内部补丁的 `cursor`；6 个参数 `description` 全空。",
        "【病灶 7】未声明预注入上下文短路规则，模型即使已在 `publishedSemanticRegistry` 看到目标指标仍惯性重复搜索。",
      ],
      solution: [
        "删除确定性范围字段 `scope_filter`、合并 `domain` 入 `query`、固化单页大小，收敛为 **3 个简单参数**并补齐参数约束说明；顶层置顶预注入命中禁调规则。",
      ],
      before: originals.search_semantics,
      buildAfter() {
        return slimTool(
          "search_semantics",
          "搜索已发布的指标、维度、实体与术语（预注入 publishedSemanticRegistry 已含目标指标时禁调，直接用 resolve_semantics 或 query_metric）。",
          ["query"],
          {
            query: {
              type: "string",
              description: "名称、别名或业务域关键词。",
            },
            kind: {
              type: "string",
              enum: ["metric", "dimension", "entity", "term"],
              description: "资产类别；省略查全部。",
            },
            cursor: {
              type: "string",
              description: "翻页传上一页 next_cursor；首页省略。",
            },
          },
        );
      },
    },
    {
      index: "05",
      group: "通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）",
      name: "get_metric_definition",
      isStaticCatalog: true,
      category: "business",
      domain: "semantic_read",
      channel: "always_on（启用语义层时默认常驻，SC-21 口径解释主力工具）",
      policy: "read_only · 免审批 · 子 Agent 可继承",
      problems: [
        "【病灶 2/3/7】暴露日常问答无需模型指定的 `version_id`；顶层仅 1 句笼统说明，未与 `resolve_semantics` 划清触发边界，导致常规查数多跑一轮定义读取。",
      ],
      solution: [
        "删除 `version_id`（服务端确定性读取当前生效发布版），仅保留**唯一必填参数 `asset_id`**；顶层明确仅用于纯口径问答或核查例外规则。",
      ],
      before: originals.get_metric_definition,
      buildAfter() {
        return slimTool(
          "get_metric_definition",
          "读取指标计算公式与排除规则（仅口径解释或核对例外时用；常规查数直接调 resolve_semantics）。",
          ["asset_id"],
          {
            asset_id: {
              type: "string",
              description: "已发布指标资产 ID。",
            },
          },
        );
      },
    },
    {
      index: "06",
      group: "通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）",
      name: "resolve_semantics",
      isStaticCatalog: true,
      category: "business",
      domain: "semantic_query",
      channel: "always_on（启用语义层时默认常驻；SC-21 纯定义场景由 SOP 约束免多余取数）",
      policy: "read_only · 歧义时触发口径选择卡 · 子 Agent 可继承（无澄清时）",
      problems: [
        "【病灶 2/5】嵌套 `$defs.Parameters` 暴露了服务端自动填写的 `as_of`，且顶层 `dimensions` 与嵌套 `group_by`、`historical_as_of` 与 `as_of` 双重重复（`2,310 B`，10 个属性仅 1 个英文说明）。",
      ],
      solution: [
        "删除 `$defs.Parameters`、删除确定性 `as_of` 与重复 `dimensions`，拍平为直观顶层参数并逐项写明日期与筛选格式；明确预注入已有 `query_ref` 时直接跳至 `query_metric`。",
      ],
      before: originals.resolve_semantics,
      buildAfter() {
        return slimTool(
          "resolve_semantics",
          "将用户提问的指标、期间与维度条件绑定为受控查询引用 query_ref。若预注入 authorized_resolution 已含匹配 query_ref，禁止重复调用，直接调 query_metric。",
          ["mention"],
          {
            mention: {
              type: "string",
              description: "用户问题中的原始指标名称（如 '净销售额'）。",
            },
            metric_id: {
              type: "string",
              description:
                "已确认的正式指标资产 ID；存在同名多口径歧义时省略以触发用户选择卡。",
            },
            start: {
              type: "string",
              description: "统计起始日期（YYYY-MM-DD，含当日）。",
            },
            end: {
              type: "string",
              description: "统计结束日期（YYYY-MM-DD，含当日）。",
            },
            group_by: {
              type: "string",
              description: "分组拆解维度编码（如 region、channel）；总体汇总省略。",
            },
            filters: {
              type: "object",
              description: "维度等值筛选键值对（如 {\"channel\": \"online\"}）。",
            },
          },
        );
      },
    },
    {
      index: "07",
      group: "通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）",
      name: "query_metric",
      isStaticCatalog: true,
      category: "business",
      domain: "semantic_query",
      channel: "always_on（启用语义层时默认常驻，正式指标取数首选）",
      policy: "read_only · 免审批 · 子 Agent 可继承",
      problems: [
        "【病灶 1/3/5】把 `query`/`compare`/`drilldown` 做成复合模式，同时挂着与 `query_ref` 重复的 `parameters`、`as_of` 与复杂联合 `parent_group`（`2,507 B`）；顶层描述用近半篇幅解释返回字段正负号和加总算法。",
      ],
      solution: [
        "彻底去复合化：删除 `operation` 枚举、删除重复 `parameters`/`$defs`、清空顶层返回值算法长文，收敛为 **3 个正交参数**（`query_ref`、`compare_to`、`drilldown_from`）——只传 `query_ref` 即查数，传 `compare_to` 即对比，传 `drilldown_from` 即下钻。",
      ],
      before: originals.query_metric,
      buildAfter() {
        return slimTool(
          "query_metric",
          "正式指标取数首选工具：凭 query_ref 执行受控指标查询、同环比对比或维度下钻（有已发布指标绑定时必须优先使用，禁止手写 SQL 替代）。",
          ["query_ref"],
          {
            query_ref: {
              type: "string",
              description: "预注入上下文或 resolve_semantics 签发的查询引用 ID。",
            },
            compare_to: {
              type: "string",
              description:
                "期间对比配置：传 'previous' 自动对比紧邻等长上期，或传 'YYYY-MM-DD..YYYY-MM-DD' 指定对照区间；普通查询省略。",
            },
            drilldown_from: {
              type: "object",
              description:
                "维度下钻的父级筛选键值（从总体汇总下钻传空对象 {}）；不下钻时省略。",
            },
          },
        );
      },
    },
    {
      index: "08",
      group: "通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）",
      name: "database_query",
      isStaticCatalog: true,
      category: "builtin",
      domain: "data_explore",
      channel: "always_on（存在授权数据库连接时所有工作阶段默认常驻）",
      policy: "read_only · 免审批（pglast 只读 AST + 确定性连接路由） · 子 Agent 可继承",
      problems: [
        "【病灶 1】人为封禁 `information_schema` 后包装出 `list_connections`/`list_tables`/`describe_table`/`query` 四个复合子命令（4 个 `$defs`、12 个属性、`2,391 B`、参数说明全空），把 1 条元数据 SQL 能解决的事拆成 4 轮对话。",
        "【病灶 2/6】把确定性数据库连接路由 `connectionId` 甩给模型并诱导先跑无意义的 `list_connections`；未登记函数、越权表与列名错误全部吞成裸词 `invalid_parameters` / `database_query_failed`。",
      ],
      solution: [
        "极致单一原语化：删除全部 `op` 子命令、`schema`、`table`、`offset` 与 `connectionId`，**仅保留唯一 1 个必填参数 `sql`**；后端放开受权限过滤的只读 `information_schema.tables` / `columns` 查询（多连接工作区并行聚合全部授权连接的元数据并附带连接标识，单表业务查询按 `schema.table` 确定性路由连接，冲突时返回结构化引导）。",
        "补齐可靠性与兼容守卫：① 纯 `information_schema` 元数据查询显式标记 `source_ref.kind = \"metadata\"`（避免误判 `transient` 阻塞附件保存）；② SQL 错列（`42703`）通过 `SAVEPOINT` 回滚后再查可见列候选（杜绝 PostgreSQL `25P02` 事务中止）；③ 回执双写 `resultId` 与 `sourceQueryId` 并更新 `fingerprint()` 兼容单参数 `sql`，保持 `python_analysis` 血缘与长任务查询复用；④ 内部适配层同步兼容旧检查点 `op` 回放。",
      ],
      before: originals.database_query,
      buildAfter() {
        return slimTool(
          "database_query",
          "执行只读 PostgreSQL 查询（仅在无 query_metric 正式指标绑定、排查底层异常明细流水或自定义临时口径时使用）。最佳实践：先查 information_schema.tables / information_schema.columns 核实可见表名与字段，再查业务表（所有业务表须带 schema.table 前缀）。",
          ["sql"],
          {
            sql: {
              type: "string",
              description:
                "单条只读 PostgreSQL SELECT 语句（支持查表清单、查字段结构及带 LIMIT/聚合的业务明细查询）。",
            },
          },
        );
      },
    },

    // ==================== CHANNEL 1C: ALWAYS ON (DOCS & SCHEDULE) ====================
    {
      index: "09",
      group: "通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）",
      name: "search_documents",
      isStaticCatalog: true,
      category: "business",
      domain: "knowledge_doc",
      channel: "always_on（启用文档检索时默认常驻，随时支持制度与历史复盘核对）",
      policy: "read_only · 免审批 · 子 Agent 可继承",
      problems: [
        "【病灶 3/6】拆分布尔开关 `historical` 与 `businessDate` 制造出“`historical=true` 必传 `businessDate`”的隐藏条件；`entity` 与 `query` 重叠；顶层混入封版过滤内幕而 5 个参数全无说明。",
      ],
      solution: [
        "删除 `historical` 与 `entity`，归并为 **3 个自明参数**（`query`、`kind`、`businessDate`），在 `businessDate` 参数说明中直接写明“传值即查该历史日生效版本，省略查现行版本”，彻底清零条件逻辑。",
      ],
      before: originals.search_documents,
      buildAfter() {
        return slimTool(
          "search_documents",
          "检索工作区经营制度、历史复盘报告或外部参考资料，返回最相关段落摘要与 ref（正式引用条款或阈值前须调 read_document 核实原文）。",
          ["query"],
          {
            query: {
              type: "string",
              description: "检索关键词（支持政策名、活动代号、实体名或业务术语）。",
            },
            kind: {
              type: "string",
              enum: ["knowledge", "history", "external"],
              description:
                "资料类型：制度规范(knowledge)、历史复盘(history)、外部资料(external)；不确定时省略。",
            },
            businessDate: {
              type: "string",
              description:
                "排查历史活动或旧版制度时填业务发生日（YYYY-MM-DD）；查现行有效版本时省略。",
            },
          },
        );
      },
    },
    {
      index: "10",
      group: "通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）",
      name: "read_document",
      isStaticCatalog: true,
      category: "business",
      domain: "knowledge_doc",
      channel: "always_on（与 search_documents 成对默认常驻）",
      policy: "read_only · 免审批 · 子 Agent 可继承",
      problems: [
        "【病灶 3/4】顶层混入权限复核内部声明，2 个参数无 `description` 且携带冗余 `title`。",
      ],
      solution: [
        "删去后端权限复核废话与 `title`，为 `ref` 与 `offset` 补齐精准参数约束说明，未知 `ref` 运行时引导先调 `search_documents`。",
      ],
      before: originals.read_document,
      buildAfter() {
        return slimTool(
          "read_document",
          "读取本轮 search_documents 命中的文档段落全文或展开相邻上下文段落，用于正式引用前核实完整条款、表头与单位。",
          ["ref"],
          {
            ref: {
              type: "string",
              description: "本轮 search_documents 返回的段落引用 ID。",
            },
            offset: {
              type: "integer",
              enum: [-1, 0, 1],
              description: "段落偏移：0 当前段（默认），-1 上一段，1 下一段。",
            },
          },
        );
      },
    },
    {
      index: "11",
      group: "通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）",
      name: "schedule_read",
      isStaticCatalog: true,
      category: "builtin",
      domain: "automation",
      channel: "always_on（启用定时任务服务时工作阶段默认常驻）",
      policy: "read_only · 免审批 · 子 Agent 可继承",
      problems: [
        "【病灶 1/4】拆成 `list`/`get`/`history` 三个子命令 `$defs`（9 个属性零说明、`1,779 B`），且 `list` 代码人为执行 `item.pop(\"question\")` 删掉分析问题，逼模型查完列表再调一次 `get`。",
      ],
      solution: [
        "归入 `always_on` 定时核心底座，方便用户在任意分析会话中随时查看与追问定时计划；取消 `operation` 子命令分支，收敛为 **2 个可选筛选参数**，一次调用返回完整配置与近期记录。",
      ],
      before: originals.schedule_read,
      buildAfter() {
        return slimTool(
          "schedule_read",
          "查询当前用户的定时分析任务（一次返回完整配置、分析问题与近期触发记录）。调用 schedule_write 修改或删除前须先调用本工具确认目标 scheduleId。",
          [],
          {
            query: {
              type: "string",
              description: "按任务名称或分析问题关键词筛选；省略则列出全部未删除任务。",
            },
            scheduleId: {
              type: "string",
              description: "按任务 ID 精确查看单个任务及执行历史。",
            },
          },
        );
      },
    },
    {
      index: "12",
      group: "通道一：内建默认常驻（always_on · DataAgent 语义、查库、文档与定时核心底座）",
      name: "schedule_write",
      isStaticCatalog: true,
      category: "builtin",
      domain: "automation",
      channel: "always_on（启用定时服务时主任务交互会话默认常驻；定时 Run 与子 Agent 由 Step 1 硬过滤永久剔除）",
      policy: "external_write · 强制所有者人工确认（HITL） · 定时 Run 与子 Agent 永久禁用",
      problems: [
        "【病灶 1/2/4】包含 8 个 `$defs` 共 35 个属性（`4,821 B`，全部零说明）：`Create` 与 `Patch` 全字段镜像重复，且强制模型生成内部模型路由 `MainModelSelection`、`timezone`、`RunConstraints`、`WorkflowAssetRef` 与 `expectedRevision`。",
      ],
      solution: [
        "归入主任务交互会话 `always_on` 底座（用户可在查数后直接自然语言设定时计划，零手动开关摩擦），通过三层安全防线护航：① `ToolPolicy(external_write, approval=required)` 强制弹出人工确认卡；② `ToolGovernance(subagent_allowed=False, scheduler_allowed=False)` 在 Step 1 对子 Agent 与无人值守定时 Run 永久剔除；③ 顶层 `description` 明确仅用户明确请求时调用。",
        "采用**外简内全适配层与并发锁保护**：清零环境配置与镜像 `$defs` 合并为 **5 个直观参数**；服务端在 `prepare_approval` 阶段确定性继承工作区时区与当前会话模型，优先绑定本轮 `schedule_read` 已读 `revision` 冻结入 `PreparedApproval`，恢复执行时严格校验乐观锁防止并发修改静默覆盖。",
      ],
      before: originals.schedule_write,
      buildAfter() {
        return slimTool(
          "schedule_write",
          "仅当用户明确要求创建、修改、启停或删除定时分析计划时调用（常规查数分析禁调；独占调用批次，经所有者确认卡生效，定时运行中禁用）。",
          [],
          {
            scheduleId: {
              type: "string",
              description:
                "修改、启停或删除已有任务时必填目标 ID（先经 schedule_read 确认）；新建任务时省略。",
            },
            name: {
              type: "string",
              description: "定时任务名称（新建时必填，修改时可选）。",
            },
            question: {
              type: "string",
              description: "每次定时触发执行的分析问题（新建时必填，修改时可选）。",
            },
            schedule: {
              type: "string",
              description:
                "触发周期：支持简写 'daily 09:00'、'weekly MON 09:00'（MON..SUN）或五段 Cron（新建时必填）。",
            },
            status: {
              type: "string",
              enum: ["enabled", "paused", "deleted"],
              description:
                "任务状态：默认 enabled；暂停传 paused；软删除传 deleted。",
            },
          },
        );
      },
    },

    // ==================== CHANNEL 2: SCENE ROUTED (DEEP ANALYSIS PACK) ====================
    {
      index: "13",
      group: "通道二：场景分流自动触发（scene_routed · 深度分析增强包）",
      name: "todo_write",
      isStaticCatalog: true,
      category: "builtin",
      domain: "planning",
      channel: "scene_routed（L2/L3 对比拆解/异常诊断/报告场景或 plan=required 时自动装载）",
      policy: "read_only · 免审批 · 主 Agent 专属",
      problems: [
        "【病灶 1/2/4】为 3–6 行单轮待办设计了 `replace`/`select`/`complete`/`skip` 四个子命令 + DAG 拓扑依赖状态机（6 个 `$defs`、19 个属性、`2,837 B`，含空定义 `SkipReason: {}`），19 个参数零说明。",
        "【病灶 2/4】强制每步手算 `baseVersion` 并对 `todoId` 强卡 `^t[1-9]...` 正则，频繁触发状态迁移报错。",
      ],
      solution: [
        "彻底去复合化：砍掉全部 `op` 子命令、`baseVersion`、`todoId` 正则与 DAG 状态机，收敛为**唯一 1 个幂等全量数组参数 `todos`**；留在 `scene_routed` 避免 L1 单步快查过度规划。",
        "采用**外简内全适配层**：服务端将 `todos[]` 无损映射回既有 `ExecutionPlan`（自动分配稳定 ID `t1..tn`、`done→verified`、首个 `in_progress→selectedTodoId`、自动递增版本并发送 `RunAutonomyAction`），前端「分析计划」进度环与历史 `op` 检查点回放 100% 兼容。",
      ],
      before: originals.todo_write,
      buildAfter() {
        return slimTool(
          "todo_write",
          "维护单轮多步分析清单（3..6 步复杂分析时用；1..2 步快查勿调，跨轮长任务用 goal）。",
          ["todos"],
          {
            todos: {
              type: "array",
              description: "完整步骤列表（2..6 项），每次全量覆盖最新状态。",
              items: {
                type: "object",
                additionalProperties: false,
                required: ["step", "status"],
                properties: {
                  step: {
                    type: "string",
                    description: "本步具体子问题或动作。",
                  },
                  status: {
                    type: "string",
                    enum: ["pending", "in_progress", "done", "skipped"],
                    description: "步骤状态（保持 1 项 in_progress）。",
                  },
                },
              },
            },
          },
        );
      },
    },
    {
      index: "14",
      group: "通道二：场景分流自动触发（scene_routed · 深度分析增强包）",
      name: "python_analysis",
      isStaticCatalog: true,
      category: "builtin",
      domain: "compute_chart",
      channel: "scene_routed（启用沙箱时在诊断/预测/清洗/图表/实验/报告与 SC-34 多轮承接场景装载，亦支持前端手动勾选）",
      policy: "local_artifact（生成 PNG 附件） · 免审批 · 主 Agent 专属",
      problems: [
        "【病灶 3/7】2 个参数均无 `description`，未说明 `source_query_id` 与上游查询回执 `resultId` 的对应关系，也未交代预置 `data` 字典结构与 `128 KiB` 输入上限。",
      ],
      solution: [
        "顶层说明聚焦何时使用 Python 沙箱，将 `resultId` 来源与 `data={\"columns\":[],\"rows\":[[]]}`、`result`、`/workspace/chart.png`、`<=128KiB` 约束精准落入两个参数的 `description` 中。",
      ],
      before: originals.python_analysis,
      buildAfter() {
        return slimTool(
          "python_analysis",
          "在隔离沙箱做统计检验或绘图（简单加总优先用 query_metric 或 SQL）。",
          ["source_query_id", "code"],
          {
            source_query_id: {
              type: "string",
              description: "上游查询回执结果 ID（数据 <=128KiB）。",
            },
            code: {
              type: "string",
              description:
                "Python 代码：读 data={\"columns\":[],\"rows\":[[]]}，结果赋给 result，图存 /workspace/chart.png。",
            },
          },
        );
      },
    },
    {
      index: "15",
      group: "通道二：场景分流自动触发（scene_routed · 深度分析增强包）",
      name: "sub_agent",
      isStaticCatalog: true,
      category: "builtin",
      domain: "orchestration",
      channel: "scene_routed（L3 多源异常诊断 SC-03/04/09/19/20、全景报告 SC-26 与复合任务 SC-40 装载）",
      policy: "read_only · 免审批（子进程只读隔离 + 继承父级授权） · 主 Agent 专属",
      problems: [
        "【病灶 2/5/6】顶层混放旧 `prompt` 与 `tasks` 且无 `required`；把确定性继承的数据库连接 `connectionIds` UUID 数组、只读 `tools` 白名单、步数 `maxSteps` 和 `InputRef.kind` 分类全甩给模型填写（`3,291 B`，12 个属性零说明）。",
      ],
      solution: [
        "彻底剥离 `connectionIds`、`tools`、`maxSteps`、`InputRef.kind` 与遗留 `prompt`，仅保留**唯一必填数组 `tasks`**，每项仅 `name`、`objective`、`inputRefs` 三个纯业务语义字段，权限与只读工具由 Runtime 100% 确定性继承；留在 `scene_routed` 避免简单提问过度派发子任务。",
        "服务端外简内全适配：对无 `kind` 的 `inputRefs` 按 `parent_evidence → ledger → artifact` 顺序确定性反查并组装标准 `InputRef` 供 `goal/handoff.py` 权限核验与导入，同时兼容历史检查点及存量工作流脚本的旧 `prompt` / `inputs` 载荷。",
      ],
      before: originals.sub_agent,
      buildAfter() {
        return slimTool(
          "sub_agent",
          "将互不依赖的只读调查子问题委派给独立子 Agent 并行探查（同一工具批次仅调用一次，子任务无对话历史且无权执行写操作）。",
          ["tasks"],
          {
            tasks: {
              type: "array",
              description: "并行执行的独立只读子任务列表（1..4 项，name 互不重复）。",
              items: {
                type: "object",
                additionalProperties: false,
                required: ["name", "objective"],
                properties: {
                  name: {
                    type: "string",
                    description: "子任务短标题（用于界面卡片展示，<=16 字）。",
                  },
                  objective: {
                    type: "string",
                    description:
                      "独立可执行的调查问题、统计期间、口径约束与期望交回的指标结论。",
                  },
                  inputRefs: {
                    type: "array",
                    items: { type: "string" },
                    description:
                      "按引用传入父任务已取得的 evidenceId、resultId 或账本条目 ID。",
                  },
                },
              },
            },
          },
        );
      },
    },

    // ==================== CHANNEL 3: USER SELECTED (+ EXPLICIT ADMIN SCENE FALLBACK) ====================
    {
      index: "16",
      group: "通道三：前端用户手动选择装载（user_selected · 高阶模式、口径治理与个人扩展）",
      name: "goal",
      isStaticCatalog: true,
      category: "builtin",
      domain: "long_task",
      channel: "user_selected（前端开启「长任务目标」模式或会话已有活动 Goal / SC-40 时装载）",
      policy: "read_only（propose 需所有者确认） · 主 Agent 专属",
      problems: [
        "【病灶 1/2/3/4】全目录最重型双层复合工具（`6,044 B`、8 个 `$defs`、31 个属性全部零说明、顶层内嵌整张 Markdown 表）：混入微美元 `costMicrousd`、`goalId`、`expectedRevision`、英文正则 `key`/`factKey`，且在日常短问答全程陪跑。",
      ],
      solution: [
        "纳入前端「长任务目标」开关控制（有未完结活动 Goal 或 `SC-40` 时自动续挂）；砍掉内部计费/版本号/键名正则及重复的待办子类型，拍平为 **6 个清晰参数**（增加轻量整型数组 `acceptanceIndexes` 标注达标验收项序号）。",
        "采用**外简内全适配层**：服务端自动为 `propose` 的验收标准编号 `a1..aN` 并补齐默认预算与版本，将 `record` 的 `acceptanceIndexes`（如 `[1,2]`）映射为 `LedgerItem.acceptanceIds` 与合法 `key`，`complete` 自动带入当前 `goal.revision`；既有 `GoalController`、`Ledger`、`gate.py` 程序化完成门禁与前端目标面板 100% 兼容。",
      ],
      before: originals.goal,
      buildAfter() {
        return slimTool(
          "goal",
          "管理跨多轮持续推进的复杂长任务目标与结论账本（单轮内多步分析用 todo_write，常规短问答勿调）。",
          ["action"],
          {
            action: {
              type: "string",
              enum: ["propose", "record", "complete", "block"],
              description:
                "动作：propose 立项待用户确认；record 记阶段结论或受阻尝试；complete 全部达标后申请核验；block 缺外部决策或权限时挂起。",
            },
            objective: {
              type: "string",
              description: "action=propose 时的长任务总目标；action=block 时填阻塞原因。",
            },
            acceptance: {
              type: "array",
              items: { type: "string" },
              description: "action=propose 时的可核验完成标准列表（1..5 项）。",
            },
            finding: {
              type: "string",
              description: "action=record 时写入跨轮账本的阶段结论或失败原因摘要。",
            },
            evidenceIds: {
              type: "array",
              items: { type: "string" },
              description: "action=record 支撑该结论的本轮工具 evidenceId 列表。",
            },
            acceptanceIndexes: {
              type: "array",
              items: { type: "integer" },
              description:
                "action=record 时本结论已满足的验收项序号（1 起始，对应立项列表顺序）；未满足任何验收项时省略。",
            },
          },
        );
      },
    },
    {
      index: "17",
      group: "通道三：前端用户手动选择装载（user_selected · 高阶模式、口径治理与个人扩展）",
      name: "create_workflow",
      isStaticCatalog: true,
      category: "builtin",
      domain: "orchestration",
      channel: "user_selected（前端勾选「工作流编排」能力组或命中 SC-29/30 自动化场景装载）",
      policy: "read_only（试跑通过后弹出保存邀请卡） · 主 Agent 专属",
      problems: [
        "【病灶 6/7】跨工具文档腐化：描述仍在教模型写已淘汰的 `agent(\"sub_agent\", {prompt})` 与 `{answer}`；5 个参数中 3 个无说明，失败统一报笼统 `invalid_workflow_script`；普通查数全程暴露。",
      ],
      solution: [
        "归入「工作流编排」手动选择包（`SC-29/30` 自动兜底开启）；修正子任务调用约定为最新 `{tasks: [...]}` 协议，并**同步升级 `workflows/engine.py`** 双向兼容 `{tasks: [...]}` 与历史存量工作流脚本的 `{prompt: \"...\"}` 调用，服务端拆分四段定位错误码。",
      ],
      before: originals.create_workflow,
      buildAfter() {
        return slimTool(
          "create_workflow",
          "当用户明确要求将已验证的分析流程沉淀为可复用工作流模板时，提交入参定义与异步编排脚本并用本次参数即时试跑（试跑通过后向用户弹出保存确认卡）。",
          ["name", "description", "parameters", "scriptSource", "args"],
          {
            name: {
              type: "string",
              description: "工作流模板名称（<=80 字）。",
            },
            description: {
              type: "string",
              description: "工作流适用场景与输出摘要说明。",
            },
            parameters: {
              type: "object",
              description:
                "工作流入参 JSON Schema（顶层须包含 type='object' 与 properties 字段定义）。",
            },
            scriptSource: {
              type: "string",
              description:
                "JS 异步函数体：从 args.<字段> 读入参，调用 await agent('sub_agent', {tasks:[...]}) 或 await parallel([...])，以 return {status:'ok', result} 结束。",
            },
            args: {
              type: "object",
              description: "用于本次即时试跑验证的实参对象（须匹配 parameters 定义）。",
            },
          },
        );
      },
    },
    {
      index: "18",
      group: "通道三：前端用户手动选择装载（user_selected · 高阶模式、口径治理与个人扩展）",
      name: "discover_semantic_candidates",
      isStaticCatalog: true,
      category: "business",
      domain: "semantic_govern",
      channel: "user_selected（仅维护者可勾选「指标口径治理」能力组，或 SC-36/38 自动装载）",
      policy: "local_effect · 保存/修订需人工确认卡 · 主 Agent 专属",
      problems: [
        "【病灶 1/2/6】把主动推荐、保存草稿、修订存量指标混入 19 个平铺属性（`3,931 B`，18 处 `anyOf null`），泄漏 `owner_scope_id`、`usage_scope_id`、`timezone` 等确定性范围参数，且在普通只读查数会话全程暴露。",
      ],
      solution: [
        "物理隔离至「指标口径治理」手动选择包（普通查数 100% 不暴露）；砍掉全部组织范围与时区参数，收敛为 **4 个正交扁平参数**（`mode`、`spec`、`resultRef`、`targetMetric`）。",
        "采用**外简内全适配层**：显式保留极简枚举 `mode`（`\"suggest\" | \"save_draft\" | \"revise\"`）无损映射到既有 `semantic/tool.py` 三条路径——`suggest` 走免阻塞推荐，`save_draft` 与 `revise` 分别弹出既有 `semantic_draft` / `semantic_result_choice` 与 `semantic_revision` 人工确认卡。",
      ],
      before: originals.discover_semantic_candidates,
      buildAfter() {
        return slimTool(
          "discover_semantic_candidates",
          "提报新的可复用经营指标草稿、顺带推荐候选指标或修订存量指标定义（仅在维护口径或沉淀高复用指标时调用）。",
          ["spec"],
          {
            mode: {
              type: "string",
              enum: ["suggest", "save_draft", "revise"],
              description:
                "动作模式：suggest 为回答后免弹窗推荐候选；save_draft 弹窗保存新草稿（默认）；revise 弹窗修订存量指标。",
            },
            spec: {
              type: "object",
              description: "拟保存或修订的业务口径定义（修订存量指标时仅填变更字段）。",
              additionalProperties: false,
              properties: {
                name: { type: "string", description: "指标中文业务名称。" },
                summary: {
                  type: "string",
                  description: "统计口径说明（含筛选、时间边界与剔除规则）。",
                },
                formula: {
                  type: "string",
                  description: "可读业务计算公式（如 '净退货额 / 签收净销售额'）。",
                },
                unit: { type: "string", description: "计量单位（如 元、%、单）。" },
                grain: { type: "string", description: "统计粒度（如 订单、门店×日）。" },
              },
            },
            resultRef: {
              type: "string",
              description: "提报新指标草稿时关联的本轮查询结果 ID；指代刚才的查询可省略。",
            },
            targetMetric: {
              type: "string",
              description:
                "mode=revise 时填写目标指标名称、编码或 ID；新建草稿或推荐时省略。",
            },
          },
        );
      },
    },
    {
      index: "19",
      group: "通道三：前端用户手动选择装载（user_selected · 高阶模式、口径治理与个人扩展）",
      name: "review_semantic_change",
      isStaticCatalog: true,
      category: "business",
      domain: "semantic_govern",
      channel: "user_selected（随「指标口径治理」开关、会话已有草稿版本或 SC-36/38 装载）",
      policy: "read_only · 免审批 · 主 Agent 专属",
      problems: [
        "【病灶 3/4/7】原参数零 `description` 且携带冗余 `title`，顶层未交代输出 `digest` 如何衔接下游送审，且在只读查数会话全程陪跑。",
      ],
      solution: [
        "随「指标口径治理」包成组挂载（或本会话已生成草稿后自动续挂）；剥离 `title` 并为唯一参数 `version_id` 补齐来源说明。",
      ],
      before: originals.review_semantic_change,
      buildAfter() {
        return slimTool(
          "review_semantic_change",
          "送审前校验语义草稿公式与冲突（通过后取回执 digest 送审）。",
          ["version_id"],
          {
            version_id: {
              type: "string",
              description: "草稿版本 ID。",
            },
          },
        );
      },
    },
    {
      index: "20",
      group: "通道三：前端用户手动选择装载（user_selected · 高阶模式、口径治理与个人扩展）",
      name: "submit_semantic_change",
      isStaticCatalog: true,
      category: "business",
      domain: "semantic_govern",
      channel: "user_selected（仅维护者勾选「指标口径治理」、草稿已校验通过或 SC-36/38 装载）",
      policy: "local_effect · 强制维护者人工确认（HITL） · 主 Agent 专属",
      problems: [
        "【病灶 2/3/4】强制模型手写随机幂等键 `request_key`（UUID 必填）并暴露底层 `binding_id`，5 个参数全部零 `description`。",
      ],
      solution: [
        "随「指标口径治理」包隔离保护；彻底删除 `request_key`（服务端用 `uuid5` 确定性生成）与 `binding_id`，仅保留 **3 个必填业务参数**。",
      ],
      before: originals.submit_semantic_change,
      buildAfter() {
        return slimTool(
          "submit_semantic_change",
          "将已通过 review_semantic_change 校验无阻断错误的语义草稿提交双人专家评审（生成确认卡经维护者确认后正式送审）。",
          ["version_id", "expected_digest", "note"],
          {
            version_id: {
              type: "string",
              description: "已通过校验的语义草稿版本 ID。",
            },
            expected_digest: {
              type: "string",
              description: "review_semantic_change 校验通过回执返回的 digest 摘要串。",
            },
            note: {
              type: "string",
              description: "提交专家评审的变更原因与业务影响简述（<=160 字）。",
            },
          },
        );
      },
    },
  ];

  return specs.map((spec) => {
    const after = spec.buildAfter(spec.before);
    const beforeBytes = wireBytes(spec.before);
    const afterBytes = wireBytes(after);
    return {
      ...spec,
      after,
      beforeBytes,
      afterBytes,
      beforeTokens: Math.ceil(beforeBytes / 4),
      afterTokens: Math.ceil(afterBytes / 4),
    };
  });
}

function renderBudgetSummaryMarkdown(specs) {
  const staticSpecs = specs.filter((s) => s.isStaticCatalog);
  const subagentSpec = specs.find((s) => s.name === "report_to_parent");

  const totalBeforeStatic = staticSpecs.reduce((a, s) => a + s.beforeBytes, 0);
  const totalAfterStatic = staticSpecs.reduce((a, s) => a + s.afterBytes, 0);

  const alwaysOnNames = new Set([
    "final_answer",
    "read_tool_result",
    "search_semantics",
    "get_metric_definition",
    "resolve_semantics",
    "query_metric",
    "database_query",
    "search_documents",
    "read_document",
    "schedule_read",
    "schedule_write",
  ]);
  const alwaysOnBytes = specs
    .filter((s) => alwaysOnNames.has(s.name))
    .reduce((a, s) => a + s.afterBytes, 0);

  const deepAnalysisNames = new Set([
    ...alwaysOnNames,
    "todo_write",
    "python_analysis",
    "sub_agent",
  ]);
  const deepAfterBytes = specs
    .filter((s) => deepAnalysisNames.has(s.name))
    .reduce((a, s) => a + s.afterBytes, 0);

  const pctAll = Math.round(
    ((totalBeforeStatic - totalAfterStatic) / totalBeforeStatic) * 100,
  );
  const pctAlwaysOn = Math.round(
    ((totalBeforeStatic - alwaysOnBytes) / totalBeforeStatic) * 100,
  );
  const pctDeep = Math.round(
    ((totalBeforeStatic - deepAfterBytes) / totalBeforeStatic) * 100,
  );
  const pctSub = subagentSpec
    ? Math.round(
        ((subagentSpec.beforeBytes - subagentSpec.afterBytes) /
          subagentSpec.beforeBytes) *
          100,
      )
    : 0;

  return `| 对比口径 | 暴露工具数 | 传输紧凑 JSON 字节 (\`json_bytes\`) | 保守估算 Token (\`bytes / 4\`) | \`$defs\` / 参数说明覆盖率 | 相对基线变化 |
| --- | --- | --- | --- | --- | --- |
| **Group A（当前平铺基线）** | 19 个静态工具全平铺 | \`${totalBeforeStatic.toLocaleString()} B\` | \`~${Math.ceil(totalBeforeStatic / 4).toLocaleString()} tok\` | \`43\` 个 \`$defs\` / 仅 \`4.0%\` 含说明 | 基线对照 |
| **Group B（极简契约 + 全平铺）** | 19 个静态工具全平铺 | \`${totalAfterStatic.toLocaleString()} B\` | \`~${Math.ceil(totalAfterStatic / 4).toLocaleString()} tok\` | **\`0\` 个 \`$defs\` / \`100%\` 含说明** | **\`−${pctAll}%\`** |
| **Group C · \`SC-01\` / \`SC-21\` 常规问答（11 常驻底座）** | 11 个核心工具（隔离长任务/编排/口径治理与扩展） | \`${alwaysOnBytes.toLocaleString()} B\` | \`~${Math.ceil(alwaysOnBytes / 4).toLocaleString()} tok\` | **\`0\` 个 \`$defs\` / \`100%\` 含说明** | **\`−${pctAlwaysOn}%\`** |
| **Group C · \`SC-03\` 深度归因（11 常驻 + 3 深度分析）** | 14 个工具（加挂计划/Python/子任务） | \`${deepAfterBytes.toLocaleString()} B\` | \`~${Math.ceil(deepAfterBytes / 4).toLocaleString()} tok\` | **\`0\` 个 \`$defs\` / \`100%\` 含说明** | **\`−${pctDeep}%\`** |
| **子 Agent 专用底座（\`report_to_parent\`）** | 子任务替换 \`final_answer\` | \`${subagentSpec.beforeBytes.toLocaleString()} B\` → \`${subagentSpec.afterBytes.toLocaleString()} B\` | \`~${subagentSpec.beforeTokens} tok\` → \`~${subagentSpec.afterTokens} tok\` | \`4 → 0\` 个 \`$defs\` / \`100%\` 含说明 | **\`−${pctSub}%\`** |`;
}

function renderChannelMatrixMarkdown() {
  return `| 装载通道 | 触发机制 | 包含工具 / 资产范围 | 解决的核心问题 |
| --- | --- | --- | --- |
| **通道一：内建默认常驻（\`always_on\`）** | 在 Step 1 硬权限与能力开关允许前提下，所有主任务 \`work\` 阶段默认常驻（\`delivery_only\` 仅留 \`final_answer\`；子任务替换 \`final_answer\` 为 \`report_to_parent\` 并剔除 \`schedule_write\`；定时 Run 剔除 \`schedule_write\`；纯澄清/拒答 \`SC-33/37/39\` 直通 \`answer_stream\`） | ① **控制底座**：\`final_answer\`、\`read_tool_result\`（子任务：\`report_to_parent\`）<br>② **语义与查库底座**：\`search_semantics\`、\`get_metric_definition\`、\`resolve_semantics\`、\`query_metric\`、\`database_query\`<br>③ **资料核对底座**：\`search_documents\`、\`read_document\`<br>④ **定时任务底座**：\`schedule_read\`、\`schedule_write\`（强制 HITL 确认） | 将 DataAgent 高频查口径、查指标、查库、核制度与设定时全部纳入稳定底座（11 工具仅 \`~8.0 KB\`），消除分类器误判死胡同并最大化跨轮 Prefix Cache 稳定复用 |
| **通道二：场景分流触发（\`scene_routed\`）** | 联动第 21 课意图路由（\`SC-01..40\`），在 \`identify -> work\` 切换时按任务复杂度自动挂载深度分析增强包（\`SC-34\` 多轮承接自动补挂 \`python_analysis\`） | **深度分析增强包（\`deep_analysis_pack\`）**：\`todo_write\`（L2/L3 或 \`plan=required\`）、\`python_analysis\`（归因/预测/绘图/清洗/报告及 \`SC-34\`）、\`sub_agent\`（L3 多源诊断/全景报告/复合任务） | 在简单快查与口径解释中隐藏重型规划与子任务派发工具，杜绝模型过度规划或滥用子 Agent 拖慢响应，复杂分析时零配置自动配齐 |
| **通道三：前端用户手动选择（\`user_selected\`）** | 用户在 Chat 输入框工具栏「工具与扩展」浮层显式勾选（随 \`RunConstraints.toolSelection\` 提交并自动继承至长任务多轮；特定显式管理场景 \`SC-29/30/36/38/40\` 或已有未完结状态自动兜底） | ① **长任务模式**：\`goal\`<br>② **工作流编排**：\`create_workflow\`<br>③ **指标口径治理组**（仅维护者可见）：\`discover_semantic_candidates\`、\`review_semantic_change\`、\`submit_semantic_change\`<br>④ **个人工作流与外部扩展**：用户勾选的 \`workflow_*\` 与 \`mcp_*\` 连接（未勾选外部 MCP 跳过启动期探测） | 将重型跨轮状态机、工作流脚本沉淀、语义治理写操作及数量无上限的自定义 \`workflow_*\` / \`mcp_*\` 交由用户显式装载，严控单次 \`<= 32\` 槽位（未勾选项留作下一课 \`ToolSearch\` 延迟池） |`;
}

function renderToolSectionMarkdown(specs) {
  const lines = [];
  let currentGroup = "";
  for (const spec of specs) {
    if (spec.group !== currentGroup) {
      currentGroup = spec.group;
      lines.push(`### ${currentGroup}`, "");
    }
    const pct = Math.round(
      ((spec.beforeBytes - spec.afterBytes) / spec.beforeBytes) * 100,
    );
    const pctStr = pct >= 0 ? `−${pct}%` : `+${Math.abs(pct)}%`;
    const deltaText = `\`${spec.beforeBytes.toLocaleString()} B (~${spec.beforeTokens} tok)\` → \`${spec.afterBytes.toLocaleString()} B (~${spec.afterTokens} tok)\`，变化 **\`${pctStr}\`**`;
    lines.push(
      `#### ${spec.index} · \`${spec.name}\`（${spec.category} · \`${spec.domain}\`）`,
      "",
      `- **装载通道**：**${spec.channel}**`,
      `- **执行边界与声明开销**：${spec.policy} ｜ 紧凑声明体积：${deltaText}`,
      `- **诊断报告命中的原始病灶**：`,
      ...spec.problems.map((p, idx) => `  ${idx + 1}. ${p}`),
      `- **重构与装载方案**：`,
      ...spec.solution.map((s, idx) => `  ${idx + 1}. ${s}`),
      "",
      `\`\`\`json [BEFORE] ${spec.name} · 改造前原始声明`,
      formatSchemaJson(spec.before),
      "```",
      "",
      `\`\`\`json [AFTER] ${spec.name} · 改造后极简单一职责契约`,
      formatSchemaJson(spec.after),
      "```",
      "",
    );
  }
  return lines.join("\n");
}

async function main() {
  const specs = await loadAllToolSpecs();
  const budgetTable = renderBudgetSummaryMarkdown(specs);
  const channelMatrix = renderChannelMatrixMarkdown();
  const toolSections = renderToolSectionMarkdown(specs);

  const staticSpecs = specs.filter((s) => s.isStaticCatalog);
  const totalBeforeStatic = staticSpecs.reduce((a, s) => a + s.beforeBytes, 0);
  const totalAfterStatic = staticSpecs.reduce((a, s) => a + s.afterBytes, 0);
  const pctAll = Math.round(
    ((totalBeforeStatic - totalAfterStatic) / totalBeforeStatic) * 100,
  );
  const alwaysOnNames = new Set([
    "final_answer",
    "read_tool_result",
    "search_semantics",
    "get_metric_definition",
    "resolve_semantics",
    "query_metric",
    "database_query",
    "search_documents",
    "read_document",
    "schedule_read",
    "schedule_write",
  ]);
  const l1AfterBytes = specs
    .filter((s) => alwaysOnNames.has(s.name))
    .reduce((a, s) => a + s.afterBytes, 0);
  const pctL1 = Math.round(
    ((totalBeforeStatic - l1AfterBytes) / totalBeforeStatic) * 100,
  );

  // 1. Write standalone Markdown source: design/TOOL_CONTRACTS.md
  const contractsMd = `# 第 27 课｜全量 20 个工具极简 ACI 契约重构、三通道装载分类与 Schema 对照详单

> 状态：**设计示意（与 TECH_DESIGN.md / TECH_DESIGN.html 同源，严格对齐 [TOOL_DIAGNOSIS_REPORT.md](./TOOL_DIAGNOSIS_REPORT.md)）** | 关联主方案：[TECH_DESIGN.md](./TECH_DESIGN.md) | 需求来源：[spec/PRD.md](../spec/PRD.md)

[返回技术方案主文](./TECH_DESIGN.md) · [查看问题诊断报告](./TOOL_DIAGNOSIS_REPORT.md) · [返回需求简报](../BRIEF.md)

本文档依据 [` + "`TOOL_DIAGNOSIS_REPORT.md`" + `](./TOOL_DIAGNOSIS_REPORT.md) 的五条工程标尺，按**三大确定性装载通道（内建默认常驻 \`always_on\`、场景分流触发 \`scene_routed\`、前端用户手动选择 \`user_selected\`）**分组，完整呈现 Compass 仓库现有全部 **20 个工具声明**（19 个静态注册工具 + 子任务专用完成工具 \`report_to_parent\`）的重构理由与前后 JSON Schema 逐行对比（模型主动 \`ToolSearch\` 机制明确划分至下一课实现）。

## 三大工具装载通道全景矩阵

${channelMatrix}

## 声明体积与结构精简实测总账

${budgetTable}

## 按三通道分组的逐工具 ACI 诊断与 Schema 前后对照（01–20）

${toolSections}`;

  await fs.writeFile(
    path.resolve(__dirname, "../TOOL_CONTRACTS.md"),
    contractsMd,
    "utf8",
  );

  // 2. Write guide.md for TECH_DESIGN.html (no external .md file links per renderReview rule)
  const guideBase = `## 设计目标与范围切分

本方案基于工具定义问题深度诊断报告梳理的七大病灶，在本课完成两件核心工程建设：① 按**去复合化回归通用原语、代码确定性清零机械参数、顶层与参数 \`description\` 各司其职、流式循证收口**重构现有全部 **20 个工具契约**（19 个静态工具 + 子任务 \`report_to_parent\`）；② 建立**“内建默认常驻（\`always_on\`）+ 场景路由分流（\`scene_routed\`）+ 前端用户手动选择（\`user_selected\`）”三通道确定性工具组装机制**与 Tool Eval 六维基准套件。模型侧主动 \`ToolSearch\` 搜索延迟工具机制明确留待下一课随自定义插件体系一并建设。

| 边界与不变量 | 设计口径（作用域） | 说明 |
| --- | --- | --- |
| Run 授权工具池上限 | \`<= 128\` 个 / 单次 Run | 承载全部静态工具、个人工作流与已选 MCP 连接，固化入 \`AgentToolBinding\`（未勾选 MCP 跳过启动期探测） |
| 单步模型可见工具上限 | \`<= 32\` 个且 \`<= 64 KiB\` / 单次逻辑模型调用 | 三通道合流后强制守卫，前端扩展选择器同步限制勾选上限 |
| 静态工具声明精简实测 | 19 工具合计 \`${totalAfterStatic.toLocaleString()} B\`（原 \`${totalBeforeStatic.toLocaleString()} B\`） | 清零 \`47\` 个 \`$defs\` 与全部 \`connectionId\`/\`request_key\` 等机械参数，参数说明覆盖率 \`4% → 100%\`，体积直降 **\`−${pctAll}%\`** |
| 常规快查单步声明开销 | \`SC-01\` / \`SC-21\` 装载 11 个常驻核心工具仅 \`${l1AfterBytes.toLocaleString()} B (~${Math.ceil(l1AfterBytes / 4).toLocaleString()} tok)\` | 相对基线单步 \`42,719 B (~10,680 tok)\` 压降 **\`−${pctL1}%\`**，隔离长任务/编排/口径治理与未选扩展，跨轮前缀 100% 稳定 |
| 前缀缓存不变量 | 单次 Run 内 \`work\` 阶段工具序列 **\`100%\` 恒定不变** | 在 \`identify -> work\` 入口冻结会话标志并一次性合流，全程不断 \`continuation_token\`、零缓存击穿 |
| 协议与权限隔离不变量 | 新 Run 启用 \`answerProtocol == 3\`；前端越权泄露数 \`= 0\` | \`RunConstraints.toolSelection\` 仅在服务端 \`AuthorizedPool\` 内取硬交集（含 \`semantic_maintainer\` 能力透传）；存量 \`answerProtocol == 2\` Run 保持兼容隔离 |

## 总体方案：三通道确定性工具组装架构

系统沿单次对话请求主链路分为四层：单一原语极简契约与确定性适配层、三通道确定性工具组装层（常驻 + 场景分流 + 前端手动选择）、模型推理与流式收口执行层、ContextOps 观测与 Tool Eval 评测层。\`runtime/\`、\`harness/\` 与 \`tools/builtin/\` 不直接导入 \`compass_agent.tools.business\`，全部业务工具装载属性通过通用 \`ToolGovernance\` 契约声明并由 \`ToolAssembler\` 统一消费。

\`\`\`mermaid 总体架构与三通道工具组装流
flowchart TB
    subgraph L1["1. 极简单一原语契约与确定性适配层"]
        SPEC["ToolSpec (catalog.py)<br/>零 $defs 扁平 Schema + 100% 参数说明 + ToolGovernance"]
        ADAPT["Deterministic Adapter<br/>自动路由 connectionId / 绑定版本号 / 时区 / 幂等键"]
        POPS["PromptOps JSON (prompts/tools/*.json)<br/>用途时机说明书 + 参数级简短约束"]
        SPEC <-->|"双向结构校验"| POPS
        SPEC --> ADAPT
    end

    subgraph L2["2. 三通道确定性组装层 (ToolAssembler)"]
        POOL["AuthorizedPool (<= 128)<br/>租户开关 + 成员权限 + 连接 + 角色硬过滤"]
        CH1["通道一：always_on 常驻核心底座 (11 工具)<br/>控制 + 语义/SQL + 文档核对 + 定时读写(HITL)"]
        CH2["通道二：scene_routed 深度分析增强包 (3 工具)<br/>todo_write / python_analysis / sub_agent"]
        CH3["通道三：user_selected 前端手动选择<br/>长任务 / 工作流编排 / 口径治理 / 个人 workflow_* & mcp_*"]
        ASM["三通道交集合流与固定排序<br/>Mounted <= 32 且 <= 64 KiB (全程静态保序)"]
        POOL --> CH1 & CH2 & CH3 --> ASM
    end

    subgraph L3["3. 模型循环与流式循证收口层"]
        PIPE["prepare_input / answer_turn<br/>恒定前缀 tools[] (100% Prefix Cache 友好)"]
        DISP["ToolDispatcher + ActionableToolError<br/>visible_names 门禁 + 字段/可见列修复引导"]
        FIN["final_answer(mode) 末位独占触发<br/>plain / evidence 双独立 Prompt 实时流式推送 -> 强制 stop"]
        ASM --> PIPE --> DISP --> FIN
        ADAPT --> DISP
    end

    subgraph L4["4. 观测与效果治理层"]
        TRACE["ContextOps 轨迹面板<br/>区分 [常驻] / [场景] / [手动] 装载来源与 Token 占比"]
        EVAL["tests/tool_eval/<br/>A 基线 / B 极简契约 / C 三通道动态组装"]
        PIPE --> TRACE
        L3 --> EVAL
    end

    L1 --> POOL
\`\`\`

### 三大装载通道划分准则与工具归属

${channelMatrix}

### 场景分流（\`scene_routed\`）与前端手动选择（\`user_selected\`）协同规则

\`\`\`mermaid 三通道决策与阶段状态流转
flowchart LR
    U["用户提交提问 + 可选 toolSelection"] --> R["Identify 意图路由 (tools = [])"]
    R --> P["Step 1: 构建 AuthorizedPool (硬权限 + 连接 + 身份过滤)"]
    P --> M["Step 2: Work 阶段三通道合流<br/>AlwaysOn(11) ∪ UserSelected ∪ SceneRouted(SC-XX)"]
    M --> W["Work 循环 (tools 序列恒定不变)"]
    W -->|"触及步数/Token预留阈值"| D["DeliveryOnly (tools = [final_answer])"]
    W -->|"末位单独调用 final_answer(mode)"| S["AnswerStream (tools = [])<br/>排序证据嵌入 + ^[1]^ 角标流式直出"]
    D --> S
    S --> STOP["流式完成 -> 强制 stop"]
\`\`\`

## 核心对象建模与存储复用

### 三层 ToolSpec 与三通道治理标签

\`ToolGovernance\` 通过 \`channel: Literal["always_on", "scene_routed", "user_selected"]\` 与 \`pack\` 标签将全部静态与扩展工具纳入统一三通道模型，前端提交的 \`ToolSelection\` 仅表达用户本轮启用意愿，最终装载列表始终受服务端 \`AuthorizedPool\` 硬权限约束：

{{code:tool_governance}}

### 持久化与现有数据库表复用

本方案不新增数据库表或 Alembic 迁移：\`AgentToolBinding\` 冻结当前 Run 的授权池快照，用户手动选择的 \`toolSelection\` 随 \`RunConstraints\` / \`context_binding\` 记录，Step 级三通道组装明细直接写入既有 \`context_snapshot\` 与 \`context_decision_summary\` JSONB：

\`\`\`mermaid 存储复用关系与基数
erDiagram
    AGENT_RUN ||--o{ AGENT_TOOL_BINDING : "1:N 冻结授权池与绑定快照 (<=128)"
    AGENT_RUN ||--o{ CONTEXT_SNAPSHOT : "1:N 加密保存 receipt.toolAssembly"
    AGENT_RUN ||--o{ CONTEXT_DECISION_SUMMARY : "1:N JSONB 投影三通道组装统计"

    AGENT_TOOL_BINDING {
        uuid id PK "绑定记录主键"
        uuid run_id FK "所属运行 ID"
        string alias "工具唯一名称"
        jsonb declaration "冻结的工具 JSON Schema 声明"
        jsonb binding "执行环境与 catalogOrdinal 序号"
        jsonb policy "effect 与 approval 策略快照"
    }
    CONTEXT_DECISION_SUMMARY {
        uuid id PK "编译步骤摘要主键"
        uuid run_id "所属运行 ID"
        uuid step_id "所属模型步骤 ID"
        string scene_id "命中的业务场景编号"
        string phase "编译阶段"
        jsonb decisions_json "三通道工具装载与排除摘要"
    }
\`\`\`

\`ToolRegistry\` 拆分授权池容量（\`128\`）与单步模型可见上限（\`32\` 个且 \`<= 64 KiB\`）：

{{code:registry_limits}}

## 逐工具极简 ACI 重构、装载通道与 Schema Diff 详单（01–20）

本节按**通道一（内建默认常驻）、通道二（场景分流触发）、通道三（前端用户手动选择）**三大分组，完整呈现 **19 个静态工具 + 子任务 \`report_to_parent\`（共 20 个工具）** 的装载通道、诊断病灶、重构方案，以及改造前（左栏：仓库当前真实声明）与改造后（右栏：极简单一职责契约）的深色代码块逐行 Diff 对照。

${budgetTable}

${toolSections}
## 核心模块详解

### 三通道确定性组装器（\`ToolAssembler\`）

\`assemble_step()\` 按 \`硬权限与身份过滤 -> 阶段收敛门禁 -> AlwaysOn + UserSelected + SceneRouted 三通道确定性合流 -> 32 槽位 / 64 KiB 守卫\` 执行。用户手动勾选的扩展工具与高阶能力组排在底座之后优先保障槽位，场景自动触发工具按固定目录序号排列，且整个 Run 工作阶段内工具顺序完全静态，100% 保护上游模型 Prefix Cache：

{{code:tool_assembler}}

### 确定性连接路由、流式循证收口与外简内全适配

服务端在 \`schema_compact.py\`、\`answer_turn.py\` 与各工具适配层落实“外简内全”工程确定性：① \`database_query(sql)\` 支持多连接元数据聚合并标记 \`kind: "metadata"\`、按 \`schema.table\` 确定性路由连接、用 \`SAVEPOINT\` 防 PostgreSQL \`25P02\` 事务中止并双写 \`resultId\` / \`sourceQueryId\`；② \`final_answer(mode, artifactFormat?)\` 在 \`answerProtocol == 3\` 下通过延期回执拦截抢跑调用，流式前先过 \`check_access\`，\`mode="evidence"\` 由工程层将验证证据排序过滤编号 \`[1]..[N]\` 注入 \`evidence.md\` 自由流式生成，流式后解析 \`^[1]^\` 精确偏移复用 \`publish_citations\` / \`<CitedAnswer>\` 与 \`build_artifact\` 落盘 \`answer/v3/accepted\` 并强制 \`stop\`；③ \`todo_write\` / \`goal\` / \`report_to_parent\` / \`discover_semantic_candidates\` / \`schedule_write\` / \`workflows/engine.py\` 保持既有后端状态机与前端面板无损兼容：

{{code:slim_adapter}}

{{code:actionable_error}}

{{code:postgres_error}}

## 实现参考与验收矩阵

### 拟变更文件清单

\`\`\`text 变更目录与文件结构
apps/agent/
├── src/compass_agent/
│   ├── schemas.py                                  # [修改] RunConstraints.toolSelection、TOOL_REGISTRY_POOL_LIMIT 与 ToolAssemblyTraceView
│   ├── tools/
│   │   ├── catalog_contracts.py                    # [修改] 三层 ToolSpec、三通道 ToolGovernance 与外简内全适配钩子
│   │   ├── schema_compact.py                       # [新增] 确定性连接路由、延期回执门禁、流式循证角标构建与周期转换
│   │   ├── contracts.py                            # [修改] RegisteredTool 挂载 governance 与资产来源 ID
│   │   ├── registry.py                             # [修改] 解耦授权池上限(128)与单步可见上限(32 / 64KiB)
│   │   ├── assembler.py                            # [新增] 常驻 + 场景分流 + 前端手动选择三通道组装器（冻结会话标志，预留 deferred_pool）
│   │   ├── errors.py                               # [新增] ActionableToolError 封装与参数/SQL 错误定位（含 SAVEPOINT 防 25P02）
│   │   ├── dispatcher.py                           # [修改] 串联外简内全输入适配与结构化错误投影
│   │   ├── builtin/{database_query,python_analysis,read_tool_result,todo_write,goal,sub_agent,schedule_read,schedule_write,create_workflow,final_answer}/__init__.py
│   │   └── business/*/__init__.py                  # [修改] 9 个业务工具极简输入模型、外简内全适配器与三通道 ToolGovernance 标签
│   ├── workflows/engine.py                         # [修改] 双向兼容 {tasks:[...]} 与存量 {prompt:"..."} 工作流子任务调用
│   ├── goal/handoff.py                             # [修改] 无 kind inputRefs 三源反查与 report_to_parent 扁平转 ChildReturn 适配
│   ├── prompts/
│   │   ├── tools/*.json                            # [修改] 19 个静态工具单一原语契约与 100% 参数级简短说明
│   │   ├── answer/{plain.md,evidence.md}           # [新增] 双模式独立流式 Answer Prompt 模板（含 {{ordered_evidences}} 变量）
│   │   └── support/subagent/report-to-parent.json  # [修改] 子任务汇报去复合化极简契约
│   ├── promptops/{catalog.py,structured.py}        # [修改] 登记 answer 模板及白名单变量；对齐极简 Schema 校验并保留历史只读兼容
│   ├── context_runtime/{profiles.py,pipeline.py,model_step.py,citations.py} # [修改] 注册 answer 路径、支持 answer_stream 流式、双格式 ^[1]^/[1] 锚点校验
│   ├── runtime/{tool_bindings.py,answer_turn.py,turn.py,subagent.py,store.py,journal.py,durable_service.py} # [修改] answerProtocol==3 落盘、按需 MCP、冻结快照恢复与 semantic_maintainer 能力透传
│   └── context_ops/{recorder.py,analytics.py}
└── tests/
    └── tool_eval/                                  # [新增] 诊断评测集、六维指标评估器与 A/B/C 消融运行器
apps/web/
├── src/generated/                                  # [生成] pnpm contracts 同步 RunConstraints.toolSelection 与工具组装 Trace 类型
├── src/server/http/                                # [修改] 内部 authz 端点透出 semantic_maintainer 布尔能力位
├── src/components/analysis-composer.tsx            # [修改] 输入框工具栏新增「工具与扩展」手动选择浮层与已选徽标
├── src/components/cited-answer.tsx                 # [修改] 双格式兼容渲染 ^[1]^ 与存量 [1] 引用锚点及详情弹窗
├── src/features/contextops/{flow.tsx,panels.tsx}   # [修改] 工具组装面板展示 [常驻]/[场景]/[手动] 通道来源与隐藏原因
└── src/features/promptops/tool-fields.tsx          # [修改] 工具编辑页只读展示装载通道与治理标签
contracts/                                          # [生成] OpenAPI 与运行时 JSON Schema
\`\`\`

### Tool Eval A/B/C 消融实验设计

| 对照组 | 工具契约与报错回执 | 工具供给策略 | 验证目标 |
| --- | --- | --- | --- |
| **A 组（Baseline）** | 改造前源码声明（19 工具 \`${totalBeforeStatic.toLocaleString()} B\`、43 个 \`$defs\`、96% 参数无说明）+ 裸错误码 | 除 \`identify\` 外全量平铺加载 | 量化当前复合工具多跳拆步、参数踩雷、盲猜重试与高额声明 Token 基线 |
| **B 组（Simple ACI Only）** | 去复合单一原语契约（19 工具仅 \`${totalAfterStatic.toLocaleString()} B\`、100% 参数说明、流式 \`final_answer\`）+ \`ActionableToolError\` | 保持全量平铺加载 | 隔离验证去复合化与确定性下沉对交互轮次缩短、首轮成功率及声明体积直降 \`−${pctAll}%\` 的收益 |
| **C 组（3-Channel Assembly）** | 与 B 组完全相同的极简单一原语契约与自愈报错 | \`ToolAssembler\` 三通道组装（\`always_on\` + \`scene_routed\` + \`user_selected\`） | 验证常规快查声明字节进一步降至 \`${l1AfterBytes.toLocaleString()} B (−${pctL1}%)\`、口径治理写工具物理隔离且非请求写操作零误触发，以及手动选择扩展工作流/MCP 的精准性 |
`;

  await fs.writeFile(path.join(__dirname, "guide.md"), guideBase, "utf8");

  const contractsGuide = `## 三大工具装载通道与极简契约总览

本专题按**通道一（内建默认常驻 \`always_on\`）、通道二（场景分流触发 \`scene_routed\`）、通道三（前端用户手动选择 \`user_selected\`）**三大分组，逐项呈现 Compass 仓库现有全部 **20 个工具声明**（19 个静态工具 + 子任务 \`report_to_parent\`）的装载规则、病灶诊断与左右深色代码块 JSON Schema Diff（\`ToolSearch\` 按需检索机制留待下一课建设）。

${channelMatrix}

## 声明体积与结构精简实测总账

${budgetTable}

## 按三通道分组的逐工具 ACI 诊断与左右深色 Schema Diff（01–20）

${toolSections}`;

  await fs.writeFile(
    path.join(__dirname, "contracts-guide.md"),
    contractsGuide,
    "utf8",
  );

  // 3. Export JSON bundle for verification
  await fs.writeFile(
    path.join(COMPASS_ROOT, "work/970-after-tool-docs.json"),
    JSON.stringify(
      specs.map((s) => ({
        index: s.index,
        name: s.name,
        channel: s.channel,
        isStaticCatalog: s.isStaticCatalog,
        beforeBytes: s.beforeBytes,
        afterBytes: s.afterBytes,
        after: s.after,
      })),
      null,
      2,
    ),
    "utf8",
  );
  console.log(
    `Generated ${specs.length} tool contracts across 3 loading channels successfully.`,
  );
}

if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  await fs.mkdir(path.join(COMPASS_ROOT, "work"), { recursive: true });
  await main();
}
