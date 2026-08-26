# Job Matcher — Workflow（agent-中立）

> 本文件是 job-matcher 的**单一事实源流程**，不绑定任何特定 agent。
> 任何具备「运行 Python + 读写文件 + web 搜索」能力的 agent 都能照此执行。
> 各 agent 的入口文件（如 Claude Code 的 `SKILL.md`）只负责把下面的「能力」映射到该 agent 的具体工具，流程本身在这里。

## 能力前提

| 能力 | 必需性 | 映射到你的运行时 | 缺失时 |
|------|:---:|------|------|
| 运行 Python 3 + 读写文件 | **必需** | shell / exec | 无法运行（脚本是骨架） |
| Web 搜索 | **必需** | 你的 web 搜索工具 | 无法做职位检索（核心残缺） |
| 并行子代理 | 可选 | 你的 sub-agent / 并行机制 | **降级：你自己主线程串行执行各步** |
| 网页抓取 | 可选 | 你的 fetch / 浏览工具 | **回退脚本**：静态/本机 `fetch_rendered.py`，以及可选的远程 `browser_control.py` |

> 下文用「**子代理**」「**web 搜索**」「**抓取**」指代上述能力。有就用，没有就按"缺失时"列降级——流程不变，只是慢一些、上下文不那么整洁。

## 编排原则

- 你是**编排者**：调脚本、融合 query、追问用户、（若有）委派子代理。
- **重上下文工作**（CV 抽取、搜索+解析、打分）尽量交给子代理；大块原始文本（CV 全文、搜索结果、JD 全文）**留在子代理/文件**，你的上下文只保留「路径 + 小 JSON」。
- 无子代理时你自己串行做这些步骤，但**仍坚持**"大文本写文件、上下文只留摘要"。
- 搜索与评估 worker 可以并行；`jobs_table.json` 是唯一主表，只有编排者可以通过 `merge_jobs.py` 写入。worker 不得直接修改主表或共享评估快照。
- **批间重叠（有子代理时的推荐模式）**：第 N 批 `merge` 返回 `eval_run` 后，在**同一条消息里**
  同时发出「第 N 批的评估 worker」和「第 N+1 批的搜索 worker」——评估不等下一批搜索，
  搜索也不等上一批评估。快照机制兜底：重叠期间同一职位不会被重复派发（`in_evaluation`），
  JD 输入被搜索改动时 `update` 报 conflict 拒收旧结果，编排者中途死亡留下的超龄快照
  由下一次 `merge` 自动作废回收（`eval_run_stale_hours`，默认 2 小时）。
- `max_parallel_subagents` 是**全局**并发预算（搜索+评估 worker 共用）；
  重叠期建议 1 个搜索 worker、其余给评估（默认 3 → 1 搜 + 2 评）。
- 脚本输出是纯 ASCII JSON，解析后使用。所有路径相对本 skill 目录。
- 每次子代理调用前先运行 `subagent_metrics.py profile --role <role>`，运行时支持时按返回的
  `model`、`reasoning_effort`、`fork_turns` 创建隔离 worker；不支持覆盖时允许继承当前模型，
  但必须在调用后把实际模型/effort 和 `fallback_used` 如实记录，不能把请求值冒充实际值。

## 脚本契约（你的确定性工具箱）

