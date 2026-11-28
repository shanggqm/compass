"use client";

import { useState } from "react";
import { UiCodeBlock } from "@/components/ui/code-block";

type Schema = { description?: string; properties?: Record<string, Schema>; [key: string]: unknown };
type ToolDocument = {
  type: string;
  function: { name: string; description: string; parameters: Schema };
  examples: unknown[];
};
const pretty = (value: unknown) => JSON.stringify(value, null, 2);

export function ToolFields({
  body,
  disabled,
  onChange,
  onValidation,
}: {
  body: string;
  disabled: boolean;
  onChange: (body: string) => void;
  onValidation: (error: string) => void;
}) {
  const document = JSON.parse(body) as ToolDocument;
  const [examples, setExamples] = useState(pretty(document.examples ?? []));
  const [exampleError, setExampleError] = useState("");
  function update(apply: (next: ToolDocument) => void) {
    const next = structuredClone(document);
    apply(next);
    onChange(pretty(next) + "\n");
  }
  return (
    <div className="prompt-form">
      <label>
        工具名称
        <input value={document.function.name} readOnly />
      </label>
      <label>
        工具描述
        <textarea
          rows={4}
          maxLength={1200}
          value={document.function.description}
          disabled={disabled}
          onChange={(event) =>
            update((next) => {
              next.function.description = event.target.value;
            })
          }
        />
      </label>
      {Object.entries(document.function.parameters.properties ?? {}).map(([name, parameter]) => (
        <label key={name}>
          参数说明 · {name}
          <input
            value={parameter.description ?? ""}
            maxLength={8000}
            disabled={disabled}
            onChange={(event) =>
              update((next) => {
                next.function.parameters.properties![name].description = event.target.value;
              })
            }
          />
        </label>
      ))}
      <label>
        调用示例（JSON）
        <textarea
          rows={8}
          value={examples}
          disabled={disabled}
          spellCheck={false}
          aria-invalid={!!exampleError}
          aria-describedby="tool-examples-help"
          onChange={(event) => {
            const text = event.target.value;
            setExamples(text);
            try {
              const value: unknown = JSON.parse(text);
              if (!Array.isArray(value) || value.length > 3) throw Error("请填写 JSON 数组。");
              setExampleError("");
              onValidation("");
              update((next) => {
                next.examples = value;
              });
            } catch {
              const error = "调用示例须为有效的 JSON 数组。";
              setExampleError(error);
              onValidation(error);
            }
          }}
        />
      </label>
      <p className="muted" id="tool-examples-help">
        每条示例包含 description 和 arguments；参数须符合下方代码定义的 Schema。描述与示例合计最多
        1200 个字符，最多 3 条示例。
      </p>
      <p className="muted">参数名称、类型、必填项和约束随代码发布。此处仅编辑描述与示例。</p>
      <UiCodeBlock
        code={pretty(document)}
        language="json"
        title="完整工具定义 · 参数 Schema 只读"
      />
    </div>
  );
}
