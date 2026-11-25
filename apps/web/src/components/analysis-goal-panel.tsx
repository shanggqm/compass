"use client";

import { useId, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { Goal } from "@/client/analysis-goal";
import { UiButton } from "./ui/button";

const labels: Record<Goal["status"], string> = {
  active: "进行中",
  paused: "已暂停",
  blocked: "受阻",
  budget_limited: "预算已用尽",
  completed: "已完成",
  canceled: "已取消",
};
export function AnalysisGoalPanel({
  goal,
  busy,
  error,
  action,
  edit,
}: {
  goal: Goal;
  busy: boolean;
  error: string;
  action: (value: "pause" | "resume" | "cancel") => Promise<unknown>;
  edit: () => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(true);
  const ended = ["completed", "canceled"].includes(goal.status);
  function act(value: "pause" | "resume" | "cancel") {
    if (value === "cancel" && !window.confirm("结束这个目标？已完成的结论和产物会保留。")) return;
    void action(value).catch(() => undefined);
  }
  return (
    <section className="analysis-goal-panel" aria-label="长任务目标">
      <button
        type="button"
        className="analysis-goal-heading"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
      >
        <ChevronDown size={16} aria-hidden="true" />
        <strong>长任务目标 · {labels[goal.status]}</strong>
        <span>
          第 {goal.usage.rounds}/{goal.budget.maxRounds} 轮 · 已用{" "}
          {Math.ceil(Math.max(goal.usage.tokensPct, goal.usage.costPct))}%
        </span>
      </button>
      <div id={id} hidden={!open}>
        <p>{goal.contract.objective}</p>
        <ul>
          {goal.contract.acceptance.map((item) => (
            <li key={item.id}>
              <span aria-label={goal.satisfied?.includes(item.id) ? "已支撑" : "待完成"}>
                {goal.satisfied?.includes(item.id) ? "✓" : "○"}
              </span>{" "}
              {item.text}
            </li>
          ))}
        </ul>
        {!!goal.contract.constraints?.length && <p>约束：{goal.contract.constraints.join("；")}</p>}
        <p>
          待办 {goal.counts.openTodos} · 待核 {goal.counts.conflicts} · 待补查 {goal.counts.stale} ·
          已复用 {goal.counts.reusedCalls} 次
        </p>
        {goal.reason && <p>{goal.reason}</p>}
        {goal.usage.overrun && <p>本轮实际用量已超过目标预算，自动续轮已停止。</p>}
        {!ended && (
          <div className="actions">
            {goal.status === "active" && (
              <UiButton busy={busy} variant="secondary" onClick={() => act("pause")}>
                暂停目标
              </UiButton>
            )}
            {["paused", "blocked"].includes(goal.status) && (
              <UiButton busy={busy} onClick={() => act("resume")}>
                恢复目标
              </UiButton>
            )}
            <UiButton disabled={busy} variant="quiet" onClick={edit}>
              修改目标
            </UiButton>
            <UiButton busy={busy} variant="quiet" onClick={() => act("cancel")}>
              取消目标
            </UiButton>
          </div>
        )}
      </div>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
