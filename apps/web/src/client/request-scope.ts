import { sessionSchema } from "../contracts/page-schema";
import type { PagePayload } from "../contracts/page";
import { readModelEvents } from "./model-stream";

export class ApiError extends Error {
  status: number;
  code?: string;
  currentVersion?: number;
  constructor(status: number, message: string, code?: string, currentVersion?: number) {
    super(message);
    this.status = status;
    this.code = code;
    if (Number.isSafeInteger(currentVersion) && currentVersion! > 0)
      this.currentVersion = currentVersion;
  }
}
export type DocumentPhase = "boot" | "active" | "retired";
export type StreamRequestOptions<T> = {
  method?: "GET" | "POST";
  signal: AbortSignal;
  consume: (body: ReadableStream<Uint8Array>, signal: AbortSignal) => Promise<T>;
  onHeaders?: (headers: Headers) => void;
  timeoutMs?: number;
  inputLimitBytes?: number;
  retainOnNotFound?: boolean;
  unavailableMessage?: string;
};
const locationKey = () =>
  location.pathname + (location.search ? `?${new URLSearchParams(location.search)}` : "");
export class DocumentSession {
  phase: DocumentPhase = "boot";
  message = "正在确认登录状态和页面权限…";
  private generation = 0;
  private sequence = 0;
  private csrf = "";
  private controllers = new Set<AbortController>();
  private listeners = new Set<() => void>();
  private clearers = new Set<() => void>();
  private urls = new Set<string>();
  private inFlight = false;
  private probeController: AbortController | null = null;
  private root: HTMLElement | null = null;
  private channel: BroadcastChannel | null = null;
  private reloading = false;
  private disposed = false;
  dirty = false;
  setDirty(value: boolean) {
    this.dirty = value;
  }
  onClear(clear: () => void) {
    this.clearers.add(clear);
    return () => {
      this.clearers.delete(clear);
    };
  }
  private clearSensitiveState() {
    for (const clear of this.clearers) clear();
    this.clearers.clear();
  }
  constructor(
    readonly payload: Pick<PagePayload, "page" | "session" | "viewKey">,
    private navigation?: { push(path: string): void; refresh(): void },
  ) {}
  get retainsContent() {
    return this.phase === "active";
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  snapshot = () => `${this.phase}:${this.message}`;
  serverSnapshot = () => "boot:正在确认登录状态和页面权限…";
  attach(root: HTMLElement | null) {
    this.root = root;
  }
  private emit() {
    for (const listener of this.listeners) listener();
  }
  private invalidateProbe() {
    this.sequence++;
    this.probeController?.abort();
    this.probeController = null;
    this.inFlight = false;
  }
  retire(message: string, destination?: string) {
    if (this.disposed) return;
    if (this.phase !== "retired") {
      // Hide synchronously, before React schedules the unmount or any pending callback runs.
      if (this.root) {
        this.root.hidden = true;
        this.root.inert = true;
      }
      this.phase = "retired";
      document.querySelectorAll<HTMLElement>("[data-sensitive-portal]").forEach((element) => {
        element.hidden = true;
        element.inert = true;
      });
      this.generation++;
      this.invalidateProbe();
      this.csrf = "";
      this.dirty = false;
      this.message = message;
      for (const controller of this.controllers) controller.abort();
      this.controllers.clear();
      this.clearSensitiveState();
      for (const url of this.urls) URL.revokeObjectURL(url);
      this.urls.clear();
      this.emit();
    }
    if (destination) this.recover(destination);
  }
  reload(destination = locationKey()) {
    if (this.disposed) return;
    if (this.phase === "retired" || !this.navigation) return this.recover(destination);
    if (destination === locationKey()) this.navigation.refresh();
    else this.navigate(destination);
  }
  confirmNavigation() {
    if (this.dirty && !window.confirm("离开当前页面并放弃未保存的修改？")) return false;
    this.dirty = false;
    return true;
  }
  navigate(destination: string) {
    if (this.disposed || !this.confirmNavigation()) return false;
    const target = new URL(destination, location.href);
    if (target.origin !== location.origin) throw new Error("Invalid navigation target");
    const path = target.pathname + target.search + target.hash;
    if (this.navigation) this.navigation.push(path);
    else this.recover(path);
    return true;
  }
  private recover(destination: string) {
    if (this.disposed) return;
    if (this.reloading) return;
    this.reloading = true;
    location.replace(destination);
  }
  private current(generation: number) {
    return (
      generation === this.generation &&
      locationKey() === this.payload.viewKey &&
      this.phase === "active"
    );
  }
  private assertCurrent(generation: number) {
    if (this.disposed || generation !== this.generation)
      throw new DOMException("页面已切换。", "AbortError");
    if (this.phase === "boot") throw new ApiError(409, "页面正在加载，请稍候。");
    if (this.retainsContent && Date.now() >= Date.parse(this.payload.session.expiresAt)) {
      this.retire("登录已过期，请重新登录。", "/login");
      throw new ApiError(401, "登录已过期，请重新登录。");
    }
    if (!this.current(generation)) {
      this.retire("页面授权校验已失效，请重新加载。");
      throw new ApiError(401, "页面已关闭，请重新加载。");
    }
  }
  private isRetired() {
    return this.phase === "retired";
  }
  async probe() {
    if (this.disposed || this.inFlight || this.isRetired()) return;
    this.inFlight = true;
    const generation = this.generation,
      sequence = ++this.sequence;
    const controller = new AbortController();
    this.probeController = controller;
    this.controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const params = new URLSearchParams({
        ...this.payload.session.scope,
        pageId: this.payload.page.id,
      });
      const response = await fetch(`/api/web/v1/session?${params}`, {
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      });
      const body = await response.json();
      if (
        this.disposed ||
        generation !== this.generation ||
        sequence !== this.sequence ||
        locationKey() !== this.payload.viewKey ||
        this.isRetired() ||
        controller.signal.aborted
      )
        return;
      if (!response.ok) {
        if (response.status >= 500 && this.retainsContent) return;
        this.retire(
          response.status === 401 ? "登录已失效，请重新登录。" : "暂时无法确认页面访问权限。",
          this.retainsContent && response.status === 401 ? "/login" : undefined,
        );
        return;
      }
      const view = sessionSchema.safeParse(
        Object.fromEntries(Object.entries(body).filter(([key]) => key !== "csrfToken")),
      );
      if (
        !view.success ||
        typeof body.csrfToken !== "string" ||
        body.csrfToken.length !== 43 ||
        response.headers.get("X-Compass-View-Revision") !== body.viewRevision
      ) {
        this.retire("页面响应校验失败，请重新加载。");
        return;
      }
      if (
        view.data.viewRevision !== this.payload.session.viewRevision ||
        view.data.sessionId !== this.payload.session.sessionId
      ) {
        const wasActive = this.retainsContent;
        this.retire("访问权限已更新，请重新加载。", wasActive ? locationKey() : undefined);
        return;
      }
      if (Date.now() >= Date.parse(view.data.expiresAt)) {
        this.retire("登录已过期，请重新登录。");
        return;
      }
      this.csrf = body.csrfToken;
      this.phase = "active";
      if (this.root) {
        this.root.hidden = false;
        this.root.inert = false;
      }
      this.emit();
    } catch {
      if (
        !this.disposed &&
        generation === this.generation &&
        sequence === this.sequence &&
        !this.isRetired()
      ) {
        if (!this.retainsContent)
          this.retire("连接暂时不可用，页面内容已收起。请恢复连接后重新加载。");
      }
    } finally {
      clearTimeout(timeout);
      this.controllers.delete(controller);
      if (this.probeController === controller) {
        this.probeController = null;
        this.inFlight = false;
      }
    }
  }
  start() {
    void this.probe();
    const heartbeat = setInterval(() => void this.probe(), 15000);
    const expiry = setInterval(() => {
      if (this.retainsContent && Date.now() >= Date.parse(this.payload.session.expiresAt))
        this.retire("登录已过期，请重新登录。", "/login");
    }, 1000);
    const pagehide = () => this.retire("页面已关闭。");
    const pageshow = (event: PageTransitionEvent) => {
      if (event.persisted) {
        this.retire("正在重新加载页面。");
        this.reload();
      }
    };
    const offline = () => this.retire("网络已断开，页面内容已收起。");
    const online = () => {
      if (this.phase === "retired") this.reload();
    };
    const beforeunload = (event: BeforeUnloadEvent) => {
      if (this.dirty && this.retainsContent) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("pagehide", pagehide);
    window.addEventListener("pageshow", pageshow);
    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    window.addEventListener("beforeunload", beforeunload);
    this.channel =
      typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("compass-identity");
    if (this.channel)
      this.channel.onmessage = () => this.retire("登录身份已变化，正在重新确认。", locationKey());
    return () => {
      clearInterval(heartbeat);
      this.generation++;
      this.invalidateProbe();
      clearInterval(expiry);
      window.removeEventListener("pagehide", pagehide);
      window.removeEventListener("pageshow", pageshow);
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
      window.removeEventListener("beforeunload", beforeunload);
      this.channel?.close();
      for (const controller of this.controllers) controller.abort();
      for (const url of this.urls) URL.revokeObjectURL(url);
      this.urls.clear();
    };
  }
  dispose() {
    this.disposed = true;
    this.generation++;
    this.invalidateProbe();
    this.dirty = false;
    this.root = null;
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
    this.clearSensitiveState();
    for (const url of this.urls) URL.revokeObjectURL(url);
    this.urls.clear();
  }
  async request<T>(
    path: string,
    method = "GET",
    body?: unknown,
    options?: {
      retainOnForbidden?: boolean;
      retainOnNotFound?: boolean;
      signal?: AbortSignal;
      timeoutMs?: number;
      durable?: boolean;
    },
  ): Promise<T> {
    const generation = this.generation;
    this.assertCurrent(generation);
    const controller = new AbortController();
    const abort = () => controller.abort(options?.signal?.reason);
    options?.signal?.addEventListener("abort", abort, { once: true });
    if (options?.signal?.aborted) abort();
    this.controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), options?.timeoutMs ?? 15000);
    try {
      const response = await fetch(`/api/web/v1/${path}`, {
        method,
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
        headers:
          method === "GET"
            ? {}
            : {
                "Content-Type": "application/json",
                "X-CSRF-Token": this.csrf,
                ...(options?.durable ? { "X-Compass-Run-Version": "2" } : {}),
              },
        ...(method === "GET" ? {} : { body: JSON.stringify(body ?? {}) }),
      });
      this.assertCurrent(generation);
      if (!response.ok) {
        const error = await response.json();
        this.assertCurrent(generation);
        if (response.status === 401) this.retire("登录已失效。", "/login");
        if (
          (response.status === 403 && !options?.retainOnForbidden) ||
          (response.status === 404 && !options?.retainOnNotFound)
        )
          this.retire("访问范围已变化，请重新加载。", locationKey());
        throw new ApiError(
          response.status,
          error.error?.message ?? "操作失败，请重试。",
          error.error?.code,
          error.error?.currentVersion,
        );
      }
      if (response.headers.get("X-Compass-View-Revision") !== this.payload.session.viewRevision) {
        this.retire("权限配置已更新，正在重新加载。", locationKey());
        throw new ApiError(409, "页面正在重新加载。");
      }
      const value =
        response.status === 204
          ? undefined
          : response.headers.get("content-type")?.includes("text/csv")
            ? await response.blob()
            : await response.json();
      this.assertCurrent(generation);
      return value as T;
    } finally {
      clearTimeout(timeout);
      options?.signal?.removeEventListener("abort", abort);
      this.controllers.delete(controller);
    }
  }
  async streamRequest<T>(
    path: string,
    body: Record<string, unknown>,
    options: StreamRequestOptions<T>,
  ): Promise<T> {
    const generation = this.generation;
    this.assertCurrent(generation);
    const serialized = JSON.stringify(body);
    if (new TextEncoder().encode(serialized).byteLength > (options.inputLimitBytes ?? 64 * 1024))
      throw new ApiError(422, "本次输入过大，请减少历史消息后重试。");
    const controller = new AbortController();
    const abort = () => controller.abort(options.signal.reason);
    options.signal.addEventListener("abort", abort, { once: true });
    if (options.signal.aborted) abort();
    this.controllers.add(controller);
    let expired = false;
    const timeout = setTimeout(() => {
      expired = true;
      controller.abort(new DOMException("Timed out", "TimeoutError"));
    }, options.timeoutMs ?? 200000);
    try {
      const response = await fetch(`/api/web/v1/${path}`, {
        method: options.method ?? "POST",
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-CSRF-Token": this.csrf },
        ...(options.method === "GET" ? {} : { body: serialized }),
      });
      this.assertCurrent(generation);
      if (controller.signal.aborted) throw new DOMException("运行读取已停止。", "AbortError");
      if (!response.ok) {
        const error = await response.json().catch(() => null);
        this.assertCurrent(generation);
        if (response.status === 401) this.retire("登录已失效。", "/login");
        if (response.status === 403 || (response.status === 404 && !options.retainOnNotFound))
          this.retire("访问范围已变化，请重新加载。", locationKey());
        throw new ApiError(
          response.status,
          error?.error?.message ?? options.unavailableMessage ?? "运行任务启动失败，请重试。",
          error?.error?.code,
        );
      }
      if (response.headers.get("X-Compass-View-Revision") !== this.payload.session.viewRevision) {
        this.retire("权限配置已更新，正在重新加载。", locationKey());
        throw new ApiError(409, "页面正在重新加载。");
      }
      if (!response.body)
        throw new ApiError(503, options.unavailableMessage ?? "运行任务未返回结果。");
      options.onHeaders?.(response.headers);
      const result = await options.consume(response.body, controller.signal);
      this.assertCurrent(generation);
      if (controller.signal.aborted) throw new DOMException("运行读取已停止。", "AbortError");
      return result;
    } catch (error) {
      if (expired) throw new ApiError(503, "本次运行已超时，请稍后手动重试。");
      throw error;
    } finally {
      clearTimeout(timeout);
      controller.abort();
      options.signal.removeEventListener("abort", abort);
      this.controllers.delete(controller);
    }
  }
  async modelRequest<T, E extends { type: string; call_id: string }>(
    path: string,
    body: { call_id: string } & Record<string, unknown>,
    options: {
      signal: AbortSignal;
      parseResult: (value: unknown) => T;
      parseEvent: (value: unknown) => E;
      onEvent: (event: E) => void;
      onHeaders?: (headers: Headers) => void;
    },
  ): Promise<T | undefined> {
    const generation = this.generation;
    this.assertCurrent(generation);
    const serialized = JSON.stringify(body);
    if (new TextEncoder().encode(serialized).byteLength > 2 * 1024 * 1024)
      throw new ApiError(422, "本次输入过大，请减少历史消息或工具内容。");
    const controller = new AbortController();
    const abort = () => controller.abort();
    options.signal.addEventListener("abort", abort, { once: true });
    if (options.signal.aborted) controller.abort();
    this.controllers.add(controller);
    let expired = false;
    const timeout = setTimeout(() => {
      expired = true;
      controller.abort();
    }, 200000);
    try {
      const response = await fetch(`/api/web/v1/${path}`, {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-CSRF-Token": this.csrf },
        body: serialized,
      });
      this.assertCurrent(generation);
      if (controller.signal.aborted) throw new DOMException("调用已停止。", "AbortError");
      options.onHeaders?.(response.headers);
      if (!response.ok) {
        const error = await response.json();
        this.assertCurrent(generation);
        if (response.status === 401) this.retire("登录已失效。", "/login");
        if (response.status === 403) this.retire("访问范围已变化，请重新加载。", locationKey());
        throw new ApiError(
          response.status,
          error.error?.message ?? "模型调用失败，请重试。",
          error.error?.code,
        );
      }
      if (response.headers.get("X-Compass-View-Revision") !== this.payload.session.viewRevision) {
        this.retire("权限配置已更新，正在重新加载。", locationKey());
        throw new ApiError(409, "页面正在重新加载。");
      }
      if (!response.body) throw new ApiError(503, "模型服务未返回结果。");
      if (response.headers.get("content-type")?.includes("text/event-stream")) {
        let final = false;
        for await (const value of readModelEvents(response.body, controller.signal)) {
          this.assertCurrent(generation);
          if (controller.signal.aborted) throw new DOMException("调用已停止。", "AbortError");
          const event = options.parseEvent(value);
          if (final || event.call_id !== body.call_id)
            throw new ApiError(503, "模型返回顺序异常，结果不完整。");
          final = event.type === "final";
          options.onEvent(event);
        }
        this.assertCurrent(generation);
        if (!final) throw new ApiError(503, "输出连接已中断，结果不完整。");
        return undefined;
      }
      const reader = response.body.getReader();
      const chunks: Uint8Array[] = [];
      let size = 0;
      try {
        while (true) {
          const { done, value } = await reader.read();
          this.assertCurrent(generation);
          if (controller.signal.aborted) throw new DOMException("调用已停止。", "AbortError");
          if (done) break;
          size += value.byteLength;
          if (size > 2 * 1024 * 1024) throw new ApiError(503, "模型返回内容超过限制。");
          chunks.push(value);
        }
      } finally {
        await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
      const data = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        data.set(chunk, offset);
        offset += chunk.byteLength;
      }
      const result = options.parseResult(JSON.parse(new TextDecoder().decode(data)));
      this.assertCurrent(generation);
      if (controller.signal.aborted) throw new DOMException("调用已停止。", "AbortError");
      return result;
    } catch (error) {
      if (expired) throw new ApiError(503, "本次调用已超时，请稍后手动重试。");
      throw error;
    } finally {
      clearTimeout(timeout);
      controller.abort();
      options.signal.removeEventListener("abort", abort);
      this.controllers.delete(controller);
    }
  }
  download(blob: Blob, filename = "compass-audit.csv") {
    this.assertCurrent(this.generation);
    const url = URL.createObjectURL(blob);
    this.urls.add(url);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    setTimeout(() => {
      if (this.urls.delete(url)) URL.revokeObjectURL(url);
    }, 1000);
  }
  releaseDownloads() {
    for (const url of this.urls) URL.revokeObjectURL(url);
    this.urls.clear();
  }
  async logout() {
    const csrf = this.csrf;
    this.retire("正在退出登录…");
    try {
      const response = await fetch("/api/web/v1/auth/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: "{}",
      });
      if (!response.ok) throw new Error();
      this.channel?.postMessage("logout");
      this.reload("/login");
    } catch {
      this.message = "退出请求尚未确认，请重新加载后重试。";
      this.emit();
    }
  }
}
