import type { RunConstraints } from "./analysis-run";

export const GOAL_REQUEST_PREFIX =
  "请以长任务目标模式持续推进以下目标，先拟定目标和验收条件供我确认：\n\n";

export function matchesGoalCommand(question: string) {
  return /^\/\S*$/u.test(question) && "goal".startsWith(question.slice(1).toLowerCase());
}

// Express the user's mode selection through the existing question and tool-selection contract.
export function goalSubmission(question: string, constraints?: RunConstraints) {
  const selection = constraints?.toolSelection;
  return {
    question: GOAL_REQUEST_PREFIX + question,
    constraints: {
      ...constraints,
      toolSelection: {
        ...selection,
        packs: [...new Set([...(selection?.packs ?? []), "long_task" as const])],
        workflowIds: selection?.workflowIds ?? [],
        mcpServerIds: selection?.mcpServerIds ?? [],
      },
    },
  };
}