| 脚本 | 调用 | 输入 | 输出 |
|------|------|------|------|
| `extract_cv.py` | `python scripts/extract_cv.py <file>` | CV 文件路径 | `{ok, source_type, char_count, cv_hash, text_path, cache_hit, cached_profile_path?, warnings}` |
| `validate_profile.py` | `python scripts/validate_profile.py`（stdin） | LLM 抽取的 CVProfile JSON | `{ok, profile, notes}` |
| `merge_jobs.py merge` | `… merge --cv-hash H --cp-hash H`（stdin） | 候选职位数组 | `{to_analyze, to_score_only, in_evaluation, cached, eval_run, stats, metrics_recorded}` |
| `merge_jobs.py update` | `… update --cv-hash H --cp-hash H --run-id R`（stdin） | 带快照元数据的打分结果数组 | `{ok, updated, rebased, rejected, conflicts, released, duration_ms, metrics_recorded}` |
| `summarize_metrics.py` | `… [--days N] [--format json\|markdown] [--fail-on-breach]` | `data/metrics.jsonl` + 活跃 eval runs | 健康状态、比率、p50/p95/p99、积压与阈值违规 |
| `verify_jobs.py` | `python scripts/verify_jobs.py`（stdin） | URL 数组 | `{results:[{url, alive, reason, final_url}]}` |
| `fetch_rendered.py` | `python scripts/fetch_rendered.py <url>` | 单 URL | `{ok, text, browser_used}` 或 `{ok:false, error}` |
| `cp_hash.py` | `python scripts/cp_hash.py`（stdin） | candidate_profile JSON | `{ok, cp_hash}`（规范化后稳定 hash） |
| `render_html.py` | `… --cv-hash H --cp-hash H [--meta-file F]` | jobs_table + meta + PII-safe metrics | `{ok, report_path, job_count, health_status, health_breaches}` |
| `round_timer.py` | `… start` / `… finish --round-id R --orchestration serial\|overlapped` | 整轮起止 | `{ok, round_id}` / `{ok, round_duration_ms, metrics_recorded}` |
| `subagent_metrics.py` | `… profile --role R` / `… record …` | 角色配置 / 实际执行计数 | 请求配置；或写入一次 PII-safe 子代理指标 |
| `browser_setup.py` | `python scripts/browser_setup.py` | localhost 表单 | 测试连接，密钥进系统密钥库，非敏感设置进 `data/` |
| `browser_control.py` | `… create/screenshot/click/type/press/scroll/close/test` | session id + 视觉动作 | 小 JSON；`create` 临时返回 Live View URL |
| `browser_workflow.py` | 由 browser worker 使用 | 逐页观察与下一页动作 | 有上限的串行翻页、链接去重与暂停状态 |

指令文档（按需读）：`references/cv_schema.md`、`references/scoring_rubric.md`、`references/search_playbook.md`。配置：`config.json`。

## 流程

### 0. 准备
- 读 `config.json` 拿参数。
- `python scripts/round_timer.py start` → 记下 `round_id`（整轮计时，第 7 步收尾时结束）。失败不阻塞流程。
- **灵活识别输入**：从用户消息找出 CV（文件路径，或粘贴的大段简历文本）和 query（求职意向）。
  - 只有 query 没 CV → 追问 CV。
  - 有 CV 没 query → 可继续，但目标职位/地点缺失时按第 3 步规则追问。

### 1. 解析 CV（脚本）
- 文件：`python scripts/extract_cv.py <path>`。
- 粘贴文本：先存成 `data/cv_text.txt`（UTF-8），再 `python scripts/extract_cv.py data/cv_text.txt`。
- `ok:false` → 告诉用户换格式；有 `warnings` → 先告知质量风险。记下 `cv_hash` / `text_path` / `cache_hit`。

### 2. CV 结构化
- `cache_hit:true` → 读 `cached_profile_path` 载入 CVProfile，**跳过抽取**。
- 否则（**有子代理就委派，否则你自己做**）：读 `references/cv_schema.md` + `text_path`，产出 CVProfile JSON → 用 `validate_profile.py` 校验补全 → 写 `data/cv/<cv_hash>.json`。
  - 委派时只回传简短摘要（roles/seniority/missing），不回贴全文。
  - 委派角色为 `cv_extract`；调用前读取 profile，调用后记录耗时、输入/输出/有效条数及实际模型。
  - 若判定输入不是简历 → 提示用户。

### 3. 构建检索条件（你来做，读 `references/search_playbook.md`）
- 融合 CVProfile + query → `search_plan`(≤5) + `candidate_profile`。
- **缺目标职位 或 地点完全缺失 → 停下追问用户**。
- 算 `candidate_profile_hash`：把 candidate_profile JSON 喂给 `python scripts/cp_hash.py`（它规范化后再 hash，**保证同语义同 hash、不每轮分裂**），取返回的 `cp_hash`。后续 `merge_jobs` / `render_html` 的 `--cp-hash` **全部用它**（不要自己另编 hash）。

