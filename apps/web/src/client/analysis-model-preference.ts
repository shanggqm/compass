import type { SessionView } from "../contracts/page";

// Keep preferences outside the route's component lifetime. Storage contains only model IDs.
const preferences = new Map<string, string>();

export function modelPreferenceKey(session: SessionView) {
  return `compass:chat-model:${session.user.id}:${session.scope.tenantId}:${session.scope.workspaceId}`;
}

function read(key: string) {
  try {
    return window.localStorage.getItem(key) ?? preferences.get(key);
  } catch {
    return preferences.get(key);
  }
}

function write(key: string, providerId: string) {
  preferences.set(key, providerId);
  try {
    window.localStorage.setItem(key, providerId);
  } catch {
    // Navigation still retains the selection when browser storage is unavailable.
  }
}

export function readModelPreference(key: string, sessionId: string | null) {
  return (sessionId && read(`${key}:${sessionId}`)) || read(key) || "auto";
}

export function saveModelPreference(key: string, sessionId: string | null, providerId: string) {
  write(key, providerId);
  if (sessionId) write(`${key}:${sessionId}`, providerId);
}
