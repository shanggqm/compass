import type { ReactNode, Ref } from "react";
import { RotateCcw } from "lucide-react";

type PageProps = {
  chatRef: Ref<HTMLElement>;
  dockRef: Ref<HTMLDivElement>;
  composer: ReactNode;
  goal: ReactNode;
};

export function NewAnalysisPage({
  chatRef,
  dockRef,
  composer,
  suggestions,
  goal,
}: PageProps & { suggestions: ReactNode }) {
  return (
    <section
      ref={chatRef}
      className="analysis-welcome"
      data-chat-state="empty"
      aria-label="新建分析对话"
    >
      <div className="analysis-intro">
        <h2>
          每个好问题，<em>都有迹可循。</em>
        </h2>
        <p>从一个问题开始，发现经营改善的方向。</p>
      </div>
      {goal}
      <div className="analysis-composer-dock" ref={dockRef}>
        {composer}
        {suggestions}
      </div>
    </section>
  );
}

export function AnalysisSessionPage({
  chatRef,
  dockRef,
  composer,
  goal,
  history,
  plan,
  onNew,
}: PageProps & {
  history: ReactNode;
  plan: ReactNode;
  onNew: () => void;
}) {
  return (
    <section
      ref={chatRef}
      className="analysis-welcome model-chat-started"
      data-chat-state="started"
      aria-label="分析会话"
    >
      <div className="model-chat-heading">
        <span>当前对话</span>
        <button type="button" className="new-chat-button" onClick={onNew}>
          <RotateCcw size={15} aria-hidden="true" />
          新建对话
        </button>
      </div>
      {history}
      {goal}
      {plan}
      <div className="analysis-composer-dock" ref={dockRef}>
        {composer}
      </div>
    </section>
  );
}
