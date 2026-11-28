import { Target, X } from "lucide-react";

export function AnalysisGoalDraft({
  objective,
  pending,
  onExit,
}: {
  objective: string;
  pending?: "preparing" | "confirmation";
  onExit?: () => void;
}) {
  return (
    <section className="analysis-todo analysis-goal-draft" aria-label="目标模式">
      <div className="analysis-goal-draft-bar">
        <Target size={18} aria-hidden="true" />
        <strong>
          Goal ·{" "}
          {pending === "preparing" ? "准备中" : pending === "confirmation" ? "待确认" : "目标模式"}
        </strong>
        <span className="analysis-todo-headline" title={objective}>
          {objective || "填写要持续推进的目标"}
        </span>
        {onExit && (
          <button
            type="button"
            className="analysis-goal-exit"
            aria-label="退出目标模式"
            onClick={onExit}
          >
            <X size={16} aria-hidden="true" />
          </button>
        )}
      </div>
    </section>
  );
}
