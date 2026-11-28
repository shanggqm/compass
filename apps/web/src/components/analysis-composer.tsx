"use client";

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { ArrowUp, ChartNoAxesColumn, ChevronDown, Paperclip, Square, Target } from "lucide-react";
import {
  type AnalysisFileView,
  type AnalysisRunEntry,
  type RunConstraints,
  type WorkflowAssetRef,
} from "@/client/analysis-run";
import { notifySessionsChanged } from "@/client/analysis-sessions";
import { requestDurableControl, useDurableConversation } from "@/client/durable-analysis";
import type { components } from "@/generated/api";
import type { PagePayload } from "@/contracts/page";
import { RUN_STREAM_LIMITS } from "@/generated/run-stream-limits";
import type { AvailableModels } from "@/features/models/types";
import { AnalysisAttachments, type ComposerAttachment } from "./analysis-attachments";
import { useAnalysisGoal } from "@/client/analysis-goal";
import { AnalysisGoalDraft } from "./analysis-goal-draft";
import {
  GOAL_REQUEST_PREFIX,
  goalSubmission,
  matchesGoalCommand,
} from "@/client/analysis-goal-mode";
import { AnalysisGoalPanel } from "./analysis-goal-panel";
import { AnalysisRunTurn } from "./analysis-run-turn";
import { AnalysisTodoPanel, planResolved } from "./analysis-todo-panel";
import { useDocumentSession } from "./shell/session-boundary";

import { useStarters, type StarterCards } from "@/features/promptops/use-starters";
import { AnalysisToolSelector, AnalysisToolSelectionBadges } from "./analysis-tool-selector";
import { AnalysisSessionPage, NewAnalysisPage } from "./analysis-pages";
import {
  modelPreferenceKey,
  readModelPreference,
  saveModelPreference,
} from "@/client/analysis-model-preference";
import { isModelContract } from "@/contracts/model-contract";

function AnalysisErrorToast({ message }: { message: string }) {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const timer = window.setTimeout(() => setVisible(false), 5000);
    return () => window.clearTimeout(timer);
  }, []);
  return visible ? (
    <p role="alert" className="toast analysis-error-toast">
      {message}
    </p>
  ) : null;
}

