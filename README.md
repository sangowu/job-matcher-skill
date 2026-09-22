# job-matcher

[English](README.en.md) | **中文**

版本文档：[更新记录](CHANGELOG.md) · [v2.4.0 发布说明](docs/releases/v2.4.0.md) · [v2.3.0 发布说明](docs/releases/v2.3.0.md) · [v2.2.0 发布说明](docs/releases/v2.2.0.md) · [v2.1.0 发布说明](docs/releases/v2.1.0.md) · [v2.0.0 发布说明](docs/releases/v2.0.0.md)

> 一个 **agent skill（Claude Code 与 Codex 通用）**：输入**简历(CV) + 求职意向**，自动抽取简历字段、组合使用**模型/Web 搜索与 BrowserOS Neo 或用户浏览器**检索匹配职位，生成一份**可交互的 HTML 报告**。

是 [JobRadar](https://github.com/sangowu/JobRadar) 的**轻量版**——默认同时使用模型搜索和可用的本机浏览器；浏览器提供者优先选择 Agent 已连接的 BrowserOS Neo，再降级到已授权用户浏览器，也可选接入 BYOK 隔离浏览器作为抓取兜底。

---

## ✨ 功能

- 📄 **简历解析**：支持 PDF / DOCX / TXT / MD，或直接粘贴文本（不做 OCR）。
- 🧠 **结构化抽取**：抽取目标职位、技能、资历(seniority)、地点、语言等，自动按相关年限定级。
- 🔎 **实时职位检索**：默认并行运行浏览器平台搜索与模型/Web 搜索；浏览器按 Neo → 用户浏览器选择提供者，平台由地区来源目录动态规划，不在代码中硬编码站点 selector。
- 🎯 **5 维匹配打分**：title / seniority / skills / location / must-have，输出五档投递建议（强烈投递→跳过）。资历硬规则与契约校验拦截打分漂移。
- 🗂️ **增量缓存**：CV、JD、匹配分三层缓存；多来源同职位自动聚合（含区域平台 job-id 强命中）；换 query 自动失效重算。
- 🛡️ **不可信输入隔离**：搜索结果与 JD 正文按纯数据处理并忽略其中指令；报告内嵌 JSON 转义、链接限 http(s)。
- 📊 **可交互报告**：两栏布局（左职位列表 30% + 右详情 70%）+ 评分徽章 + 深色模式 + 排序/搜索 + 市场/来源/验证状态筛选 + 多来源发现证据 + 7/30 天运行健康快照 + 中英 i18n，自包含单文件 HTML。
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
   │ [发现计划] 浏览器平台+模型/Web+可选ATS → [脚本] merge_jobs(去重/聚合/缓存+评估快照)
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
│   ├── markets.json          # ie/uk/cn/de 地点、语言与 query 模板
│   ├── role_taxonomy.json    # 中英德稳定角色族与同义词
│   ├── source_seeds.json     # 四市场已验证公开来源种子（不含凭据）
│   ├── multi_region_smoke_plan.json # Phase D2 固定来源与请求硬上限
│   ├── shadow_run.schema.json # Phase E 计数型 shadow 证据契约
│   ├── shadow_compare.schema.json # Phase E 临时候选对照输入契约
│   ├── candidate_envelope.schema.json # Phase C 发现候选契约
│   ├── ats_phase1_boards.json # ATS 小基线公开公司样本
│   └── ats_phase5_quality_boards.json # 三供应商质量小样本
├── scripts/              # 确定性 Python 脚本
│   ├── extract_cv.py         # 解析 CV → 文本 + hash
│   ├── validate_profile.py   # 校验 + seniority→levels 映射
│   ├── market_plan.py        # 校验市场资源并生成确定性多地区计划
│   ├── discovery_mode.py     # 只读能力状态的发现模式选择与安全降级
│   ├── discovery_plan.py     # 连接市场/健康/来源目录并生成多通道执行任务
│   ├── discovery_batch.py    # 校验所有任务结果，一次 merge 并决定是否扩展下一批
│   ├── local_browser_probe.py # 校验 Agent 已观察到的本机浏览器工具面
│   ├── local_browser_panel.py # localhost 设置、状态与闪烁人工提醒面板
│   ├── cookie_consent.py    # accessibility Cookie 语义分类；仅必要或暂停
│   ├── browser_candidate_smoke.py # 临时 store 验证浏览器候选与 merge，不污染正式数据
│   ├── source_registry.py    # 来源种子校验、健康状态、迁移与确定性来源计划
│   ├── candidate_contract.py # 严格校验 Phase C CandidateEnvelope
│   ├── candidate_handoff.py  # 双发现管道串行交给 merge/registry 单写入器
│   ├── multi_region_smoke.py # 显式、计数型四市场公开来源 smoke
│   ├── shadow_gate.py        # Phase E 幂等 shadow 台账与逐市场发布门
│   ├── shadow_compare.py     # 只读计算增量、交集、JD 与潜在 Top-N
│   ├── analysis_contract.py  # 校验 JDProfile/MatchScore worker 输出
│   ├── merge_jobs.py         # 单写入器：去重/缓存/评估快照/条件化回写
│   ├── runtime_metrics.py    # PII-safe JSONL 指标与健康计算
│   ├── search_metrics.py     # Web Search 页级计数与耗时指标
│   ├── summarize_metrics.py  # 7/30 天 Markdown/JSON 健康报告
│   ├── round_timer.py        # 整轮计时，按编排模式对比墙钟
│   ├── subagent_metrics.py   # 子代理模型/effort 配置与结果指标
│   ├── browser_provider.py   # Kernel/Fake Provider 与安全配置
│   ├── browser_control.py    # 远程视觉浏览器控制命令；本机浏览器动作的指标自报路径
│   ├── browser_setup.py      # 一次性 localhost 配置页面
│   ├── browser_workflow.py   # 列表翻页/暂停状态机
│   ├── ats_provider.py       # Ashby/Greenhouse/Lever 公开 GET 适配器与 Fake
│   ├── ats_pipeline.py       # ATS 标识库、初筛、同步与候选归一化
│   ├── ats_handoff.py        # ATS 正文内存直送统一 merge，避免主上下文暴露
│   ├── board_harvest.py      # 从职位 URL 反推 ATS board，复验后写入来源注册表
│   ├── seed_promotion.py    # 把已复验的采集来源提升进版本控制的种子目录
│   ├── benchmark_pipeline.py # 固定小数据集核心/Fake Provider 基准
│   ├── benchmark_ats.py      # 复用生产适配器的公开 ATS 有界回归
│   ├── benchmark_ats_e2e.py  # 固定 Web 对照组与 Web+ATS 受控 A/B
│   ├── benchmark_ats_quality.py # 三供应商 JD/评分质量审计
│   ├── benchmark_ats_compression.py # ATS HTTP 压缩交错 A/B
│   ├── cp_hash.py            # 稳定的 candidate_profile hash
│   ├── verify_jobs.py        # 失效职位状态码检测
│   ├── fetch_rendered.py     # headless 渲染兜底（复用系统浏览器）
│   ├── render_html.py        # 渲染 HTML 报告
│   └── _jobutil.py           # 共享：归一化/去重键/URL 规范化
├── assets/template.html  # 静态报告模板（Tailwind + 纯 JS）
└── data/                 # 运行时数据（.gitignore，含 PII）
```

多地区 Phase A/B/C 目前仍是显式入口：`python scripts/market_plan.py validate`
校验版本化市场/角色配置，`python scripts/market_plan.py plan` 从 stdin 接收
`{cv_profile,user_intent}` 并生成 Ireland、UK、China、Germany 的市场、语言、地点与
Web query 计划。`python scripts/source_registry.py validate` 校验公开来源种子；`init` 在
`data/source_registry.json` 原子合并种子，并在旧 `data/ats_companies.json` 存在时只读迁移；
`plan --markets ie uk` 只返回 `enabled + verified + TTL 未过期` 的确定性来源，每个全球来源
只出现一次。它们都不执行搜索或修改职位表，因此旧单地区 Web Search/ATS/merge/报告流程
保持兼容。Phase C 编排者可让 `regional_registry` 与 `agent_web_search` 同时开始，待两条 route
都回报成功、失败或跳过后，把同一 `batch_id` 的严格 CandidateEnvelope 交给
`candidate_handoff.py`；它先经唯一 `merge_jobs.py` 写职位与评估快照，再幂等提交来源状态。
契约与回滚见 [`docs/multi-region-phase0-baseline.md`](docs/multi-region-phase0-baseline.md)、
[`docs/source-registry-phase-b.md`](docs/source-registry-phase-b.md) 和
[`docs/multi-region-phase-c.md`](docs/multi-region-phase-c.md)。
Phase D1 的离线报告契约见 [`docs/multi-region-phase-d1.md`](docs/multi-region-phase-d1.md)。
Phase D2 的有界公开来源 smoke、隐私边界与本次证据见
[`docs/multi-region-phase-d2.md`](docs/multi-region-phase-d2.md)；它必须显式传
`--live`，不会进入默认 CI 或生产发现路径。
Phase E 的 count-only shadow 契约、逐市场门槛和当前未达标状态见
[`docs/multi-region-phase-e.md`](docs/multi-region-phase-e.md)。
`multi_region_enabled` 默认仍是 `false`；因此 Phase C 不会自动替换现有单地区流程。

发现模式 Phase 1+2 提供 `discovery_mode.py plan|event` 与
`local_browser_probe.py probe` 的确定性契约。默认 `coverage` 同时运行模型/Web 搜索与一个
**已连接、已授权且可执行**的浏览器提供者，浏览器内部按 BrowserOS Neo → 用户浏览器选择；旧
`auto` 保留单 route 兼容行为，`browser_only` 绝不静默改用模型搜索。`discovery_plan.py` 再把
market plan、URL-free 健康计划和公开来源目录连接成有界 browser/Web/structured 任务。探测基于
Agent 已暴露的工具面，不扫描端口、浏览器配置、Cookie 或现有标签页；实际发现使用专用标签页，
候选仍进入同一 CandidateEnvelope/merge 主表。详见
[`docs/discovery-mode-phase1.md`](docs/discovery-mode-phase1.md) 与
[`docs/local-browser-phase2.md`](docs/local-browser-phase2.md)。浏览器 route 可运行
`python scripts/local_browser_panel.py serve` 打开 loopback 控制面板：它只保存模式和低基数状态，
登录/验证/consent/限流时闪烁并等待用户恢复；详细契约见
[`docs/local-browser-phase3-panel.md`](docs/local-browser-phase3-panel.md)。现有 Kernel 远程浏览器仍只是
单独的可选抓取兜底，不会代替用户本机的登录会话。

DiscoveryPlan 把跨通道任务分成确定性波次；Agent 先只执行 `initial_wave_id`。每个当前波次 task
完成后，`discovery_batch.py` 要求其恰有一个终态，校验候选与 task 的
route/source/market/language 一致性，再把该波次所有通道候选一次性交给现有单写入 merge。
它的脱敏 manifest 支持中断恢复，并按新增唯一候选、计划内剩余波次和现有停止阈值决定是否返回
`next_wave_id`；调用方不能自行覆盖。该决策只控制发现扩展，不等同于 JD 完整或 CV 匹配合格。
2026-09-21 的有界运行时 smoke 已分别验证 BrowserOS Neo MCP 主路径和用户 Chrome 降级路径的
专用标签页 create/navigate/read/close；当 Neo 可用时浏览器提供者会选择 Neo。LinkedIn 实测还验证了
登录暂停、面板提醒/恢复、登录态复用和同标签页受限搜索；后续单候选 smoke 已通过受限主区域读取、
详情存活验证、CandidateEnvelope 和临时 merge；三候选同页批次也完成 3/3 provenance 保留。
2026-09-22 又完成了一次真实 CV、Dublin-first Ireland 的三波生产运行：12 个任务全部到达终态，
两个强身份候选进入正式 merge、JD 评分和 HTML 报告。该运行同时发现，Neo MCP 直接操作尚未写入
仓库 browser 指标，因此报告健康状态会正确显示 `unknown`；站点 host alias、歧义 consent、
自定义 combobox、跨页翻页、CAPTCHA 和限流恢复仍是 gate。证据边界见
[`docs/local-browser-phase4-smoke.md`](docs/local-browser-phase4-smoke.md)。确定性首波的 Neo + Web Search
联合 smoke、consent 暂停、三波继续/停止决策以及临时单写入 merge 的证据见
[`docs/discovery-wave-live-smoke.md`](docs/discovery-wave-live-smoke.md)；完整生产试运行见
[`docs/browseros-neo-production-trial-2026-09-22.md`](docs/browseros-neo-production-trial-2026-09-22.md)。

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
| `version_check_enabled` | true | 启动时是否检查本地 skill 与 GitHub `main` 是否同步 |
| `version_check_interval_hours` | 24 | GitHub 版本检查缓存时长；缓存期内不发网络请求 |
| `version_check_timeout_seconds` | 3 | 单次只读 GitHub 请求超时秒数 |
| `max_parallel_subagents` | 3 | 批内并行上限 |
| `subagent_profiles` | 见配置 | 各角色请求的 model、reasoning effort 与隔离上下文策略 |
| `max_websearch_calls` | 6 | WebSearch 总次数上限 |
| `discovery_mode` | coverage | `coverage` 默认并行浏览器+模型搜索；`auto` 保留单 route 兼容；也支持 `model_only`、`browser_only`、`combined` |
| `cookie_consent_policy` | necessary_only | 只在唯一明确语义按钮上自动拒绝可选 Cookie；也可设 `ask_every_time` |
| `discovery_max_waves` | 3 | 单次计划最多生成的确定性发现波次数 |
| `browser_sources_per_market` | 2 | 每个市场、每个波次的浏览器来源上限；浏览器首个波次内先保证来源类型多样性 |
| `browser_first_wave` | 2 | 浏览器从第几个波次开始；默认让结构化/Web Search 先跑一波，浏览器退为兜底 |
| `browser_queries_per_source` | 2 | 单个浏览器来源本轮执行的地区化查询上限 |
| `web_queries_per_market_per_wave` | 2 | 每市场、每波次执行的 Web Search 查询上限 |
| `web_source_hints_per_task` | 6 | 每条 Web Search 任务携带的公开来源提示上限 |
| `multi_region_enabled` | false | 多地区来源总开关；关闭时所有市场有效模式均为 off |
| `multi_region_rollout` | 四市场均 off | 每市场独立设置 off / shadow / opt_in / default；default 必须通过 Phase E 门禁 |
| `stop_threshold` | 12 | 净有效职位达标停止 |
| `consecutive_empty_stop` | 2 | 连续 N 批 0 结果则停止 |
| `ats_enabled` | false | 是否启用公开 ATS 增强管道；默认显式关闭 |
| `ats_max_concurrency` | 3 | 跨 ATS board 并发硬上限 |
| `ats_boards_per_round` | 30 | 单轮同步 board 数硬上限 |
| `ats_requests_per_round` | 100 | 单轮 ATS HTTP 请求硬上限 |
| `ats_page_size` | 50 | Lever 每页请求数量 |
| `ats_max_pages` | 10 | 单个 Lever board 顺序翻页硬上限 |
| `ats_timeout_seconds` | 30 | 单次公开 ATS GET 超时秒数 |
| `ats_registry_ttl_days` | 30 | 已验证 board 再同步间隔 |
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

`python scripts/version_check.py` 会比较本地 `pyproject.toml` 版本、Git commit 与 GitHub `main`，结果缓存到已忽略的 `data/version_check.json`。它不读取或上传 CV/JD/搜索数据，不需要 GitHub token，也不会自动更新文件；离线、超时或限流只返回 `unknown`，不阻断职位流程。需要绕过缓存做只读诊断时使用 `python scripts/version_check.py --force`。

运行时只保留一个职位主表 `data/jobs_table.json`。`record_id` 是稳定记录/评估主键，`identity_keys` 保存平台职位 ID；公司 + 标题 `dedup_key` 仅作兼容弱匹配。两个不相交的强 ID 不会因同公司同标题而误合并，弱键匹配还要求地点兼容且结果唯一。旧表会在下一次 merge/update 时原位补齐身份字段。每轮待评估职位写入最小化快照 `data/eval_runs/<run_id>.json`；ATS 已返回 JD 时，正文只临时存在对应任务快照，主表只保存内容 hash，worker 可直接分析并跳过网页抓取。单个任务完成即清除正文，整轮完成后快照释放，仅在 `history.jsonl` 留下不含 CV/JD 正文的摘要。每次 merge/update 另写一条 PII-safe `data/metrics.jsonl` 事件。

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

报告包含 run 完整性、Web Search 调用/有效候选、吞吐/缓存、评估成功/拒绝/冲突率、子代理实际模型/effort/token/成本覆盖、浏览器与 ATS 计数、命令与锁等待分位数及积压状态。阈值违规为 `degraded`；缺失必需事件时健康状态为 `unknown`，不会误报 `healthy`。CLI 的 `--fail-on-breach` 对两种情况都返回退出码 2。字段定义见 [运行时监控文档](docs/monitoring.md)，设计与后续 A/B 门槛见 [run-scoped 指标契约](docs/run-metrics-contract.md)。

整轮墙钟另行采集——脚本级耗时相比编排者在两次调用之间的搜索与评估工作可以忽略，无法回答「批间重叠值不值」：

```text
python scripts/round_timer.py start          # → {"run_id": "round-...", "round_id": "round-..."}
python scripts/search_metrics.py --ok --run-id <R> --query-slot q1 --duration-ms 120
python scripts/round_timer.py finish --round-id <R> --orchestration overlapped|serial --expect subagent
```

汇总按编排模式给出 p50/p95 与 `overlap_saving_pct`；两种模式都有样本前显示 `n/a`。

`monitoring_thresholds.unfinished_run_age_minutes_max` 控制未完成 run 何时被判为陈旧并令健康状态变为 `unknown`；默认 120 分钟。

版本性能回归使用固定 15 职位冷数据集和 10 个 Fake 会话：`python scripts/benchmark_pipeline.py --output <json> --baseline docs/performance/v2.2.0-small-baseline.json`。输出同时包含原始迭代、p50/p95、绝对变化和相对变化；不会调用真实 Web Search 或云 Provider，并验证三家 Fake ATS 的 JD 均进入临时任务、主表零正文。强身份迁移基准见 [`docs/performance/strong-job-identity-baseline.md`](docs/performance/strong-job-identity-baseline.md)，三家 ATS 离线管道基准见 [`docs/performance/ats-phase2-fake-baseline.md`](docs/performance/ats-phase2-fake-baseline.md)，Phase 4 交接基准见 [`docs/performance/ats-phase4-jd-handoff.md`](docs/performance/ats-phase4-jd-handoff.md)，真实三条五维抽检见 [`docs/performance/ats-phase4-live-quality.md`](docs/performance/ats-phase4-live-quality.md)。固定 Web 候选对照组与受限真实 ATS 的 discovery-to-merge A/B 使用 `python scripts/benchmark_ats_e2e.py --web-candidates <json> --profile <json> --output <json>`；它会发出公开 ATS 请求，必须显式提供本地输入并遵守生产硬上限。结果与限制见 [`docs/performance/ats-phase3-controlled-e2e.md`](docs/performance/ats-phase3-controlled-e2e.md)。三供应商 JD 质量复核可先用 `python scripts/benchmark_ats_quality.py collect ...` 创建不提交的本地样本，再用 `audit` 生成计数型门禁报告；本次小样本结果与限制见 [`docs/performance/ats-phase5-multiprovider-quality.md`](docs/performance/ats-phase5-multiprovider-quality.md)。ATS HTTP 压缩可用 `python scripts/benchmark_ats_compression.py --output <json> --pairs 3` 做相同结果集的交错 A/B；本次三供应商实测中位传输量减少 79.31%，内容指纹、职位数与请求数均相同，详见 [`docs/performance/ats-http-compression-ab.md`](docs/performance/ats-http-compression-ab.md)。

ATS Phase 2 已提供可选的生产增强管道，默认仍由 `ats_enabled: false` 关闭。Web Search 结果中的官方 Ashby/Greenhouse/Lever URL 可经 `python scripts/ats_pipeline.py discover` 写入本地标识库；Greenhouse 同时识别 `job-boards.eu.greenhouse.io` 的公开职位页，但公开 API 仍使用官方 `boards-api.greenhouse.io`。启用后用 `sync --profile <cv-profile.json>` 同步已到期 board，或用 `run --profile ...` 一次完成发现与同步。管道只做公开 GET，默认请求 gzip 并同时限制压缩响应与解压后正文大小，按标题/地点/资历确定性初筛；单独的 `AI` 产品/团队后缀不是有效岗位匹配，明确的 `AI evaluation`、`AI systems`、`agent systems` 等岗位短语才作为 AI 方向信号。Phase 4 会把 ATS 已提供的 JD 清洗并限制为 50,000 字符，通过同一 `merge_jobs.py` 的本地 run 快照交给精排 worker；有正文的任务跳过网页抓取，没有正文的任务继续走原容错阶梯。Web 与 ATS 仍共用职位主表和分析缓存，主表只留 JD hash。Phase B 初始化通用 `data/source_registry.json` 后，ATS 控制状态只写通用 registry，旧 `data/ats_companies.json` 保持只读；通用 registry 不存在时仍回退旧文件。`data/ats_sync_state.json` 继续保存低敏同步摘要。ATS 预算独立于 Web Search；跨 board 可并发，Lever 单 board内顺序翻页。Greenhouse 的 `content=true` 响应超过 25 MB 时可在同一全局请求预算内降级重试不含正文的列表，并记录 `content_fallback`。公开 API 回归仍使用 `python scripts/benchmark_ats.py --output <json> --page-size 50 --max-pages 10`，且脱敏证据不保存职位正文、标题或 URL。详见 [`docs/ats-provider-phase1.md`](docs/ats-provider-phase1.md)。

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
