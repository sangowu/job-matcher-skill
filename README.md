# job-matcher

[English](README.en.md) | **中文**

版本文档：[更新记录](CHANGELOG.md) · [v2.3.0 发布说明](docs/releases/v2.3.0.md) · [v2.2.0 发布说明](docs/releases/v2.2.0.md) · [v2.1.0 发布说明](docs/releases/v2.1.0.md) · [v2.0.0 发布说明](docs/releases/v2.0.0.md)

> 一个 **agent skill（Claude Code 与 Codex 通用）**：输入**简历(CV) + 求职意向**，自动抽取简历字段、用 **web 搜索实时检索**匹配职位，生成一份**可交互的 HTML 报告**。

是 [JobRadar](https://github.com/sangowu/JobRadar) 的**轻量版**——默认只依赖 agent 原生能力（web 搜索 + 子代理 + Python 脚本），并可选接入 BYOK 隔离浏览器，借鉴 JobRadar 的 schema、算法与界面风格。

---

## ✨ 功能

- 📄 **简历解析**：支持 PDF / DOCX / TXT / MD，或直接粘贴文本（不做 OCR）。
- 🧠 **结构化抽取**：抽取目标职位、技能、资历(seniority)、地点、语言等，自动按相关年限定级。
- 🔎 **实时职位检索**：基于 WebSearch 自适应分批搜索；按 CV 语言选职位词，按目标地点叠加当地平台（爱尔兰/英国、欧陆、澳新、中国大陆，其余市场按语言+地点推断）。
- 🎯 **5 维匹配打分**：title / seniority / skills / location / must-have，输出五档投递建议（强烈投递→跳过）。资历硬规则与契约校验拦截打分漂移。
- 🗂️ **增量缓存**：CV、JD、匹配分三层缓存；多来源同职位自动聚合（含区域平台 job-id 强命中）；换 query 自动失效重算。
- 🛡️ **不可信输入隔离**：搜索结果与 JD 正文按纯数据处理并忽略其中指令；报告内嵌 JSON 转义、链接限 http(s)。
- 📊 **可交互报告**：两栏布局（左职位列表 30% + 右详情 70%）+ 评分徽章 + 深色模式 + 排序/筛选/搜索 + 7/30 天运行健康快照 + 中英 i18n，自包含单文件 HTML。
- 🌐 **可选隔离浏览器**：Kernel BYOK 作为最终抓取兜底，支持受限列表翻页、视觉控制和 Live View 人工接管；默认关闭，CI 使用 Fake Provider。

## 🏗️ 架构

- **主 agent = 编排者**：调脚本、融合 query、追问用户、spawn subagent。
- **subagent 承担重上下文工作**（CV 抽取 / 搜索 / 打分）：大块原始文本留在 subagent，主上下文只搬「路径 + 小 JSON」，保持整洁。
- **Python 脚本承担确定性工作**：解析、校验、去重聚合缓存、失效验证、渲染。
- **并行计算、串行提交**：第 N 批评估与第 N+1 批搜索并行发出（批间重叠）；`jobs_table.json` 只有一个写入路径，使用评估快照、跨进程锁和原子替换防止丢失更新。超龄未完成的快照会在下次 merge 时作废回收，职位不会永久卡在评估中。

```
CV + query
   │ [脚本] extract_cv          → 纯文本 + cv_hash
   │ [缓存检查]                 → 命中则跳过抽取
   │ [subagent] 抽取 CVProfile  → [脚本] validate_profile
   │ [主agent] 融合 query       → search_plan + candidate_profile
   │ [并行 subagent] WebSearch+解析+初筛 → [脚本] merge_jobs(去重/聚合/缓存+评估快照)
   │ [并行 subagent] 粗排→精排抓JD+5维打分 + 失效验证 → [脚本] 条件化回写
   │ [脚本] render_html         → report_*.html（自动打开）
   ▼
可交互 HTML 报告
```

**容错阶梯**（失效验证 & JD 抓取共用）：`WebFetch → requests 静态抓 → 本机 headless → 可选远程隔离浏览器 → 标注未验证不阻塞`。

## 📁 结构

```
job-matcher/
├── SKILL.md              # 触发描述 + 编排入口
├── WORKFLOW.md           # agent-中立完整流程
├── config.json           # 配置旋钮
├── docs/monitoring.md     # 运行指标、阈值与健康汇总
├── references/           # subagent 按需读取的指令
│   ├── cv_schema.md          # CV 抽取规则
│   ├── scoring_rubric.md     # 5 维打分 + 五档阈值
│   ├── search_playbook.md    # fan-out / 分市场 / 自适应分批
│   └── ats_phase1_boards.json # ATS 小基线公开公司样本
├── scripts/              # 确定性 Python 脚本
│   ├── extract_cv.py         # 解析 CV → 文本 + hash
│   ├── validate_profile.py   # 校验 + seniority→levels 映射
│   ├── analysis_contract.py  # 校验 JDProfile/MatchScore worker 输出
│   ├── merge_jobs.py         # 单写入器：去重/缓存/评估快照/条件化回写
│   ├── runtime_metrics.py    # PII-safe JSONL 指标与健康计算
│   ├── summarize_metrics.py  # 7/30 天 Markdown/JSON 健康报告
│   ├── round_timer.py        # 整轮计时，按编排模式对比墙钟
│   ├── subagent_metrics.py   # 子代理模型/effort 配置与结果指标
│   ├── browser_provider.py   # Kernel/Fake Provider 与安全配置
│   ├── browser_control.py    # 远程视觉浏览器控制命令
│   ├── browser_setup.py      # 一次性 localhost 配置页面
│   ├── browser_workflow.py   # 列表翻页/暂停状态机
│   ├── benchmark_pipeline.py # 固定小数据集核心/Fake Provider 基准
│   ├── benchmark_ats.py      # 公开 ATS API 的有界脱敏基线（非生产适配器）
│   ├── cp_hash.py            # 稳定的 candidate_profile hash
│   ├── verify_jobs.py        # 失效职位状态码检测
│   ├── fetch_rendered.py     # headless 渲染兜底（复用系统浏览器）
│   ├── render_html.py        # 渲染 HTML 报告
│   └── _jobutil.py           # 共享：归一化/去重键/URL 规范化
├── assets/template.html  # 静态报告模板（Tailwind + 纯 JS）
└── data/                 # 运行时数据（.gitignore，含 PII）
```

## 🚀 使用

克隆到个人 skill 目录（目录名用 `job-matcher`，与 skill 名一致）：

```bash
# Claude Code
git clone https://github.com/sangowu/job-matcher-skill ~/.claude/skills/job-matcher
# Codex
git clone https://github.com/sangowu/job-matcher-skill ~/.agents/skills/job-matcher
```

agent 会自动识别。然后在对话里：

> 这是我的简历 `D:\cv.pdf`，帮我找远程后端职位

或直接粘贴简历文本 + 求职意向。skill 会走完整流程并在浏览器打开报告。

## ⚙️ 配置

`config.json` 集中所有旋钮：

| 键 | 默认 | 说明 |
|----|------|------|
| `top_n` | 15 | 最终展示职位数 |
| `precise_buffer` | 5 | 精排多抓缓冲 |
| `max_parallel_subagents` | 3 | 批内并行上限 |
| `subagent_profiles` | 见配置 | 各角色请求的 model、reasoning effort 与隔离上下文策略 |
| `max_websearch_calls` | 6 | WebSearch 总次数上限 |
| `stop_threshold` | 12 | 净有效职位达标停止 |
| `consecutive_empty_stop` | 2 | 连续 N 批 0 结果则停止 |
| `jd_ttl_days` | 30 | JD 缓存有效期 |
| `seniority_mode` | balanced | strict / balanced / stretch |
| `enable_headless_fallback` | true | headless 兜底开关 |
| `headless_budget` | 3 | 每次运行 headless 上限 |
| `remote_browser_enabled` | false | 是否启用远程隔离浏览器最终兜底 |
| `browser_provider` | kernel | `kernel`；`fake` 仅供测试 |
| `browser_max_concurrency` | 2 | 远程浏览器并发硬上限 |
| `browser_max_pages` | 3 | 单个招聘列表串行翻页硬上限 |
| `browser_session_budget` | 10 | 单轮新建远程会话硬上限 |
| `browser_cost_limit_usd` | 1.0 | 单轮估算费用硬上限（美元） |
| `browser_handoff_timeout_minutes` | 10 | 人工接管等待硬上限（分钟） |
| `browser_allow_handoff` | true | 是否允许通过临时 Live View 交给用户处理 |
| `browser_timeout_seconds` | 600 | 单个远程会话超时硬上限 |
| `browser_headless` | false | Provider 是否隐藏浏览器；默认保留可接管视图 |
| `browser_stealth` | false | stealth 开关；默认关闭且不用于绕过验证 |
| `table_lock_timeout_seconds` | 10 | 等待主表写锁的最长秒数 |
| `stale_lock_seconds` | 120 | 回收异常遗留锁的时间阈值 |
| `eval_run_stale_hours` | 2 | 作废未完成评估快照的时间阈值 |
| `monitoring_default_window_days` | 7 | 默认健康报告窗口 |
| `monitoring_thresholds` | 见配置 | 冲突、拒绝、成功率、锁等待和积压阈值 |

运行时只保留一个职位主表 `data/jobs_table.json`。`record_id` 是稳定记录/评估主键，`identity_keys` 保存平台职位 ID；公司 + 标题 `dedup_key` 仅作兼容弱匹配。两个不相交的强 ID 不会因同公司同标题而误合并，弱键匹配还要求地点兼容且结果唯一。旧表会在下一次 merge/update 时原位补齐身份字段。每轮待评估职位写入最小化快照 `data/eval_runs/<run_id>.json`；worker 完成后由主 agent 串行回写评估字段，成功完成的快照会释放，仅在 `history.jsonl` 留下不含 CV/JD 正文的摘要。每次 merge/update 另写一条 PII-safe `data/metrics.jsonl` 事件。

远程浏览器为可选功能。安装依赖后运行一次性本地设置页；页面只绑定 `127.0.0.1`，连接测试成功后把 API Key 保存到系统密钥库，非敏感设置保存到已忽略的 `data/browser_provider.json`：

```text
python -m pip install "kernel>=0.94,<1" keyring
python scripts/browser_setup.py
python scripts/browser_control.py test
```

无界面环境也可通过 `KERNEL_API_KEY` 提供密钥。控制脚本的 `create/screenshot/click/type/press/scroll/close` 供 browser 子代理进行视觉控制；Live View URL 只临时返回，不写入指标或文件。

## 📈 运行监控

```text
python scripts/summarize_metrics.py --days 7 --format markdown
python scripts/summarize_metrics.py --days 30 --format json
python scripts/summarize_metrics.py --fail-on-breach
```

报告包含吞吐/缓存、评估成功/拒绝/冲突率、子代理按实际模型与 effort 的成功率/有效结果率/回退率、浏览器会话与接管计数、命令与锁等待 p50/p95/p99，以及积压状态。每次生成 HTML 时会自动嵌入 7/30 天静态快照；默认阈值违规时状态为 `degraded`。CLI 的 `--fail-on-breach` 同时返回退出码 2。字段定义、隐私边界和接入方式见 [运行时监控文档](docs/monitoring.md)。

整轮墙钟另行采集——脚本级耗时相比编排者在两次调用之间的搜索与评估工作可以忽略，无法回答「批间重叠值不值」：

```text
python scripts/round_timer.py start          # → {"round_id": "round-..."}
python scripts/round_timer.py finish --round-id <R> --orchestration overlapped|serial
```

汇总按编排模式给出 p50/p95 与 `overlap_saving_pct`；两种模式都有样本前显示 `n/a`。

版本性能回归使用固定 15 职位冷数据集和 10 个 Fake 会话：`python scripts/benchmark_pipeline.py --output <json> --baseline docs/performance/v2.2.0-small-baseline.json`。输出同时包含原始迭代、p50/p95、绝对变化和相对变化；不会调用真实 Web Search 或云 Provider。强身份迁移的本地基准见 [`docs/performance/strong-job-identity-baseline.md`](docs/performance/strong-job-identity-baseline.md)。

ATS Phase 1 只提供开发基线，不会进入正常求职管道：`python scripts/benchmark_ats.py --output <json> --page-size 50 --max-pages 10`。它只调用样本配置中的官方公开 GET endpoint，职位正文/标题/URL 不落盘；设计结论与接入门槛见 [`docs/ats-provider-phase1.md`](docs/ats-provider-phase1.md)。

## 🔧 依赖

- Python 3.10+
- 必需：`pdfplumber` `python-docx` `requests`
- 可选：`playwright`（headless 兜底，复用系统已装的 Chromium 系浏览器，无需 `playwright install`）
- 可选远程浏览器：`kernel`、`keyring`

```bash
pip install pdfplumber python-docx requests
pip install playwright   # 可选
pip install "kernel>=0.94,<1" keyring  # 可选远程浏览器
```

---

*Built with [Claude Code](https://claude.com/claude-code).*