### 4. 检索职位（web 搜索 + 脚本，自适应分批）
- 按 search_playbook 自适应分批：每批执行若干条 query 的 **web 搜索**（有子代理则用 `search` profile 并行委派、各 1 次搜索；否则你逐条搜），按 search_playbook「搜索职责」解析+三维初筛，得结构化职位数组。
- Web 搜索“结果翻页”视为下一次独立搜索调用；仅在上一页仍有高相关未覆盖结果时继续，且每一页都计入 `max_websearch_calls`。不要假定一次搜索调用会自动替你翻完全部结果页。
- Web Search 发现公司招聘列表但职位链接不完整时，可把该列表交给 browser worker 做网站内翻页；同一网站第 1→N 页必须串行，不同网站可在 `browser_max_concurrency` 内并行。
- 每批结构化 Web 候选先送入 `python scripts/ats_pipeline.py discover`，只识别 allowlist 中的官方 Ashby/Greenhouse/Lever board。已登记的 verified board 只抑制重复的招聘列表抓取，不跳过该公司的普通 Web 职位、新闻或未知来源。
- `ats_enabled` 为 true 时，每轮最多调用一次 `python scripts/ats_pipeline.py sync --profile <cv-profile.json>`；也可对首批候选使用 `run --profile ...` 合并发现与同步。脚本返回 merge-ready 候选数组，必须由主 agent 与 Web 候选一起串行交给同一个 `merge_jobs.py merge`。不要把 ATS 标识库当作第二张职位表。
- 已到期的 known verified board 可在首批开始时同步；跨 board ATS 同步可与下一批 Web Search/既有 JD 评估并发。同一 Lever board 的 `skip/limit` 翻页必须串行。ATS 失败只降级该 board，不能阻塞或丢弃 Web 结果。
- ATS 的 board/request/page/concurrency 预算独立于 `max_websearch_calls` 和浏览器预算；不得因为 Web 预算尚有余额而突破 ATS 硬上限。
- 汇总 → `merge_jobs.py merge` → `{to_analyze, to_score_only, in_evaluation, cached, eval_run, stats}`。
- `merge` 同时创建 `data/eval_runs/<run_id>.json` 评估任务快照，并在 `eval_run` 返回路径。`in_evaluation` 中的职位已有未完成任务，不要重复委派。
- 按 stats 判断是否追加下一批（阈值/上限/连续空批见 playbook）。
- **重叠执行**：决定追加第 N+1 批时，不必等第 N 批评完——把「第 N 批评估 worker（第 5 步）」
  和「第 N+1 批搜索 worker」放进同一条消息并行发出，评估结果回来就增量 `update`。
- 一行进度：`第N批 搜X条→候选Y→新Z/缓存W`。
- 每个搜索 worker 返回后调用 `subagent_metrics.py record`，至少记录请求/实际模型、effort、耗时、候选输出数、通过初筛数、拒绝数和是否回退；不得记录 query 或 URL。

### 5. 匹配排序（打分 + 脚本，读 `references/scoring_rubric.md`）
- **粗排**：对 `to_analyze`+`to_score_only` 用 snippet 做 5 维快速估分排序（有子代理则分片并行）。
- **精排（worker 一条龙）**：取 Top-(top_n+precise_buffer)，每个精排 worker 在**一个子代理内**
  完成「抓 JD 全文（用**抓取**能力或回退脚本）→ 抽 jd_profile → 精确 5 维打分 → 回传结构化结果」，
  JD 全文留在 worker 内不回传；`to_score_only` 复用已有 jd_profile 只打分。
- 精排使用 `evaluation` profile；需视觉远程浏览时使用 `browser` profile。两种 worker 都要记录实际模型/effort、耗时、成功、有效输出和回退情况。
- **失效验证**（精排 Top-N）：`verify_jobs.py` 查死链；`possibly_closed` 的走容错阶梯确认；失效则剔除、从次位递补。
- 每个 worker 必须原样回传任务中的 `record_id`、`dedup_key`、`base_record_version`、`jd_input_hash`，再附加 `jd_profile`、`match_score`、`verified`、`scored_from`。`record_id` 是主键；`dedup_key` 仅是兼容弱键。不得回传或覆盖 title/company/url/source 等搜索字段。
- 写回：`merge_jobs.py update --run-id <eval_run.run_id>`。脚本会校验评分契约，只合并评估字段；搜索期间仅来源等非评估输入变化时安全 rebase，JD 输入变化时报告 conflict 并拒绝旧结果。
- 同一 run 可增量提交多个 worker 结果；全部任务完成或被判定为冲突后 `released:true`，任务快照自动释放并在 `data/eval_runs/history.jsonl` 留一条不含 CV/JD 正文的运行摘要。冲突职位由后续 `merge` 重新建立新快照。

### 6. 生成报告（脚本）
- 写 `data/run_meta.json`：`{profile_summary, new_count, cached_count, lang}`（lang = CVProfile.search_language）。
- `python scripts/render_html.py --cv-hash H --cp-hash H --meta-file data/run_meta.json` → 生成并**自动打开报告**。
- 渲染时自动计算并嵌入最近 7/30 天运行健康静态快照；顶部状态入口可查看关键指标和阈值告警。监控计算失败只显示 `unavailable`，不阻断职位报告。
- ⚠ 每轮**只在这里 render 一次**；返回的 `opened: true` 表示报告**已自动打开**，**不要再手动打开报告**（os.startfile / 浏览器 / 重复 render 都不要），否则会打开多次。
- 把 `report_path` 告诉用户。

