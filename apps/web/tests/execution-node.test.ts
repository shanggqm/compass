import { describe, expect, it } from "vitest";
import { ExecutionSequence, type ExecutionNode } from "../src/contracts/execution-node";
import {
  RUN_STREAM_LIMITS,
  autonomyExtensions,
  parseRunPacket,
  projectAutonomyExtension,
  RunSequence,
} from "../src/contracts/analysis-run";
import { applyAnalysisRunEvent, type AnalysisTurnView } from "../src/client/analysis-run";
import { executionEntries } from "../src/components/ui/execution-timeline";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const node = (n: number, fields: Partial<ExecutionNode> = {}): ExecutionNode => ({
  id: id(n),
  revision: 1,
  kind: "workflow",
  status: "running",
  title: "渠道分析",
  startedAtMs: 1000,
  dependsOn: [],
  display: { policy: "summary" },
  ...fields,
});
const extension = (value: ExecutionNode) => ({
  type: "stream.extension" as const,
  payload: { name: "compass.execution.node", version: 1, required: false, data: value },
});

describe("unified execution protocol", () => {
  it("passes the BFF projection, preserves parentage and merges by identity", () => {
    const sequence = new RunSequence({ requestId: id(100), extensions: autonomyExtensions });
    const events = [
      { type: "run.started", payload: {} },
      extension(node(1)),
      extension(node(2, { parentId: id(1), kind: "group", scheduling: "parallel" })),
      extension(
        node(3, {
          parentId: id(2),
          kind: "tool",
          origin: { kind: "mcp", serverId: "retail", toolName: "query" },
          executor: { kind: "sandbox", runtime: "Python" },
        }),
      ),
      extension(
        node(3, { parentId: id(2), kind: "tool", revision: 2, status: "failed", endedAtMs: 2000 }),
      ),
    ];
    let turn = {
      id: id(99),
      requestId: id(100),
      question: "test",
      modelLabel: "model",
      steps: [],
      phase: "running",
      stopState: "idle",
    } as AnalysisTurnView;
    events.forEach((event, index) => {
      const packet = parseRunPacket({
        protocol: "compass.run",
        version: 1,
        requestId: id(100),
        runId: id(101),
        seq: index + 1,
        event,
      });
      sequence.accept(packet, 1000);
      turn = applyAnalysisRunEvent(turn, id(101), packet.event);
      if (packet.event.type === "stream.extension")
        expect(projectAutonomyExtension(packet.event)).toBeDefined();
    });
    expect(turn.execution?.map((entry) => entry.id)).toEqual([id(1), id(2), id(3)]);
    expect(turn.execution?.[2].status).toBe("failed");
  });

  it("rejects orphan, reparented, cyclic, replayed and terminal updates", () => {
    for (const invalid of [
      node(2, { parentId: id(99) }),
      node(1, { revision: 3 }),
      node(1, { revision: 2, parentId: id(2) }),
      node(1, { revision: 2, dependsOn: [id(2)] }),
    ]) {
      const sequence = new ExecutionSequence();
      sequence.accept(node(1), RUN_STREAM_LIMITS);
      sequence.accept(node(2, { dependsOn: [id(1)] }), RUN_STREAM_LIMITS);
      expect(() => sequence.accept(invalid, RUN_STREAM_LIMITS)).toThrow();
    }
    const sequence = new ExecutionSequence();
    sequence.accept(node(1, { status: "completed", endedAtMs: 1200 }), RUN_STREAM_LIMITS);
    expect(() => sequence.accept(node(1, { revision: 2 }), RUN_STREAM_LIMITS)).toThrow();
  });

  it("enforces display policy and MCP identity without guessing execution environment", () => {
    for (const invalid of [
      node(1, { kind: "tool", origin: { kind: "mcp", toolName: "query" } }),
      node(1, { display: { policy: "summary", inputPreview: "private" } }),
      node(1, { executor: { kind: "sandbox" }, endedAtMs: 1200 }),
    ]) {
      expect(() => new ExecutionSequence().accept(invalid, RUN_STREAM_LIMITS)).toThrow();
    }
  });
});