export function AnalysisComposer({
  payload,
  workflow,
}: {
  payload: PagePayload;
  workflow?: { name: string; ref: WorkflowAssetRef };
}) {
  const api = useDocumentSession();
  const input = useRef<HTMLTextAreaElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const stopButton = useRef<HTMLButtonElement>(null);
  const focusInput = useCallback(() => {
    if (input.current?.disabled) stopButton.current?.focus();
    else input.current?.focus();
  }, []);
  const fileInput = useRef<HTMLInputElement>(null);
  const history = useRef<HTMLDivElement>(null);
  const chat = useRef<HTMLElement>(null);
  const composerDock = useRef<HTMLDivElement>(null);
  const followOutput = useRef(true);
  const pendingSubmit = useRef<Record<string, unknown> | null>(null);
  const pendingGoalFocus = useRef(false);
  const [accepting, setAccepting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [navigating, setNavigating] = useState(false);
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState("");
  const [question, setQuestion] = useState("");
  const [goalMode, setGoalMode] = useState(false);
  const [commandDismissed, setCommandDismissed] = useState(false);
  const [composing, setComposing] = useState(false);
  const commandId = useId();
  const editGoal = useCallback(
    (objective: string) => {
      setQuestion(objective ? `修改目标：${objective}\n修改要求：` : "修改目标：");
      api.setDirty(true);
      pendingGoalFocus.current = true;
    },
    [api],
  );
  const workflowPrefilled = useRef(false);
  const workflowName = workflow?.name;
  const prefillWorkflow = useCallback(
    (cards: StarterCards) => {
      if (!workflowName || workflowPrefilled.current) return;
      workflowPrefilled.current = true;
      if (!api.dirty && cards.workflow_parts?.length)
        setQuestion(cards.workflow_parts.join(workflowName));
    },
    [api, workflowName],
  );
  const [entry, setEntry] = useState<AnalysisRunEntry>(
    workflow ? { workflowAssetRef: workflow.ref } : {},
  );
  const constraints = entry.constraints;
  const [toolLabels, setToolLabels] = useState<Record<string, string>>({});
  const [models, setModels] = useState<AvailableModels | null>(null);
  const sessionId = new URL(payload.viewKey, "https://compass.local").searchParams.get("session");
  const preferenceKey = modelPreferenceKey(payload.session);
  const [providerId, setProviderId] = useState(() => readModelPreference(preferenceKey, sessionId));

  const [loadError, setLoadError] = useState("");
  const [retry, setRetry] = useState(0);
  const [formError, setFormError] = useState("");
  const allowed = payload.session.capabilities.includes("models.invoke");
  const { tenantId, workspaceId } = payload.session.scope;
  const starters = useStarters(
    !sessionId && allowed && tenantId && workspaceId
      ? "analysis/prompt-starters?" + new URLSearchParams({ tenantId, workspaceId })
      : null,
    prefillWorkflow,
  );
  const workflowScope = tenantId && workspaceId ? { tenantId, workspaceId } : undefined;
  const conversation = useDurableConversation(api, sessionId, workflowScope);
  const goalControl = useAnalysisGoal(
    api,
    sessionId,
    workflowScope,
    conversation.runs.at(-1)?.runId,
    conversation.reload,
  );
  const canInterrupt =
    ["active", "paused"].includes(goalControl.goal?.status ?? "") &&
    !!conversation.active &&
    /^继续推进目标 · 第 [0-9]+ 轮$/.test(conversation.turns.at(-1)?.question ?? "");
  const notification =
    formError ||
    attachmentError ||
    (conversation.missing ? "原会话已找不到，发送消息将开启新会话。" : conversation.error);
  const { turns } = conversation;
  const busy = accepting || navigating || conversation.loading || Boolean(conversation.active);
  useEffect(() => {
    if (pendingGoalFocus.current && (!busy || canInterrupt)) {
      pendingGoalFocus.current = false;
      input.current?.focus();
    }
  }, [busy, canInterrupt, question]);
  const workflowPluginHref = payload.session.navigation.find(
    (item) => item.id === "plugins-workflows",
  )?.href;
  const automatic = providerId === "auto";
  const selected = models?.items.find((model) => model.id === providerId);
  const canSelectModel = allowed && !busy && Boolean(models?.items.length);
  const commandOpen =
    allowed &&
    (!busy || canInterrupt) &&
    !composing &&
    !commandDismissed &&
    matchesGoalCommand(question) &&
    !entry.workflowAssetRef;
  function chooseGoalMode() {
    setGoalMode(true);
    setCommandDismissed(true);
    setQuestion("");
    setFormError("");
    api.setDirty(true);
    input.current?.focus();
  }
  const canSend =
    allowed &&
    (!busy || canInterrupt) &&
    !accepting &&
    !goalControl.busy &&
    Boolean(models?.items.length) &&
    (automatic || Boolean(selected)) &&
    Boolean(question.trim()) &&
    !matchesGoalCommand(question) &&
    !entry.workflowAssetRef;
  const latestTurn = turns.at(-1);
  const activeTurn = busy ? latestTurn : undefined;
  const dockTurn =
    latestTurn?.phase === "running" ||
    (latestTurn?.terminal?.status === "completed" &&
      latestTurn.plan &&
      planResolved(latestTurn.plan))
      ? latestTurn
      : undefined;

  const revealOutput = useCallback(() => {
    const node = history.current;
    if (node && followOutput.current) node.scrollTop = node.scrollHeight;
  }, []);
  const downloadFile = useCallback(
    (file: Extract<AnalysisFileView, { status: "ready" }>) =>
      api.download(file.blob, file.filename),
    [api],
  );

  useLayoutEffect(() => revealOutput(), [turns, revealOutput]);

  useLayoutEffect(() => {
    const dock = composerDock.current;
    const section = chat.current;
    if (!sessionId || !dock || !section) return;
    const measure = () => {
      section.style.setProperty("--composer-overlap", `${dock.offsetHeight}px`);
      revealOutput();
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(dock);
    return () => {
      observer.disconnect();
      section.style.removeProperty("--composer-overlap");
    };
  }, [revealOutput, sessionId]);

  useEffect(() => {
    if (workflow) {
      api.setDirty(true);
      input.current?.focus({ preventScroll: true });
    }
  }, [api, workflow]);

  useEffect(() => {
    let live = true;
    if (allowed)
      void api
        .request<AvailableModels>(
          `models/available?${new URLSearchParams({ ...payload.session.scope, view: "chat" })}`,
        )
        .then((result) => {
          if (live) {
            setModels(result);
            setLoadError("");
          }
        })
        .catch(() => {
          if (live) setLoadError("可用模型加载失败，请重新加载。");
        });
    return () => {
      live = false;
    };
  }, [api, allowed, payload.session.scope, retry]);

  useEffect(() => {
    const remove = api.onClear(() => {
      setQuestion("");
      setGoalMode(false);
      setEntry({});
      setModels(null);
      setAttachments([]);
      setAttachmentError("");
      setFormError("");
      followOutput.current = true;
      if (fileInput.current) fileInput.current.value = "";
    });
    return () => {
      remove();
    };
  }, [api]);

  async function changeModel(value: string) {
    if (busy || value === providerId) return;
    if (sessionId && turns.length) {
      if (!window.confirm("切换模型将开启新的对话上下文，已有内容保留在会话中。继续？")) return;
      setAccepting(true);
      try {
        let cursor: string | null = null;
        let found: components["schemas"]["SessionListItem"] | undefined;
        do {
          const query = new URLSearchParams({
            ...workflowScope,
            limit: "50",
            ...(cursor ? { cursor } : {}),
          });
          const list: components["schemas"]["SessionList"] = await api.request(
            `analysis/sessions?${query}`,
          );
          found = list.items.find((item) => item.sessionId === sessionId);
          cursor = list.nextCursor;
        } while (!found && cursor);
        if (!found) throw new Error("会话信息已变化，请重新加载。");
        await api.request(`analysis/sessions/${sessionId}/context-reset`, "POST", {
          ...workflowScope,
          requestId: crypto.randomUUID(),
          expectedVersion: found.version,
        });
      } catch (error) {
        setFormError(error instanceof Error ? error.message : "上下文重置失败。");
        return;
      } finally {
        setAccepting(false);
      }
    }
    saveModelPreference(preferenceKey, sessionId, value);
    setProviderId(value);
    setFormError("");
  }

  async function send(event: React.FormEvent) {
    event.preventDefault();
    if (!canSend || !workflowScope) return;
    if (!form.current?.reportValidity()) return;
    if (attachments.length) {
      setFormError("附件暂不支持发送给模型，请移除附件后发送文字。");
      return;
    }
    const text = question.trim();
    const input = {
      question: text,
      sessionId: conversation.missing ? null : sessionId,
      selection: automatic
        ? { mode: "auto", provider_id: null }
        : { mode: "fixed", provider_id: selected!.id },
      ...entry,
      ...(goalMode ? goalSubmission(text, constraints) : {}),
    };
    if (input.question.length > 8000) {
      setFormError("目标内容过长，请缩短后重试。");
      return;
    }
    if (
      !pendingSubmit.current ||
      JSON.stringify(pendingSubmit.current.input) !== JSON.stringify(input)
    )
      pendingSubmit.current = { input, requestId: crypto.randomUUID() };
    setAccepting(true);
    setFormError("");
    try {
      if (canInterrupt) await goalControl.interrupt();
      if (!automatic) {
        const current = await api.request<AvailableModels>(
          `models/available?${new URLSearchParams({ ...payload.session.scope, view: "chat" })}`,
        );
        setModels(current);
        if (!current.items.some((model) => model.id === providerId)) {
          setFormError("所选模型已不可用，请重新选择后发送。");
          return;
        }
      }
      const result = await api.request<components["schemas"]["SessionAccepted"]>(
        "analysis/sessions/submit",
        "POST",
        { ...workflowScope, ...input, requestId: pendingSubmit.current.requestId },
      );
      saveModelPreference(preferenceKey, result.sessionId, providerId);
      pendingSubmit.current = null;
      setQuestion("");
      setGoalMode(false);
      setEntry({});
      api.setDirty(false);
      followOutput.current = true;
      notifySessionsChanged(
        result.session?.titleStatus === "pending"
          ? {
              ...result.session,
              title: Array.from(text.trim().replace(/\s+/gu, " ")).slice(0, 40).join(""),
            }
          : result.session,
      );
      if (result.sessionId === sessionId) conversation.reload();
      else if (api.navigate(`${payload.viewKey.split("?")[0]}?session=${result.sessionId}`))
        setNavigating(true);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "提交未确认，请重试。");
    } finally {
      setAccepting(false);
    }
  }

  function changeConstraints(update: Partial<RunConstraints>) {
    setEntry({ constraints: { ...constraints, ...update } });
    api.setDirty(true);
  }

  function reset() {
    if (!sessionId) {
      setQuestion("");
      setGoalMode(false);
      setEntry({});
      setAttachments([]);
      api.setDirty(false);
      return;
    }
    api.setDirty(Boolean(question || attachments.length));
    if (api.navigate(payload.viewKey.split("?")[0])) setNavigating(true);
  }

  function addAttachments(files: FileList | null) {
    if (!files?.length || !allowed || busy) return;
    const incoming = Array.from(files);
    const available = 10 - attachments.length;
    setAttachmentError(incoming.length > available ? "每条消息最多添加 10 个附件。" : "");
    setAttachments((items) => [
      ...items,
      ...incoming.slice(0, Math.max(0, 10 - items.length)).map((file) => ({
        id: crypto.randomUUID(),
        file,
      })),
    ]);
    setFormError("");
    api.setDirty(true);
    if (fileInput.current) fileInput.current.value = "";
  }

  function removeAttachment(id: string) {
    const remaining = attachments.filter((item) => item.id !== id);
    setAttachments(remaining);
    setAttachmentError("");
    setFormError("");
    api.setDirty(Boolean(question || remaining.length));
  }

  const stopWaiting =
    accepting ||
    stopping ||
    conversation.active?.run?.status === "cancelling" ||
    conversation.loading ||
    (activeTurn &&
      ["waiting_for_run", "requesting", "accepted", "already_terminal"].includes(
        activeTurn.stopState,
      ));
  const stopLabel =
    activeTurn?.stopState === "unconfirmed"
      ? "重试停止"
      : activeTurn?.stopState === "already_terminal"
        ? "等待结果流"
        : "停止任务";
  const stoppingLabel = activeTurn?.cancellationBlocked ? "终止待核对" : "正在终止";

  const composer = (
    <>
      {notification && <AnalysisErrorToast key={notification} message={notification} />}
      <form ref={form} className="analysis-composer" data-editor onSubmit={send}>
        {constraints && Object.keys(constraints).some((key) => key !== "toolSelection") && (
          <fieldset className="analysis-run-constraints" disabled={busy}>
            <legend>本次运行约束</legend>
            <label>
              <input
                type="checkbox"
                checked={constraints.plan === "required"}
                onChange={(event) =>
                  changeConstraints({ plan: event.target.checked ? "required" : undefined })
                }
              />
              先列计划
            </label>
            <div className="ui-field">
              <label htmlFor="analysis-max-steps">最多轮次</label>
              <input
                id="analysis-max-steps"
                className="ui-input"
                type="number"
                min={1}
                max={RUN_STREAM_LIMITS.steps}
                placeholder={String(RUN_STREAM_LIMITS.steps)}
                value={constraints.maxSteps ?? ""}
                onChange={(event) =>
                  changeConstraints({
                    maxSteps: event.target.value ? event.target.valueAsNumber : undefined,
                  })
                }
              />
            </div>
            <div className="ui-field">
              <label htmlFor="analysis-timeout">时限（秒）</label>
              <input
                id="analysis-timeout"
                className="ui-input"
                type="number"
                min={1}
                max={RUN_STREAM_LIMITS.timeoutSeconds}
                placeholder={String(RUN_STREAM_LIMITS.timeoutSeconds)}
                value={constraints.timeoutSeconds ?? ""}
                onChange={(event) =>
                  changeConstraints({
                    timeoutSeconds: event.target.value ? event.target.valueAsNumber : undefined,
                  })
                }
              />
            </div>
            <button type="button" className="secondary compact" onClick={() => setEntry({})}>
              清除约束
            </button>
          </fieldset>
        )}
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          aria-label="选择附件"
          onChange={(event) => addAttachments(event.target.files)}
          disabled={!allowed || busy}
        />
        <AnalysisAttachments items={attachments} disabled={busy} onRemove={removeAttachment} />
        <AnalysisToolSelectionBadges
          value={constraints?.toolSelection}
          labels={toolLabels}
          disabled={busy}
          onChange={(toolSelection) => changeConstraints({ toolSelection })}
        />
        <div className="analysis-command-input">
          {commandOpen && (
            <div
              id={commandId}
              className="analysis-command-menu"
              role="listbox"
              aria-label="输入命令"
            >
              <button
                id={`${commandId}-goal`}
                type="button"
                role="option"
                aria-selected="true"
                tabIndex={-1}
                onPointerDown={(event) => event.preventDefault()}
                onClick={chooseGoalMode}
              >
                <Target size={20} aria-hidden="true" />
                <span>
                  <strong>Goal · 目标模式</strong>
                  <small>设定目标，确认后持续推进</small>
                </span>
                <kbd>/goal</kbd>
              </button>
            </div>
          )}
          <textarea
            ref={input}
            role={commandOpen ? "combobox" : undefined}
            aria-expanded={commandOpen ? true : undefined}
            aria-autocomplete={commandOpen ? "list" : undefined}
            aria-controls={commandOpen ? commandId : undefined}
            aria-activedescendant={commandOpen ? `${commandId}-goal` : undefined}
            onCompositionStart={() => setComposing(true)}
            onCompositionEnd={() => setComposing(false)}
            onBlur={() => setCommandDismissed(true)}
            aria-label="分析问题"
            aria-keyshortcuts="Enter"
            aria-describedby={attachments.length ? "analysis-attachment-notice" : undefined}
            placeholder={
              canInterrupt
                ? "发送消息会先停下当前这一轮"
                : busy
                  ? "Compass 正在分析…"
                  : goalMode
                    ? "描述目标与完成要求，发送后确认目标"
                    : "给 Compass 发送消息，输入 / 选择模式"
            }
            value={question}
            onChange={(event) => {
              setQuestion(event.target.value);
              setCommandDismissed(false);
              setFormError("");
            }}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
              if (commandOpen) {
                if (["ArrowDown", "ArrowUp"].includes(event.key)) {
                  event.preventDefault();
                  return;
                }
                if (
                  (event.key === "Enter" && !event.shiftKey) ||
                  (event.key === "Tab" && !event.shiftKey)
                ) {
                  event.preventDefault();
                  chooseGoalMode();
                  return;
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  setCommandDismissed(true);
                  return;
                }
              }
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing &&
                event.nativeEvent.keyCode !== 229
              ) {
                event.preventDefault();
                form.current?.requestSubmit();
              }
            }}
            maxLength={goalMode ? 8000 - GOAL_REQUEST_PREFIX.length : 8000}
            disabled={busy && !canInterrupt}
          />
        </div>
        <div className="composer-toolbar">
          <label className="composer-model-picker">
            <select
              aria-label="选择 Chat 主模型"
              value={providerId}
              disabled={!canSelectModel}
              onChange={(event) => changeModel(event.target.value)}
            >
              <option value="">选择模型</option>
              <option value="auto">Auto</option>
              {!automatic && !selected && (
                <option value={providerId}>
                  {models ? "所选模型不可用，请重新选择" : "正在读取所选模型…"}
                </option>
              )}
              {models?.items.map((model) => (
                <option value={model.id} key={model.id}>
                  {model.alias}
                </option>
              ))}
            </select>
            <ChevronDown size={16} aria-hidden="true" />
          </label>
          <div className="composer-actions">
            {workflowScope && (
              <AnalysisToolSelector
                value={constraints?.toolSelection}
                onChange={(toolSelection) => changeConstraints({ toolSelection })}
                onLabelsChange={setToolLabels}
                scope={workflowScope}
                capabilities={payload.session.capabilities}
                disabled={!allowed || busy}
              />
            )}
            <button
              type="button"
              className="composer-attach"
              aria-label="添加附件"
              title="添加附件，最多 10 个"
              disabled={!allowed || busy}
              onClick={() => fileInput.current?.click()}
            >
              <Paperclip size={21} aria-hidden="true" />
            </button>
            {busy && !canInterrupt ? (
              <button
                ref={stopButton}
                type="button"
                className="composer-stop-task"
                disabled={Boolean(stopWaiting)}
                aria-label={
                  activeTurn?.stopState === "already_terminal"
                    ? "任务已结束，正在等待原结果流"
                    : stopWaiting
                      ? activeTurn?.cancellationBlocked
                        ? stoppingLabel
                        : "正在终止任务"
                      : stopLabel
                }
                onClick={async () => {
                  const run = conversation.active;
                  if (!run?.run) return;
                  setStopping(true);
                  setFormError("");
                  try {
                    await requestDurableControl(api, sessionId!, run.runId, "cancel", {
                      ...workflowScope,
                      requestId: crypto.randomUUID(),
                      expectedVersion: run.run.version,
                    });
                    conversation.reload();
                  } catch (error) {
                    setFormError(error instanceof Error ? error.message : "终止未确认。");
                  } finally {
                    setStopping(false);
                  }
                }}
              >
                <Square size={15} aria-hidden="true" />
                {activeTurn?.stopState === "already_terminal"
                  ? "等待结果流"
                  : stopWaiting
                    ? stoppingLabel
                    : stopLabel}
              </button>
            ) : (
              <button
                className="composer-send"
                type="submit"
                disabled={!canSend}
                aria-label="发送消息"
                title="发送消息"
              >
                <ArrowUp size={23} aria-hidden="true" />
              </button>
            )}
          </div>
        </div>
      </form>
      {attachments.length > 0 && (
        <p className="composer-attachment-notice" id="analysis-attachment-notice">
          附件仅供预览，暂不支持发送给模型。
        </p>
      )}
      {!allowed ? (
        <p className="model-notice">当前账户尚未获得模型调用权限，请联系工作区管理员。</p>
      ) : loadError ? (
        <p role="alert">
          {loadError}
          <button className="secondary compact" onClick={() => setRetry((value) => value + 1)}>
            重新加载模型
          </button>
        </p>
      ) : models && models.items.length === 0 ? (
        <p className="model-notice">暂无可用主模型，请联系平台管理员开放模型。</p>
      ) : null}
      {entry.workflowAssetRef && (
        <button type="button" className="secondary compact" onClick={() => setEntry({})}>
          旧版工作流执行已停止 · 清除选择后继续分析
        </button>
      )}
    </>
  );

  const pendingGoal = conversation.runs
    .flatMap((run) => Object.values(run.objects))
    .find(
      (object) =>
        object.kind === "approval" && object.value.status === "pending" && object.value.goal,
    );
  const preparingGoal =
    conversation.active && latestTurn?.question.startsWith(GOAL_REQUEST_PREFIX)
      ? latestTurn.question.slice(GOAL_REQUEST_PREFIX.length)
      : null;
  const goalPanel = goalMode ? (
    <AnalysisGoalDraft
      objective={question}
      onExit={
        busy
          ? undefined
          : () => {
              setGoalMode(false);
              api.setDirty(Boolean(question || attachments.length));
              input.current?.focus();
            }
      }
    />
  ) : goalControl.goal ? (
    <AnalysisGoalPanel
      goal={goalControl.goal}
      busy={goalControl.busy}
      error={goalControl.error}
      action={goalControl.act}
      edit={() => editGoal("")}
    />
  ) : pendingGoal?.kind === "approval" ? (
    <AnalysisGoalDraft
      objective={pendingGoal.value.goal!.contract.objective}
      pending="confirmation"
    />
  ) : preparingGoal !== null ? (
    <AnalysisGoalDraft objective={preparingGoal} pending="preparing" />
  ) : null;

  // The URL selects the page before any messages arrive; loading never renders the new page.
  if (sessionId)
    return (
      <AnalysisSessionPage
        chatRef={chat}
        dockRef={composerDock}
        composer={composer}
        onNew={reset}
        goal={goalPanel}
        plan={
          dockTurn?.plan && (
            <AnalysisTodoPanel key={dockTurn.id} plan={dockTurn.plan} onFocusReturn={focusInput} />
          )
        }
        history={
          <>
            <div
              ref={history}
              className="model-chat-history"
              aria-label="当前会话记录"
              tabIndex={0}
              onScroll={(event) => {
                const node = event.currentTarget;
                followOutput.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
              }}
            >
              {conversation.loading && <p role="status">正在读取会话…</p>}
              {!conversation.loading && conversation.error && !conversation.missing && (
                <p role="alert">
                  {conversation.error}{" "}
                  <button type="button" className="secondary compact" onClick={conversation.reload}>
                    重新加载会话
                  </button>
                </p>
              )}
              {conversation.state?.nextCursor && (
                <button className="secondary" onClick={conversation.older}>
                  加载更早记录
                </button>
              )}
              {turns.map((turn) => {
                const run = conversation.runs.find((view) => view.runId === turn.id);
                return (
                  <AnalysisRunTurn
                    key={turn.id}
                    contextOpsHref={
                      run
                        ? `${payload.session.navigation.find((p) => p.id === "contextops")?.href ?? ""}?run=${run.runId}`
                        : undefined
                    }
                    turn={turn}
                    retry={
                      busy || question.trim()
                        ? undefined
                        : () => {
                            setQuestion(turn.question);
                            api.setDirty(true);
                            input.current?.focus();
                          }
                    }
                    onDownload={downloadFile}
                    onReveal={revealOutput}
                    scope={workflowScope}
                    pluginHref={workflowPluginHref}
                    saved
                    durable={
                      run && sessionId
                        ? { run, sessionId, reload: conversation.reload, editGoal }
                        : undefined
                    }
                    partialText={
                      Object.values(run?.messages ?? {}).find((m) => m.role === "assistant")
                        ?.content.text
                    }
                  />
                );
              })}
            </div>
          </>
        }
      />
    );
  return (
    <NewAnalysisPage
      chatRef={chat}
      dockRef={composerDock}
      composer={composer}
      goal={goalPanel}
      suggestions={
        <div className="analysis-suggestions" role="group" aria-label="候选提示词">
          {starters.failed && (
            <p role="status">
              预填问题暂时无法读取，可直接输入。{" "}
              <button className="secondary compact" onClick={starters.retry}>
                重新加载预填
              </button>
            </p>
          )}
          {starters.cards?.analysis?.map((card) => (
            <section className="analysis-prompt-card" key={card.title} aria-label={card.title}>
              <h3>{card.title}</h3>
              <ul>
                {card.suggestions.map(({ label, question, constraints }) => (
                  <li key={label}>
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy}
                      onClick={() => {
                        setQuestion(question);
                        setEntry(
                          constraints && isModelContract("RunConstraints", constraints)
                            ? { constraints: constraints as RunConstraints }
                            : {},
                        );
                        api.setDirty(true);
                        input.current?.focus();
                      }}
                    >
                      <ChartNoAxesColumn size={16} aria-hidden="true" />
                      <span>{label}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      }
    />
  );
}
