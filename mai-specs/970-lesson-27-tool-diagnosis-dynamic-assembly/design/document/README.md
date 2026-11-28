# 第 27 课技术方案离线阅读版生成入口

本目录维护 `970-lesson-27-tool-diagnosis-dynamic-assembly` 技术方案、20 个工具 ACI Schema 对照专题及代码设计附录的离线 HTML 生成配置，遵循 [`docs/ui-style-guide.md`](../../../../docs/ui-style-guide.md#技术方案-html-阅读版) 规范，在调用共用 `applyCourseReader` 基础上将相邻 `[BEFORE]` / `[AFTER]` 工具 JSON 声明渲染为桌面左右并排、窄屏自动堆叠的深色代码块逐行 Diff。

## 源文件与产物映射

| 配置文件 | 阅读编排源 | 权威技术源 | 输出 HTML |
| --- | --- | --- | --- |
| [`edition.json`](./edition.json) | [`guide.md`](./guide.md) | [`../TECH_DESIGN.md`](../TECH_DESIGN.md)、[`../TOOL_CONTRACTS.md`](../TOOL_CONTRACTS.md)、[`../CODE_DESIGN.md`](../CODE_DESIGN.md) | [`../TECH_DESIGN.html`](../TECH_DESIGN.html) |
| [`contracts-edition.json`](./contracts-edition.json) | [`contracts-guide.md`](./contracts-guide.md) | [`../TOOL_CONTRACTS.md`](../TOOL_CONTRACTS.md) | [`../TOOL_CONTRACTS.html`](../TOOL_CONTRACTS.html) |
| [`code-edition.json`](./code-edition.json) | [`code-guide.md`](./code-guide.md) | [`../CODE_DESIGN.md`](../CODE_DESIGN.md) | [`../CODE_DESIGN.html`](../CODE_DESIGN.html) |

## 重建命令

从 Compass 根目录（`learner-kit/code/compass`）执行：

```bash
node mai-specs/970-lesson-27-tool-diagnosis-dynamic-assembly/design/document/generate-contracts.mjs
node mai-specs/970-lesson-27-tool-diagnosis-dynamic-assembly/design/document/build-html.mjs mai-specs/970-lesson-27-tool-diagnosis-dynamic-assembly/design/document/edition.json
node mai-specs/970-lesson-27-tool-diagnosis-dynamic-assembly/design/document/build-html.mjs mai-specs/970-lesson-27-tool-diagnosis-dynamic-assembly/design/document/contracts-edition.json
node mai-specs/970-lesson-27-tool-diagnosis-dynamic-assembly/design/document/build-html.mjs mai-specs/970-lesson-27-tool-diagnosis-dynamic-assembly/design/document/code-edition.json
```
