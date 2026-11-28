# **Claude Code Tool Search 技术原理**

## **一、核心设计理念：工具契约的渐进式披露**

Claude Code 的 Tool Search 本质上是一种**由模型主动驱动的工具发现与延迟加载机制（Model-driven Tool Discovery & Deferred Tool Loading）**。

传统 Agent 在每次调用大模型时，将所有工具的名称、描述和参数 Schema 全量注入上下文。随着 MCP Server、插件及自定义工具的增加，会产生工具声明膨胀、工具选择准确率下降、上下文资源浪费等问题。

Claude Code 的解决思路是将工具的**注册、发现、加载、执行**四个环节解耦。

- **注册（Registration）**：Runtime 持有完整的工具定义与执行器。
- **发现（Discovery）**：模型根据当前任务主动搜索需要的工具。
- **加载（Loading）**：将命中工具的完整调用契约提供给模型。
- **执行（Execution）**：模型生成结构化参数，通过统一执行管线完成调用。

其核心思想是：**工具可以预先注册，但不必提前进入模型上下文；模型需要什么能力，就主动发现并加载什么能力。**

## **二、整体技术架构**

```
内建工具 / MCP Server / 插件
              │
              ▼
       Tool Pool Assembly
       工具注册、过滤、去重
              │
       ┌──────┴──────┐
       ▼             ▼
  Eager Tools    Deferred Tools
  提前加载工具     延迟加载工具
       │             │
       │         可检索工具池
       │             │
       ▼             │
  LLM Context        │
  基础工具            │
  ToolSearch ────────┘
       │
       ▼
  模型主动生成搜索请求
       │
       ▼
  Tool Search Engine
       │
       ▼
  匹配工具 / Tool Reference
       │
       ▼
  展开完整 Tool Schema
       │
       ▼
  模型生成 Tool Call
       │
       ▼
  权限校验 / 参数校验 / 执行
```

整个架构包含五个核心模块：

| 模块              | 核心职责              |
| --------------- | ----------------- |
| Tool Pool       | 维护完整工具池           |
| Loading Policy  | 决定哪些工具提前加载、哪些延迟加载 |
| ToolSearch      | 面向模型的工具发现入口       |
| Search Engine   | 根据搜索请求匹配候选工具      |
| Schema Resolver | 将命中的工具引用解析为完整调用契约 |

上述模块名称是便于理解的架构抽象，并非全部对应 Claude Code 源码中的同名类。

## **三、工具池初始化与分层加载策略**

Claude Code 在初始化会话时，会先组装完整的工具候选池。

根据此前提供的源码分析材料，其内部主要经过以下流程：

```
getAllBaseTools()
        ↓
getTools()
        ↓
assembleToolPool()
        ↓
会话级 / Agent 级过滤
        ↓
toolToAPISchema()
```

其中，环境变量、功能开关、权限规则、Agent 角色、MCP 连接状态共同决定工具池的最终组成。

粘贴的文本 (1)(1).txt



在此基础上，工具的加载方式进一步分为两类：

| 工具分层           | 加载策略            | 典型工具                      |
| -------------- | --------------- | ------------------------- |
| Eager Tools    | 提前展开，模型可以直接调用   | Read、Bash 等基础能力，具体取决于运行配置 |
| Deferred Tools | 保留完整定义，等待模型主动发现 | MCP 工具及其他符合延迟策略的工具        |

`ToolSearch` 自身必须提前提供，否则模型将失去发现其他延迟工具的入口。

### **MCP 工具加载策略**

Claude Code 通过 `ENABLE_TOOL_SEARCH` 控制 MCP 工具的加载策略。

| 配置                              | 加载行为                   |
| ------------------------------- | ---------------------- |
| `true` 或默认配置                    | 支持条件下启用延迟加载            |
| `auto`                          | 工具声明达到上下文窗口的 10% 时启用搜索 |
| `auto:5`                        | 将触发阈值调整为 5%            |
| `auto:20`                       | 将触发阈值调整为 20%           |
| `false`                         | 关闭搜索，提前加载工具定义          |
| MCP Server 的 `alwaysLoad: true` | 对指定服务器采用提前加载           |

需要注意，10% 是 `auto` 模式的默认阈值，而不是所有运行模式统一的启动条件。

## **四、ToolSearch 本身的接口设计**

Claude Code 将工具搜索本身抽象为一个模型可调用的元工具（Meta Tool），即**用于发现其他工具的工具**。

根据提供的 Claude Code 源码快照，其输入参数为：

```
interface ToolSearchInput {
  query: string;
  max_results?: number; // 默认 5
}
```

支持两种调用形式。

粘贴的文本 (1)(1).txt



### **4.1 关键词搜索**

模型知道自己需要什么能力，但不知道具体的工具名称。

例如：

```
{
  "query": "github search issues",
  "max_results": 5
}
```

