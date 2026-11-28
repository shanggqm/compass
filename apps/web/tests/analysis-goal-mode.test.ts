import { expect, it } from "vitest";
import {
  goalSubmission,
  GOAL_REQUEST_PREFIX,
  matchesGoalCommand,
} from "../src/client/analysis-goal-mode";

it("offers Goal for a leading slash command while leaving ordinary text and URLs alone", () => {
  for (const text of ["/", "/g", "/go", "/goal", "/GOAL"])
    expect(matchesGoalCommand(text)).toBe(true);
  for (const text of ["", "销量/库存", "https://example.com", "/unknown", "/goal report", "/\n"])
    expect(matchesGoalCommand(text)).toBe(false);
});

it("submits explicit goal intent and long-task tools alongside the user's other selections", () => {
  const constraints = {
    plan: "required" as const,
    toolSelection: {
      packs: ["python_sandbox" as const, "long_task" as const],
      workflowIds: ["workflow"],
      mcpServerIds: ["mcp"],
    },
  };
  const result = goalSubmission("核对本月销售与库存", constraints);
  expect(result.question).toBe(GOAL_REQUEST_PREFIX + "核对本月销售与库存");
  expect(result.constraints).toEqual(constraints);
  expect(goalSubmission("目标").constraints.toolSelection).toEqual({
    packs: ["long_task"],
    workflowIds: [],
    mcpServerIds: [],
  });
  expect(constraints.toolSelection.packs).toEqual(["python_sandbox", "long_task"]);
});