### 7. 收尾
- `python scripts/round_timer.py finish --round-id <R> --orchestration overlapped|serial --batches N --evaluations N --jobs-reported N`
  —— `overlapped` 表示本轮真的把「第 N 批评估」和「第 N+1 批搜索」并行发出过，否则填 `serial`。
  如实填写：这是唯一能实测重叠编排收益的数据来源，填错会让对比失去意义。
- 简述结果（新增/复用/路径），指出风险（未验证/基于摘要评分的职位）。
- `metrics_recorded:false` 时提示运行指标未落盘；需要健康检查时运行 `summarize_metrics.py`。指标字段和默认阈值见 `docs/monitoring.md`。

## 容错阶梯（失效验证 & JD 抓取共用）
```
抓取正文（你的 fetch 工具）→ 失败退避重试1次
  → requests 静态抓（可在子代理内，或脚本）扫关闭关键词
  → fetch_rendered.py <url>（仅当 enable_headless_fallback 为 true；受 headless_budget 约束，缺浏览器自动跳过）
  → browser_control.py（仅当 remote_browser_enabled 为 true；Kernel BYOK，受并发/页数/会话/估算费用硬上限约束）
  → 全失败：标注「未验证」/「基于摘要评分」，不阻塞
```

### 远程视觉浏览器协议

1. 未配置时运行 `browser_setup.py`；密钥缺失或连接测试失败即跳过远程层，不阻塞整轮。
2. 使用第 0 步的 `round_id` 创建会话：`browser_control.py create --round-id R --url U`。控制脚本在调用 Provider **之前**原子预留并发、单轮会话数和估算费用预算；默认每次预留 `browser_cost_limit_usd / browser_session_budget`。
3. `screenshot` 保存到 `data/browser_sessions/`，browser worker 读取图片并用 `click/type/press/scroll` 操作。不要引入本机 Playwright 来控制远程会话。
4. 单个招聘列表最多 `browser_max_pages` 页；用 `browser_workflow.py` 的状态契约逐页观察、去重链接、再点击下一页。单站串行，多站并行。
5. 识别到验证码、登录、限流或人工确认时，返回 `user_action_required` 或 `rate_limited`，立即暂停该任务；不得自动解验证码、启用 stealth 或轮换代理。
6. 若 `browser_allow_handoff` 为 true，把本次 `create` 返回的临时 Live View URL 告诉用户。用户处理后在同一 session 继续截图；等待超过 `browser_handoff_timeout_minutes` 就关闭并标记未验证。等待期间其他 worker 继续。
7. 无论成功或失败都调用 `close --round-id R --session-id S`；关闭会释放并发槽，但已创建会话数和估算费用仍计入本轮硬上限。
8. 用 `browser_control.py event --status ...` 记录页数/链接计数、接管等待、限流和估算费用；动作本身自动记录 Provider 与耗时。不得记录 session id、Live View URL、页面 URL、输入文本、Cookie 或截图内容。

### ATS 增强协议

生产路由默认由 `ats_enabled: false` 显式关闭；启用是用户/本地配置选择。`ats_pipeline.py` 只允许官方公开 HTTPS GET，不需要 API key，不调用申请、Harvest、Hire 或 Partner API。它在内存中规范化并按 CV 的 title/location/remote/seniority 做确定性初筛，最多输出 `top_n + precise_buffer` 个候选，再进入统一强身份 merge。`data/ats_companies.json` 保存 board 控制标识，`data/ats_sync_state.json` 和 `ats` 指标只保存低基数状态/计数，不保存职位名、URL、JD、CV、token 或异常全文。连续三次 404/410 才标记 unavailable；429、超时和网络失败保留可重试状态。`benchmark_ats.py` 复用同一生产解析器做公开小样本回归，但其脱敏报告不进入职位主表。

## 护栏
- 抓取**不绕验证码、不模拟登录、不抓需付费/登录内容、尊重 robots/ToS**。
- 失败一律**降级不阻塞**；搜 0 结果/全失效时如实告知并建议放宽条件。
- 大块文本留子代理/文件，上下文只放路径与小 JSON。
- 不臆造职位或字段；CV 含 PII，数据落 `data/`（已 .gitignore）。
- 并行只用于搜索、抓取和评估计算；所有 `merge/update` 由编排者串行提交。脚本仍使用跨进程锁和原子替换防止误并发及中断损坏。
