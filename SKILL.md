---
name: job-matcher
description: 根据用户简历(CV)和求职意向，抽取CV结构化字段、实时检索匹配职位、生成可交互HTML报告。当用户提供简历文件(pdf/docx/txt/md)或粘贴简历文本，并希望找工作、匹配职位、获取职位推荐、做求职匹配时使用。
---

# Job Matcher

把简历(CV) + 求职意向 变成一份匹配职位的可交互 HTML 报告。

**执行方式：先读 [`WORKFLOW.md`](WORKFLOW.md)，按它的 0–7 步流程做。** WORKFLOW.md 是 agent-中立的单一事实源，包含：所需能力、如何映射到你当前运行时的工具、脚本调用契约、缺能力时的降级策略。本文件兼容 Claude Code 与 Codex 的 skill 机制（同样的 `name`/`description` + `scripts/`、`references/`、`assets/` 结构）。


## 能力映射（本文件唯一的职责）

WORKFLOW.md 是 agent-中立的，它说「用你的 web 搜索工具」。下表把那些能力换成本运行时的具体工具；**流程规则不在这里，全在 WORKFLOW.md**，两处都写会漂。

| 能力 | 必需性 | 各 agent 对应 | 缺失时 |
|------|:---:|------|------|
| **模型/Web 搜索** | 可选发现路径 | Claude: `WebSearch`；Codex: 内置 web 搜索 | 降级到已就绪的本机浏览器；两者都缺失则无法实时检索 |
| **子代理**（并行+隔离） | 可选 | Claude: `Task`/`Agent`（单消息内并行 spawn）；Codex: custom agents | **降级为串行** |
| **网页抓取** | 可选 | Claude: `WebFetch` | **回退** `scripts/fetch_rendered.py` |
| **本机浏览器发现** | 可选发现路径 | 当前运行时已连接、已授权且具备 tabs/navigate/read 的 BrowserOS Neo 或用户浏览器工具 | 继续模型/Web 搜索；绝不读取 Cookie 文件 |

Claude Code 特有的两点：

- **子代理并行**：`Task`/`Agent` 支持在**同一条消息里**同时 spawn 多个 worker，WORKFLOW 的「批间重叠」和并行搜索/评估都靠这个；没有它就按 WORKFLOW 的降级列串行做。
- **本机浏览器**：只使用当前会话已暴露、已授权的浏览器工具（BrowserOS Neo 或用户浏览器），按 WORKFLOW 第 0 步用 `scripts/local_browser_probe.py probe` 确认状态。**不扫描端口、浏览器配置、Cookie 或既有标签页**；已安装不等于 `ready`。

## 其余

流程、脚本契约、容错阶梯、护栏、配置、降级 —— 全部见 [`WORKFLOW.md`](WORKFLOW.md)（规则的理由在 [`docs/rationale.md`](docs/rationale.md)）。指令文档在 `references/`。
