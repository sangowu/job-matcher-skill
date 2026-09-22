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
| **模型/Web 搜索** | 可选发现路径 | Claude: `WebSearch`；Codex: 内置 web 搜索 | 降级到已就绪的本机浏览器；两者都缺失则无法实时检索 |
| **子代理**（并行+隔离） | 可选 | Claude: `Task`/`Agent`（单消息内并行 spawn）；Codex: custom agents | **降级为串行** |
| **网页抓取** | 可选 | Claude: `WebFetch` | **回退** `scripts/fetch_rendered.py` |
| **本机浏览器发现** | 可选发现路径 | 当前运行时已连接、已授权且具备 tabs/navigate/read 的 BrowserOS Neo 或用户浏览器工具 | 继续模型/Web 搜索；绝不读取 Cookie 文件 |

- CV 抽取 / 搜索 / 打分这类重活交给子代理（若有）；委派时只回传「**摘要 + 文件路径**」，CV 全文 / 搜索原始结果 / JD 全文 **留在子代理或文件**，保持主上下文整洁。无子代理则你自己串行做，但仍坚持"大文本写文件、上下文只留摘要"。
- 搜索每条 query 恰好 1 次 web 搜索，计入 `config.json` 的 `max_websearch_calls`；并行度受 `max_parallel_subagents` 约束。
- 启动时按 [`docs/local-browser-phase2.md`](docs/local-browser-phase2.md) 核对当前运行时已暴露的工具，用 `scripts/local_browser_probe.py probe` 验证本机浏览器能力，再把状态交给 `scripts/discovery_mode.py plan`。默认 `coverage` 同时运行可用的浏览器与模型搜索；浏览器提供者单独按 BrowserOS Neo → 用户浏览器选择。旧 `auto` 保留单 route 兼容语义。安装 Neo 不等于 `ready`，不得扫描端口、浏览器配置、Cookie 或既有标签页。
- 生成 market plan 和 URL-free source health plan 后，必须用 [`scripts/discovery_plan.py`](scripts/discovery_plan.py) 连接公开来源目录，得到确定性的 browser/Web/structured 任务；不得让模型自行发明入口 URL。浏览器任务不含站点 selector，只能按 accessibility tree 语义操作，且 `automation_allowed=false` 的来源只能作为允许的 Web Search 提示。
- DiscoveryPlan 带有确定性 `waves`。先只执行 `initial_wave_id` 指向的任务；每个当前波次 task 必须恰好返回一个 `succeeded`/`failed`/`skipped` 终态，不能提前执行后续波次。登录、验证或限流先暂停并等待处理，不能伪报终态。把 `wave_id` 与完整 task results 一次性交给 `scripts/discovery_batch.py`，由它校验 task/CandidateEnvelope 对应关系、经唯一 merge 写入并决定是否派发 `next_wave_id`；worker 和调用方都不得自行判断或写入下一波。
- 选择浏览器 route 时，按 [`docs/local-browser-phase3-panel.md`](docs/local-browser-phase3-panel.md) 读取 localhost 面板设置并发布低基数事件。Cookie 默认用 `necessary_only`：仅当 [`scripts/cookie_consent.py`](scripts/cookie_consent.py) 对当前 accessibility consent dialog 返回唯一 `target_ref` 时，立即点击该“仅必要/拒绝可选”按钮；不得点击 `Accept All`、操作分类开关、沿用旧 ref 或读取 Cookie。`ask_every_time`、零/多匹配、非 dialog 或点击后仍有歧义时按 [`docs/cookie-consent.md`](docs/cookie-consent.md) 暂停单站并提醒用户。登录、验证或限流同样只暂停单站；面板不可用时改为聊天提醒，不能阻断其余 route。
- 浏览器候选只读取职位列表/详情子树，只返回 CandidateEnvelope 字段；不得把账户导航、通知数或个性化侧栏带入候选或日志。跨市场平台使用 `global_job_board`，只有详情页出现有效申请入口且无关闭信号时才标 `alive`。首次接入或回归先用 `scripts/browser_candidate_smoke.py` 做临时 merge 验证，正式运行再经主 agent 串行写入 `merge_jobs.py`。
- 每批 Web 候选交给 `ats_pipeline.py discover` 识别官方 ATS board；仅当 `ats_enabled` 为 true 时同步已验证/到期 board。ATS 使用独立请求预算，返回候选仍由主 agent 串行交给同一个 `merge_jobs.py`。
- 搜索与职位评估可以并行执行，但 worker 只返回结果；`jobs_table.json` 的 `merge/update` 必须由主 agent 串行提交。按 `WORKFLOW.md` 使用 `eval_run` 快照和 `run_id`，不要让 worker 直接写共享主表。
- **批间重叠**：第 N 批 `merge` 拿到 `eval_run` 后，在同一条消息里并行 spawn「第 N 批评估 worker + 第 N+1 批搜索 worker」（Claude 的 Task/Agent 支持单消息并行）；`max_parallel_subagents` 是搜索+评估共用的全局预算，重叠期建议 1 搜 + 2 评。
- `merge/update` 会写入 PII-safe 运行指标；若返回 `metrics_recorded:false`，应告知用户。健康汇总使用 `scripts/summarize_metrics.py`。
- 每次启动先运行 `scripts/version_check.py`。只在 `different`、`version_different` 或 `local_modified` 时简短提醒；`unknown` 不阻断流程。检查器只读且有 24 小时缓存，绝不能自行 `git pull` 或覆盖本地文件。
- 子代理创建前用 `scripts/subagent_metrics.py profile` 解析角色模型/effort；创建后记录实际生效配置、耗时、成功率所需计数与 fallback。运行时不支持覆盖时继承当前模型并如实标记，不得伪报。
- 可选远程隔离浏览器只作为最终抓取兜底：先通过 `scripts/browser_setup.py` 配置 Kernel BYOK，再由 browser 子代理使用 `scripts/browser_control.py` 视觉操作。单站翻页串行、不同网站可并行；验证码/登录/限流只做人工接管或降级。
- 缺目标职位 / 地点完全缺失 → 停下追问用户。

## 其余

脚本契约、容错阶梯、护栏、配置、降级 —— 全部见 [`WORKFLOW.md`](WORKFLOW.md)。
指令文档在 `references/`，完整流程见 [`WORKFLOW.md`](WORKFLOW.md)。
