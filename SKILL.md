---
name: job-matcher
description: 根据用户简历(CV)和求职意向，抽取CV结构化字段、实时检索匹配职位、生成可交互HTML报告。当用户提供简历文件(pdf/docx/txt/md)或粘贴简历文本，并希望找工作、匹配职位、获取职位推荐、做求职匹配时使用。
---

# Job Matcher

把简历(CV) + 求职意向 变成一份匹配职位的可交互 HTML 报告。

**执行方式：先读 [`WORKFLOW.md`](WORKFLOW.md)，按它的 0–7 步流程做。** WORKFLOW.md 是 agent-中立的单一事实源，包含：所需能力、如何映射到你当前运行时的工具、脚本调用契约、缺能力时的降级策略。本文件兼容 Claude Code 与 Codex 的 skill 机制（同样的 `name`/`description` + `scripts/`、`references/`、`assets/` 结构）。

## 执行要点（任何 agent 通用）

把流程里的三种能力映射到**你自己运行时的工具**：

| 能力 | 必需性 | 各 agent 对应 | 缺失时 |
|------|:---:|------|------|
| **web 搜索** | 必需 | Claude: `WebSearch`；Codex: 内置 web 搜索 | 无法做职位检索 |
| **子代理**（并行+隔离） | 可选 | Claude: `Task`/`Agent`（单消息内并行 spawn）；Codex: custom agents | **降级为串行** |
| **网页抓取** | 可选 | Claude: `WebFetch` | **回退** `scripts/fetch_rendered.py` |

- CV 抽取 / 搜索 / 打分这类重活交给子代理（若有）；委派时只回传「**摘要 + 文件路径**」，CV 全文 / 搜索原始结果 / JD 全文 **留在子代理或文件**，保持主上下文整洁。无子代理则你自己串行做，但仍坚持"大文本写文件、上下文只留摘要"。
- 搜索每条 query 恰好 1 次 web 搜索，计入 `config.json` 的 `max_websearch_calls`；并行度受 `max_parallel_subagents` 约束。
- 搜索与职位评估可以并行执行，但 worker 只返回结果；`jobs_table.json` 的 `merge/update` 必须由主 agent 串行提交。按 `WORKFLOW.md` 使用 `eval_run` 快照和 `run_id`，不要让 worker 直接写共享主表。
- `merge/update` 会写入 PII-safe 运行指标；若返回 `metrics_recorded:false`，应告知用户。健康汇总使用 `scripts/summarize_metrics.py`。
- 缺目标职位 / 地点完全缺失 → 停下追问用户。

## 其余

脚本契约、容错阶梯、护栏、配置、降级 —— 全部见 [`WORKFLOW.md`](WORKFLOW.md)。
指令文档在 `references/`，完整流程见 [`WORKFLOW.md`](WORKFLOW.md)。
