# 983 设计目录

| 文件                                                         | 用途                                                            |
| ------------------------------------------------------------ | --------------------------------------------------------------- |
| [TECH_DESIGN.md](TECH_DESIGN.md)                             | 技术方案（有效设计源）                                          |
| [CODE_DESIGN.md](CODE_DESIGN.md)                             | 供评审的代码设计附录                                            |
| [PROMPT_DESIGN.md](PROMPT_DESIGN.md)                         | 提示词、工具清单与工具描述附录                                  |
| [TECH_DESIGN.html](TECH_DESIGN.html)                         | 主方案课程阅读版 HTML，由 `document/` 生成，勿手改              |
| [PROMPT_DESIGN.html](PROMPT_DESIGN.html)                     | 提示词与工具设计阅读版 HTML，由 `document/prompt/` 生成，勿手改 |
| [document/guide.md](document/guide.md)                       | 主方案阅读版正文                                                |
| [document/edition.json](document/edition.json)               | 主方案阅读版生成配置                                            |
| [document/prompt/guide.md](document/prompt/guide.md)         | 提示词阅读版正文                                                |
| [document/prompt/edition.json](document/prompt/edition.json) | 提示词阅读版生成配置                                            |

重建阅读版（从 Compass 根目录执行）：

```bash
node scripts/course-design/reader.mjs mai-specs/983-lesson-25-long-task-context/design/document/edition.json
node scripts/course-design/reader.mjs mai-specs/983-lesson-25-long-task-context/design/document/prompt/edition.json
```

修改源 Markdown 后，先同步对应 `guide.md`，再重建 HTML。
