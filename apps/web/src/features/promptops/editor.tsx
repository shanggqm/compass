"use client";
import { useRef, useState } from "react";
import {
  Check,
  Code2,
  Download,
  Eye,
  GitCompareArrows,
  History,
  LoaderCircle,
  Play,
  Rocket,
  Save,
  SlidersHorizontal,
} from "lucide-react";
import { ApiError } from "@/client/request-scope";
import { AnalysisMarkdown } from "@/components/analysis-markdown";
import { UiCodeBlock } from "@/components/ui/code-block";
import { useDocumentSession } from "@/components/shell/session-boundary";
import { errorText, versionLabel, type Ops, type FileView } from "./client";
import { ToolFields } from "./tool-fields";

export function FileEditor({
  ops,
  file,
  onPublish,
  onHistory,
  onSaved,
  onDirty,
}: {
  ops: Ops;
  file: FileView;
  onPublish: () => void;
  onHistory: () => void;
  onSaved: (msg: string) => void;
  onDirty: (dirty: boolean) => void;
}) {
  const session = useDocumentSession();
  const [base, setBase] = useState(file);
  const [body, setBody] = useState(file.draft_body ?? file.body ?? "");
  const [sample, setSample] = useState<Record<string, string>>({});
  const [view, setView] = useState<"source" | "markdown" | "sample">("source");
  const [rendered, setRendered] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [validation, setValidation] = useState("");
  const validationRef = useRef("");
  const structured = file.path.endsWith(".json");
  const [conflict, setConflict] = useState<FileView | null>(null);
  const [requiresReview, setRequiresReview] = useState(
    file.has_draft && file.draft_base_epoch !== file.active_epoch,
  );
  const dirty = body !== (base.draft_body ?? base.body ?? "");
  function change(value: string) {
    setBody(value);
    setRendered(null);
    setMessage("");
    onDirty(!!validationRef.current || value !== (base.draft_body ?? base.body ?? ""));
  }
  async function save() {
    setMessage("");
    setSaving(true);
    try {
      await ops.write("file-draft", {
        path: file.path,
        body,
        expected_edit_rev: base.edit_rev,
        base_epoch: base.active_epoch,
      });
      onDirty(false);
      onSaved("草稿已保存。");
    } catch (error) {
      setMessage(errorText(error));
      if (error instanceof ApiError && error.status === 409) {
        setRequiresReview(true);
        await compare();
      }
    } finally {
      setSaving(false);
    }
  }
  async function compare() {
    try {
      setConflict(await ops.get("file", { path: file.path }));
    } catch (err) {
      setMessage(errorText(err));
    }
  }
  async function render() {
    setMessage("");
    setSaving(true);
    setRendered(null);
    try {
      const result = await ops.preview("render-preview", {
        path: file.path,
        body,
        variables: sample,
      });
      setRendered(result.rendered);
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      setSaving(false);
    }
  }
  function download() {
    const blob = new Blob([body], {
      type: structured ? "application/json;charset=utf-8" : "text/markdown;charset=utf-8",
    });
    session.download(blob, file.path.split("/").at(-1) ?? "prompt.md");
  }
  return (
    <section className="prompt-editor" aria-label="文件编辑器">
      <div className="prompt-head">
        <div>
          <h2>{file.path.split("/").at(-1)}</h2>
          <p className="prompt-path">{file.path}</p>
        </div>
        <span className="status status-muted">
          {versionLabel(file.current_version)} ·{" "}
          {dirty ? "未保存" : file.has_draft ? "已保存草稿" : "生效正文"}
        </span>
      </div>
      <p className="muted">
        使用位置　{file.usages.join("、") || "尚未接入"} ·{" "}
        {file.origin === "online" ? "线上新增" : "随包文件"}
      </p>
      {file.governance && (
        <p className="muted" aria-label="工具治理信息">
          加载：
          {
            { always_on: "默认常驻", scene_routed: "按场景", user_selected: "用户选择" }[
              file.governance.channel
            ]
          }{" "}
          · 能力包：{file.governance.pack} · 效果：{file.governance.effect} · 审批：
          {file.governance.approval === "required" ? "需要确认" : "无需确认"}
        </p>
      )}
      <div className="prompt-actions prompt-file-actions" aria-label="文件操作">
        <button className="secondary" onClick={onHistory}>
          <History size={16} aria-hidden="true" /> 版本历史
        </button>
        <button className="secondary" onClick={download}>
          <Download size={16} aria-hidden="true" /> {structured ? "下载 JSON" : "下载 Markdown"}
        </button>
        <button
          onClick={() => void save()}
          disabled={
            saving ||
            !!validation ||
            requiresReview ||
            (!dirty && !(base.has_draft && base.draft_base_epoch !== base.active_epoch))
          }
        >
          {saving ? (
            <LoaderCircle size={16} className="ui-spinner" aria-hidden="true" />
          ) : (
            <Save size={16} aria-hidden="true" />
          )}
          {saving ? "正在处理…" : "保存草稿"}
        </button>
        <button
          className="secondary"
          disabled={saving || dirty || !!validation || requiresReview || !file.has_draft}
          onClick={onPublish}
        >
          <Rocket size={16} aria-hidden="true" /> 发布此文件
        </button>
      </div>
      <div className="prompt-tabs" aria-label="正文视图">
        {(
          [
            ["source", "源码编辑", Code2],
            ["markdown", structured ? "JSON 预览" : "Markdown 预览", Eye],
            ...(!structured ? [["sample", "模板试填", SlidersHorizontal] as const] : []),
          ] as const
        ).map(([id, label, Icon]) => (
          <button
            key={id}
            className="secondary"
            aria-pressed={id === view}
            onClick={() => setView(id)}
          >
            <Icon size={16} aria-hidden="true" /> {label}
          </button>
        ))}
      </div>
      {file.kind === "tool" && (
        <div hidden={view !== "source"}>
          <ToolFields
            body={body}
            disabled={saving}
            onChange={change}
            onValidation={(error) => {
              validationRef.current = error;
              setValidation(error);
              onDirty(!!error || dirty);
            }}
          />
        </div>
      )}
      {view === "source" && file.kind === "tool" ? null : view === "source" ? (
        <label>
          {structured ? "JSON 正文" : "Markdown 正文"}
          <textarea
            rows={16}
            value={body}
            disabled={saving}
            spellCheck={false}
            onChange={(e) => change(e.target.value)}
          />
        </label>
      ) : view === "markdown" && structured ? (
        <div className="prompt-json-preview">
          <UiCodeBlock code={body} language="json" title="JSON 资产" />
        </div>
      ) : view === "markdown" ? (
        <div className="prompt-preview" tabIndex={0} role="region" aria-label="Markdown 预览内容">
          <AnalysisMarkdown source={body} embedded />
        </div>
      ) : (
        <div className="prompt-grid">
          {base.variables.map((v) => (
            <label key={v}>
              {v}
              <input
                value={sample[v] ?? ""}
                onChange={(e) => {
                  setSample({ ...sample, [v]: e.target.value });
                  setRendered(null);
                }}
              />
            </label>
          ))}
          {!base.variables.length && <p className="muted">本文件没有登记模板变量。</p>}
          <button className="secondary" disabled={saving} onClick={() => void render()}>
            <Play size={16} aria-hidden="true" /> 运行模板预览
          </button>
          {rendered !== null && (
            <div className="prompt-preview" tabIndex={0} role="region" aria-label="模板预览内容">
              <AnalysisMarkdown source={rendered} embedded />
            </div>
          )}
        </div>
      )}
      {(message || validation) && (
        <p role="alert" className="ui-notice">
          {message || validation}
        </p>
      )}
      {requiresReview && !conflict && (
        <button className="secondary" onClick={() => void compare()}>
          <GitCompareArrows size={16} aria-hidden="true" /> 核对最新草稿和生效版本
        </button>
      )}
      {conflict && (
        <div className="prompt-review">
          <h3>当前生效正文 · {versionLabel(conflict.current_version)}</h3>
          <pre tabIndex={0}>{conflict.body ?? "尚未发布"}</pre>
          {conflict.has_draft && (
            <>
              <h3>线上最新草稿</h3>
              <pre tabIndex={0}>{conflict.draft_body}</pre>
            </>
          )}
          <button
            className="secondary"
            onClick={() => {
              const revised = body !== (conflict.draft_body ?? conflict.body ?? "");
              setBase(conflict);
              setConflict(null);
              setRequiresReview(false);
              onDirty(revised);
              setMessage("已核对最新基线，可继续编辑并保存。");
            }}
          >
            <Check size={16} aria-hidden="true" /> 保留我的输入，使用此基线
          </button>
        </div>
      )}
    </section>
  );
}
