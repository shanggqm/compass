# 971 设计目录

| 文件                                       | 用途                                       |
| ------------------------------------------ | ------------------------------------------ |
| [TECH_DESIGN.md](TECH_DESIGN.md)           | 技术方案（有效设计源）                     |
| [CODE_DESIGN.md](CODE_DESIGN.md)           | 供评审的代码设计附录                       |
| [compass-contextops-tech-selection.png](compass-contextops-tech-selection.png) | Compass 技术选型方案图：在线 PEP、OTel / Langfuse 与 PG 双路消费、控制台、只读 MCP 和确定性回放 |
| [compass-context-security-mechanisms.png](compass-context-security-mechanisms.png) | 安全机制与技术方案详图：信任分级、同步 PEP、派生权限、快照加密、复权与审计关口 |
| [compass-contextops-observability-views.png](compass-contextops-observability-views.png) | ContextOps 后台观测设计图：双路采集、五个标签页、三轨瀑布、节点详情、异常下钻与局部回放 |
| [TECH_DESIGN.html](TECH_DESIGN.html)       | 主方案课程阅读版 HTML，由 `document/` 生成 |
| [document/guide.md](document/guide.md)     | 主方案阅读版正文                           |
| [document/edition.json](document/edition.json) | 主方案阅读版生成配置                   |

重建阅读版（从 Compass 根目录执行）：

```bash
node scripts/course-design/reader.mjs mai-specs/971-lesson-26-context-ops/design/document/edition.json
```

修改源 Markdown 后，先同步对应 `guide.md`，再重建 HTML。
