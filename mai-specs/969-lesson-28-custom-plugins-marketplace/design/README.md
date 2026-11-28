# 第 28 课技术方案与离线阅读版维护说明

- 规范源技术方案：[TECH_DESIGN.md](TECH_DESIGN.md)
- 代码设计附录：[CODE_DESIGN.md](CODE_DESIGN.md)
- 离线阅读编辑源：[document/guide.md](document/guide.md)、[document/edition.json](document/edition.json)
- 离线 HTML 阅读版：[TECH_DESIGN.html](TECH_DESIGN.html)

## 重建与检查命令

从 Compass 仓库根目录（`learner-kit/code/compass`）执行：

```bash
node scripts/course-design/reader.mjs \
  mai-specs/969-lesson-28-custom-plugins-marketplace/design/document/edition.json

node .agents/skills/my-design-to-html/scripts/check-html.mjs \
  mai-specs/969-lesson-28-custom-plugins-marketplace/design/TECH_DESIGN.html \
  --report-dir work/969-design-html-qa
```
