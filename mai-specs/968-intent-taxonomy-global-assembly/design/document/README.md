# 离线阅读版生成说明

本目录维护 Feature 968 技术方案与代码设计附录的离线 HTML 阅读版配置与导读正文，采用 Compass 统一离线阅读样式（`scripts/course-design/reader.mjs`）。

## 源文件

- 主技术方案：`../TECH_DESIGN.md`（配置 `edition.json` + 导读 `guide.md` → `../TECH_DESIGN.html`）
- 代码设计附录：`../CODE_DESIGN.md`（配置 `code-edition.json` + 导读 `code-guide.md` → `../CODE_DESIGN.html`）

## 重建与检查命令（从 Compass 根目录执行）

```bash
node scripts/course-design/reader.mjs mai-specs/968-intent-taxonomy-global-assembly/design/document/edition.json
node scripts/course-design/reader.mjs mai-specs/968-intent-taxonomy-global-assembly/design/document/code-edition.json

node .agents/skills/my-design-to-html/scripts/check-html.mjs \
  mai-specs/968-intent-taxonomy-global-assembly/design/TECH_DESIGN.html \
  --report-dir work/design-preview/968-tech

node .agents/skills/my-design-to-html/scripts/check-html.mjs \
  mai-specs/968-intent-taxonomy-global-assembly/design/CODE_DESIGN.html \
  --report-dir work/design-preview/968-code
```
