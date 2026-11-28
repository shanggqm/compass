"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, Target } from "lucide-react";
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
  const panel = useRef<HTMLElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const [visibility, setVisibility] = useState<"closed" | "preview" | "pinned">("closed");
  const open = visibility !== "closed";
  const ended = ["completed", "canceled"].includes(goal.status);
  useEffect(() => {
    if (!open) return;
    function dismiss(event: PointerEvent) {
      if (event.target instanceof Node && !panel.current?.contains(event.target))
        setVisibility("closed");
    }
    function escape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (panel.current?.contains(document.activeElement)) toggle.current?.focus();
      setVisibility("closed");
    }
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  function act(value: "pause" | "resume" | "cancel") {
    if (value === "cancel" && !window.confirm("结束这个目标？已完成的结论和产物会保留。")) return;
    void action(value).catch(() => undefined);
  }
  return (
    <section
      ref={panel}
      className="analysis-todo analysis-goal-panel"
      aria-label="长任务目标"
      data-complete={goal.status === "completed"}
      onPointerEnter={(event) => {
        if (event.pointerType === "mouse")
          setVisibility((previous) => (previous === "closed" ? "preview" : previous));
      }}
      onPointerLeave={(event) => {
        if (event.pointerType === "mouse")
          setVisibility((previous) => (previous === "preview" ? "closed" : previous));
      }}
      onBlur={(event) => {
        // A pending action disables its button and may clear focus without leaving the panel.
        if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget))
          setVisibility("closed");
      }}
    >
      <button
        ref={toggle}
        type="button"
        className="analysis-todo-toggle analysis-goal-heading"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setVisibility((previous) => (previous === "pinned" ? "closed" : "pinned"))}
      >
        <Target size={18} aria-hidden="true" />
        <strong>Goal · {labels[goal.status]}</strong>
        <span className="analysis-todo-headline" title={goal.contract.objective}>
          {goal.contract.objective}
        </span>
        <span className="analysis-goal-rounds">
          第 {goal.usage.rounds}/{goal.budget.maxRounds} 轮
        </span>
        <ChevronDown className="analysis-goal-chevron" size={14} aria-hidden="true" />
      </button>
      <div className="analysis-todo-popover" hidden={!open}>
        <div
          id={id}
          className="analysis-goal-details"
          onFocusCapture={() => setVisibility("pinned")}
        >
          <div className="analysis-goal-content" tabIndex={0} aria-label="目标详情">
            <p>
              <strong>{goal.contract.objective}</strong>
            </p>
            <p>
              第 {goal.usage.rounds}/{goal.budget.maxRounds} 轮 · 预算已用{" "}
              {Math.ceil(Math.max(goal.usage.tokensPct, goal.usage.costPct))}%
            </p>
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
            {!!goal.contract.constraints?.length && (
              <p>约束：{goal.contract.constraints.join("；")}</p>
            )}
            <p>
              待办 {goal.counts.openTodos} · 待核 {goal.counts.conflicts} · 待补查{" "}
              {goal.counts.stale} · 已复用 {goal.counts.reusedCalls} 次
            </p>
            {goal.reason && <p>{goal.reason}</p>}
            {goal.usage.overrun && <p>本轮实际用量已超过目标预算，自动续轮已停止。</p>}
          </div>
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
              <UiButton
                disabled={busy}
                variant="quiet"
                onClick={() => {
                  setVisibility("closed");
                  edit();
                }}
              >
                修改目标
              </UiButton>
              <UiButton busy={busy} variant="quiet" onClick={() => act("cancel")}>
                取消目标
              </UiButton>
            </div>
          )}
          {error && <p role="alert">{error}</p>}
        </div>
      </div>
    </section>
  );
}
