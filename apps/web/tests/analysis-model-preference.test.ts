import { afterEach, expect, it, vi } from "vitest";
import {
  modelPreferenceKey,
  readModelPreference,
  saveModelPreference,
} from "../src/client/analysis-model-preference";
import type { SessionView } from "../src/contracts/page";

afterEach(() => vi.unstubAllGlobals());

it("retains each conversation's selection and the latest explicit choice for new conversations", () => {
  const values = new Map<string, string>();
  vi.stubGlobal("window", {
    localStorage: {
      getItem: (key: string) => values.get(key),
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
  const key = "conversation-preferences";
  expect(readModelPreference(key, null)).toBe("auto");
  saveModelPreference(key, null, "provider-a");
  saveModelPreference(key, "first", "provider-a");
  expect(readModelPreference(key, "first")).toBe("provider-a");
  expect(readModelPreference(key, null)).toBe("provider-a");
  saveModelPreference(key, "second", "auto");
  expect(readModelPreference(key, "first")).toBe("provider-a");
  expect(readModelPreference(key, "second")).toBe("auto");
  expect(readModelPreference(key, null)).toBe("auto");
});

it("keeps choices across route remounts when local storage is blocked", () => {
  vi.stubGlobal("window", {
    get localStorage() {
      throw new Error("Storage disabled");
    },
  });
  saveModelPreference("blocked-storage", "first", "provider-b");
  expect(readModelPreference("blocked-storage", "first")).toBe("provider-b");
  expect(readModelPreference("blocked-storage", null)).toBe("provider-b");
});

it("isolates users, tenants and workspaces", () => {
  const session = (user: string, tenant: string, workspace: string) =>
    ({
      user: { id: user },
      scope: { tenantId: tenant, workspaceId: workspace },
    }) as SessionView;
  const keys = [
    session("a", "t", "w"),
    session("b", "t", "w"),
    session("a", "u", "w"),
    session("a", "t", "v"),
  ].map(modelPreferenceKey);
  expect(new Set(keys).size).toBe(4);
  saveModelPreference(keys[0], null, "provider-c");
  for (const key of keys.slice(1)) expect(readModelPreference(key, null)).toBe("auto");
});
