import "server-only";

export function publicLangfuseProjectUrl(): string | null {
  try {
    const candidate = new URL(process.env.COMPASS_LANGFUSE_PROJECT_URL ?? "");
    if (
      (candidate.protocol === "https:" ||
        (candidate.protocol === "http:" &&
          ["127.0.0.1", "localhost"].includes(candidate.hostname))) &&
      !candidate.username &&
      !candidate.password &&
      !candidate.search &&
      !candidate.hash &&
      /\/project\/[^/]+\/?$/.test(candidate.pathname)
    )
      return candidate.href.replace(/\/$/, "");
  } catch {
    // Invalid or missing public configuration leaves the browser entry disabled.
  }
  return null;
}