搜索模块从延迟工具池中寻找相关工具。

可能命中：

```
mcp__github__search_issues
mcp__github__list_issues
mcp__github__get_issue
```

上述工具名称为机制演示，实际名称由 MCP Server 提供。

### **4.2 精确工具选择**

当模型已经知道具体工具名称时，可以通过 `select:` 直接选择，而不需要进行模糊匹配。

```
{
  "query": "select:mcp__github__search_issues"
}
```

这两种方式分别解决不同问题：

| 方式    | 解决的问题                |
| ----- | -------------------- |
| 关键词搜索 | 我需要某种能力，但不知道哪个工具能够提供 |
| 精确选择  | 我已经知道工具名称，需要加载它的完整契约 |

## **五、底层工具检索算法**

这里必须区分 **Claude Code 客户端的&#x20;**`ToolSearch` 和 **Anthropic Messages API 提供的 Tool Search Tool**。

前者是 Agent Harness 的工具发现入口，后者是模型服务平台提供的原生工具检索与契约展开机制。两者设计理念一致，但不能直接认定搜索算法完全相同。

Anthropic API 官方提供两种检索算法。

[image](https://www.google.com/s2/favicons?domain=https://platform.claude.com\&sz=32)

Claude Platform Docs+1



### **5.1 Regex 正则表达式检索**

工具定义：

```
{
  "type": "tool_search_tool_regex_20251119",
  "name": "tool_search_tool_regex"
}
```

模型负责将当前任务中的能力需求转化为正则表达式。

例如：

```
github.*issue|issue.*search
```

检索范围包括工具名称、工具描述、参数名称及参数描述。

底层采用不区分大小写的 Python 正则匹配，搜索表达式最长 200 个字符。

\*\*设计特点：\*\*实现简单、匹配结果可解释，适合命名规范、描述清晰的工具体系。语义泛化能力则相对有限。

### **5.2 BM25 关键词相关性检索**

工具定义：

```
{
  "type": "tool_search_tool_bm25_20251119",
  "name": "tool_search_tool_bm25"
}
```

模型生成自然语言或关键词查询：

```
Search GitHub issues by repository and status
```

检索模块基于 BM25 相关性对工具进行排序，返回最相关的候选工具。

BM25 的主要依据是关键词匹配、词频、逆文档频率及文档长度归一化。它属于稀疏文本检索，不是 Embedding 向量检索。

### **5.3 两种算法的对比**

| 对比维度           | Regex      | BM25          |
| -------------- | ---------- | ------------- |
| 查询形式           | 正则表达式      | 自然语言或关键词      |
| 匹配方式           | 字符模式匹配     | 文本相关性排序       |
| 精确匹配能力         | 强          | 较强            |
| 语义泛化能力         | 有限         | 有限，但更适合自然语言查询 |
| 是否需要 Embedding | 否          | 否             |
| 是否需要向量数据库      | 否          | 否             |
| 适用场景           | 规范命名、已知能力域 | 大规模工具目录检索     |

**值得注意的是，Anthropic 官方提供的这两种原生方案都不依赖向量数据库。** 开发者也可以通过自定义检索器引入 Embedding、混合检索或 Rerank，并复用后续的工具引用展开机制。

现有 Claude Code 源码分析材料能够确认关键词搜索与 `select:` 接口，但不足以确认其客户端检索器的全部打分公式、权重以及排序策略。因此不能直接将 API 的 BM25 实现归因给 Claude Code 客户端。

## **六、Deferred Tool Loading：完整 Schema 如何延迟加载**

这是整个 Tool Search 机制中最重要的底层技术。

在 Anthropic Messages API 中，每个工具可以增加一个加载控制字段：

```
{
  "name": "search_issues",
  "description": "Search issues in a GitHub repository",
  "input_schema": {
    "type": "object",
    "properties": {
      "repository": {
        "type": "string"
      },
      "query": {
        "type": "string"
      }
    },
    "required": ["repository", "query"]
  },
  "defer_loading": true
}
```

其中：

- `defer_loading: false` 或普通非延迟定义：直接展开。
- `defer_loading: true`：保留定义，但初始不展开。

### **请求传输与模型上下文是两个不同层次**

这是一个非常关键的架构设计。

Anthropic API 原生延迟加载模式下，客户端仍然可以在每次 HTTP 请求中发送完整的 `tools[]` 数组。

但是，服务端构造模型输入时，并不会将所有延迟工具都展开到模型上下文中。

[image](https://www.google.com/s2/favicons?domain=https://platform.claude.com\&sz=32)

Claude Platform Docs+1



```
HTTP Request
│
├── Read              完整 Schema
├── Bash              完整 Schema
├── ToolSearch        完整 Schema
├── GitHub Tool 1     defer_loading: true
├── GitHub Tool 2     defer_loading: true
├── Slack Tool 1      defer_loading: true
└── Jira Tool 1       defer_loading: true

               ↓ API Context Assembly

LLM Context
│
├── Read
├── Bash
└── ToolSearch
```

因此，Deferred Loading 优化的是**模型上下文中的工具声明 Token 开销**，而不是必然减少 HTTP 请求体大小。

## **七、Tool Reference：工具如何从搜索结果进入上下文**

当模型调用搜索工具后，搜索模块返回的核心内容不是普通的工具说明文本，而是结构化的 **Tool Reference**。

Anthropic API 支持如下工具引用格式：

```
{
  "type": "tool_reference",
  "tool_name": "search_issues"
}
```

对于自定义工具检索器，可以将其放入标准工具结果中：

```
{
  "type": "tool_result",
  "tool_use_id": "toolu_123",
  "content": [
    {
      "type": "tool_reference",
      "tool_name": "search_issues"
    }
  ]
}
```

模型服务端收到引用后，根据完整工具池解析其名称，展开对应的工具定义。

```
Tool Reference
      │
      ▼
按名称定位完整 Tool Definition
      │
      ▼
展开 name / description / input_schema
      │
      ▼
加入模型后续推理上下文
      │
      ▼
模型获得原生 Function Calling 能力
```

这意味着模型不需要通过普通文本理解一段 JSON Schema，再调用一个万能的 `execute_tool(name, args)` 工具，而是能够继续使用原生结构化工具调用接口。

自定义检索器返回的引用必须对应请求中存在的工具定义，否则无法完成展开。

[image](https://www.google.com/s2/favicons?domain=https://platform.claude.com\&sz=32)

Claude Platform Docs



## **八、端到端运行时执行流程**

假设用户提出任务：

> 查询 GitHub 仓库中所有与 MCP 工具加载相关的未关闭 Issue。

### **Step 1：初始化模型上下文**

假设 Runtime 共注册了 100 个工具，其中 8 个常驻工具，92 个延迟工具。

初始推理时，模型只获得常驻工具的完整定义及必要的能力发现信息。

### **Step 2：模型主动发现能力缺口**

模型判断当前任务需要 GitHub Issue 查询能力，因此调用：

```
{
  "name": "ToolSearch",
  "input": {
    "query": "github search issues",
    "max_results": 3
  }
}
```

### **Step 3：检索候选工具**

搜索模块从延迟工具池中匹配：

```
mcp__github__search_issues
mcp__github__list_issues
mcp__github__get_issue
```

### **Step 4：返回工具引用并展开契约**

```
[
  {
    "type": "tool_reference",
    "tool_name": "mcp__github__search_issues"
  },
  {
    "type": "tool_reference",
    "tool_name": "mcp__github__list_issues"
  },
  {
    "type": "tool_reference",
    "tool_name": "mcp__github__get_issue"
  }
]
```

模型获得三个工具的完整参数定义。

### **Step 5：模型执行真正的业务工具调用**

```
{
  "name": "mcp__github__search_issues",
  "input": {
    "query": "repo:example/project is:issue is:open MCP loading"
  }
}
```

### **Step 6：Runtime 执行与结果返回**

Claude Code 进入正常的工具执行管线：

```
定位工具实例
     ↓
Schema 参数解析
     ↓
业务参数校验
     ↓
PreToolUse Hook
     ↓
权限检查
     ↓
MCP tools/call
     ↓
结果转换
     ↓
PostToolUse Hook
     ↓
返回 Tool Result
```

这条执行链在此前提供的源码分析中已有明确描述。

粘贴的文本 (1)(1).txt



如果任务还需要 Slack、Jira 或其他能力，模型可以继续调用 ToolSearch，迭代补充工具。

整个过程可以理解为 Agentic Tool Retrieval：

```
Think → Search Tool → Load Tool → Call Tool → Observe
  ↑                                               │
  └───────────────────────────────────────────────┘
```

## **九、如何避免动态加载击穿 Prefix Cache**

传统的动态工具组装通常采用直接修改 `tools[]` 的方式：

```
Step 1:
[Tool A, Tool B, Tool C] + System Prompt + History

Step 2:
[Tool A, Tool B, Tool D] + System Prompt + History
```

由于工具定义通常处在提示前缀部分，工具集合发生变化，可能导致后续前缀无法复用。

Anthropic API 原生 Deferred Loading 采用另一种方式：保持初始非延迟工具前缀稳定，将新发现的工具通过 `tool_reference` 在对话历史中内联展开。

**传统动态工具注入**

工具 A/B/C

System

History

工具 A/B/D

System

History

前缀发生变化，后续缓存可能失效。

**Deferred Loading + Tool Reference**

稳定工具前缀

System

History

稳定工具前缀

System

History

新工具

新工具在对话后部展开，原有前缀保持稳定。

Anthropic 官方明确说明，这种原生工具展开方式能够保留已有 Prompt Cache，同时严格模式的工具调用语法可以基于完整工具集构建，不需要因为一次工具发现而重新编译。

[image](https://www.google.com/s2/favicons?domain=https://platform.claude.com\&sz=32)

Claude Platform Docs+1



这也是普通 Tool RAG 与模型服务原生 Deferred Loading 之间的重要技术区别。

## **十、技术收益与架构代价**

Anthropic 官方披露，典型的 GitHub、Slack、Sentry、Grafana、Splunk 多服务器组合，工具定义可能占用约 55,000 Tokens；Tool Search 在典型场景中能够减少超过 85% 的工具定义上下文开销。该数据不是所有 Claude Code 会话的固定收益。

[image](https://www.google.com/s2/favicons?domain=https://platform.claude.com\&sz=32)

Claude Platform Docs



### **工具声明 Token 优化示意**

以官方 55K Token 场景和 85% 降幅进行推算

全量加载

**55,000**

延迟加载

**8,250**

减少工具声明

# **85%**

释放上下文空间

# **46,750**

| 维度           | 技术收益        | 架构代价         |
| ------------ | ----------- | ------------ |
| 上下文成本        | 减少无关工具声明    | 已发现工具仍会消耗上下文 |
| 工具选择         | 减少无关工具干扰    | 存在检索漏召回风险    |
| 执行延迟         | 高频工具可直接调用   | 长尾工具增加搜索环节   |
| 扩展性          | 支持大规模工具目录   | 需要维护工具元数据质量  |
| Prefix Cache | 原生延迟展开可保留前缀 | 依赖模型服务端支持    |
| 安全治理         | 可在进入候选池前过滤  | 搜索本身不能替代执行授权 |

## **十一、对企业级 Agent 的架构启示**

Claude Code Tool Search 最值得借鉴的不是某一种检索算法，而是以下六项架构设计原则：

| 设计原则      | 核心方法                              |
| --------- | --------------------------------- |
| 注册与暴露解耦   | Runtime 持有完整工具池，模型只获得当前需要的能力      |
| 常驻与延迟分层   | 高频核心工具提前加载，长尾工具延迟加载               |
| 模型主动发现    | 将 ToolSearch 作为元工具纳入自主决策循环        |
| 检索与契约展开解耦 | 搜索返回 Tool Reference，独立解析完整 Schema |
| 动态加载与缓存协同 | 尽可能保持稳定前缀，避免反复重组工具声明              |
| 发现与执行权限解耦 | 工具被发现不代表有权执行，Runtime 强制边界         |

对于 Compass 这样的企业级 DataAgent，可以进一步演进为：

```
Tool Registry
      ↓
租户 / 角色 / 权限过滤
      ↓
┌───────────────────────┐
│ L1 常驻核心工具集       │
│ 高频、通用、基础能力     │
├───────────────────────┤
│ L2 场景动态工具集       │
│ Scenario / Phase 驱动  │
├───────────────────────┤
│ L3 按需发现工具集       │
│ ToolSearch / Top-K     │
└───────────────────────┘
      ↓
统一 Tool Dispatcher
      ↓
权限校验 / 执行治理 / Tool Eval
```

其中 L2 业务场景动态组装，是企业级 Agent 根据垂直业务特征增加的确定性治理机制，并不是 Claude Code 已经实现的通用业务场景路由器。

这套架构属于对 Claude Code 设计理念的企业级延伸。

## **十二、技术本质总结**

**Claude Code Tool Search 实际上将工具选择从单阶段决策升级为两阶段决策：**

| 阶段              | 模型需要回答的问题          |
| --------------- | ------------------ |
| Tool Discovery  | 当前任务需要什么能力？        |
| Tool Invocation | 应该调用哪个具体工具，传入什么参数？ |

它通过 **Meta Tool + Deferred Loading + Tool Retrieval + Tool Reference + Schema Expansion**，实现工具调用契约的渐进式披露。

从架构本质上看，就是将工具供给从 **Runtime 预先决定并全量注入**，转变为 **Runtime 控制能力边界、模型自主决定发现需求、系统按需提供调用契约**。

### **参考资料与证据边界**

- [Claude Code 官方 MCP 文档](https://code.claude.com/docs/en/mcp?utm_source=chatgpt.com)
- [Anthropic Tool Search Tool 官方技术文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool?utm_source=chatgpt.com)
- [Anthropic Tool Context 管理文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context?utm_source=chatgpt.com)

本文区分了 Claude Code 客户端已确认的接口行为、Anthropic API 原生延迟加载机制，以及面向企业级 Agent 的架构推演。代码调用示例为原理演示，并非实际客户端抓包；Claude Code 客户端内部的完整检索打分算法尚未得到充分源码验证。