describe("database query timeline", () => {
  const databaseNode = (fields: Partial<ExecutionNode>) =>
    node(1, {
      kind: "tool",
      origin: { kind: "builtin", toolName: "database_query" },
      title: "查询数据库",
      ...fields,
    });

  it("keeps running and completed summaries on the same call and leaves full details readable", () => {
    const input = JSON.stringify({
      op: "query",
      sql: `SELECT\n  COUNT(*) AS orders FROM ${"sales_".repeat(12)} WHERE paid = true`,
    });
    const output = JSON.stringify({ columns: ["orders"], rows: [[0]], truncated: false });
    const running = databaseNode({ detail: { input } });
    const [started] = executionEntries([running], true);
    const [finished] = executionEntries(
      [{ ...running, revision: 2, status: "completed", detail: { input, output } }],
      false,
    );
    expect(started.title).toMatch(
      /^执行 SELECT COUNT\(\*\) AS orders FROM .*….*WHERE paid = true$/,
    );
    expect(started.queryResult).toBeUndefined();
    expect(finished).toMatchObject({
      id: started.id,
      title: started.title,
      queryResult: "返回 1 行 · orders=0",
      input,
      output,
    });
  });

  it.each([
    [
      { op: "list_connections" },
      { connections: [{ name: "零售库" }] },
      "列出可用连接",
      "1 个连接 · 零售库",
    ],
    [
      { op: "list_tables", offset: 50, connectionId: "warehouse" },
      { columns: ["schema", "table"], rows: [["retail", "sales"]], truncated: true },
      "列出数据表 · 偏移 50 · 连接 warehouse",
      "返回 1 行（已截断） · schema=retail · table=sales",
    ],
    [
      { op: "describe_table", schema: "retail", table: "sales" },
      { columns: ["column_name", "data_type"], rows: [["paid", "boolean"]] },
      "查看表结构 retail.sales",
      "返回 1 行 · column_name=paid · data_type=boolean",
    ],
  ] as const)(
    "summarizes authorized metadata previews for %o",
    (input, output, title, queryResult) => {
      const [entry] = executionEntries(
        [
          databaseNode({
            status: "completed",
            display: {
              policy: "preview",
              inputPreview: JSON.stringify(input),
              outputPreview: JSON.stringify(output),
            },
          }),
        ],
        false,
      );
      expect(entry).toMatchObject({ title, queryResult });
    },
  );

  it("handles empty, failed, malformed and detail-free calls without inventing results", () => {
    const detail = { input: '{"op":"query","sql":"SELECT 1"}', output: '{"columns":[],"rows":[]}' };
    const entries = executionEntries(
      [
        databaseNode({ status: "empty", detail }),
        databaseNode({ status: "failed", detail }),
        databaseNode({ status: "completed", detail: { input: '{"op":', output: '{"rows":' } }),
        databaseNode({ status: "completed", title: "查询经营数据" }),
        databaseNode({
          status: "completed",
          detail,
          origin: { kind: "mcp", serverId: "remote", toolName: "database_query" },
        }),
      ],
      false,
    );
    expect(entries.map(({ title, queryResult }) => [title, queryResult])).toEqual([
      ["执行 SELECT 1", "返回 0 行"],
      ["执行 SELECT 1", undefined],
      ["查询数据库", undefined],
      ["查询经营数据", undefined],
      ["database_query", undefined],
    ]);
  });
});

describe("context optimization process", () => {
  it("keeps a completed model node with an optimization summary visible", () => {
    const [entry] = executionEntries(
      [node(90, { kind: "model", status: "completed", summary: "历史保留 5 轮，移出 3 轮" })],
      false,
    );
    expect(entry).toMatchObject({ title: "整理上下文", summary: "历史保留 5 轮，移出 3 轮" });
  });
  it("shows the original row count independently of its five-row preview", () => {
    const [entry] = executionEntries(
      [
        node(91, {
          kind: "tool",
          status: "completed",
          origin: { kind: "builtin", toolName: "database_query" },
          detail: {
            input: '{"op":"query","sql":"SELECT * FROM daily"}',
            output: JSON.stringify({
              returnedRows: 10000,
              complete: true,
              ref: "results/original.jsonl",
              columns: ["id"],
              preview: [[1], [2], [3], [4], [5]],
            }),
          },
        }),
      ],
      false,
    );
    expect(entry.queryResult).toContain("返回 10000 行（原件已保存）");
  });
});

describe("independent child task traces", () => {
  it("keeps two children separate even when their completed model ancestors are hidden", () => {
    const first = node(101, {
      kind: "subagent",
      title: "库存",
      subagent: { name: "库存", childRunId: id(201) },
    });
    const second = node(102, {
      kind: "subagent",
      title: "销售",
      subagent: { name: "销售", childRunId: id(202), deliveryStatus: "partial" },
    });
    const hidden = node(103, { kind: "model", parentId: first.id, title: "", status: "completed" });
    const tools = [
      node(104, { kind: "tool", parentId: hidden.id }),
      node(105, { kind: "tool", parentId: second.id }),
    ];
    const entries = executionEntries([first, second, hidden, ...tools], true);
    expect(entries.map((e) => e.id)).toEqual([first.id, second.id]);
    expect(entries[0].children?.map((e) => e.id)).toEqual([id(104)]);
    expect(entries[1].children?.map((e) => e.id)).toEqual([id(105)]);
    expect(entries[1].deliveryStatus).toBe("partial");
  });
});
