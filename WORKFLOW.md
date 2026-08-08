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
| 网页抓取 | 可选 | 你的 fetch / 浏览工具 | **回退脚本**：`fetch_rendered.py` / `verify_jobs.py`（已内置 requests/playwright） |

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
  - 若判定输入不是简历 → 提示用户。

### 3. 构建检索条件（你来做，读 `references/search_playbook.md`）
- 融合 CVProfile + query → `search_plan`(≤5) + `candidate_profile`。
- **缺目标职位 或 地点完全缺失 → 停下追问用户**。
- 算 `candidate_profile_hash`：把 candidate_profile JSON 喂给 `python scripts/cp_hash.py`（它规范化后再 hash，**保证同语义同 hash、不每轮分裂**），取返回的 `cp_hash`。后续 `merge_jobs` / `render_html` 的 `--cp-hash` **全部用它**（不要自己另编 hash）。

### 4. 检索职位（web 搜索 + 脚本，自适应分批）
- 按 search_playbook 自适应分批：每批执行若干条 query 的 **web 搜索**（有子代理则并行委派、各 1 次搜索；否则你逐条搜），按 search_playbook「搜索职责」解析+三维初筛，得结构化职位数组。
- 汇总 → `merge_jobs.py merge` → `{to_analyze, to_score_only, in_evaluation, cached, eval_run, stats}`。
- `merge` 同时创建 `data/eval_runs/<run_id>.json` 评估任务快照，并在 `eval_run` 返回路径。`in_evaluation` 中的职位已有未完成任务，不要重复委派。
- 按 stats 判断是否追加下一批（阈值/上限/连续空批见 playbook）。
- **重叠执行**：决定追加第 N+1 批时，不必等第 N 批评完——把「第 N 批评估 worker（第 5 步）」
  和「第 N+1 批搜索 worker」放进同一条消息并行发出，评估结果回来就增量 `update`。
- 一行进度：`第N批 搜X条→候选Y→新Z/缓存W`。

### 5. 匹配排序（打分 + 脚本，读 `references/scoring_rubric.md`）
- **粗排**：对 `to_analyze`+`to_score_only` 用 snippet 做 5 维快速估分排序（有子代理则分片并行）。
- **精排（worker 一条龙）**：取 Top-(top_n+precise_buffer)，每个精排 worker 在**一个子代理内**
  完成「抓 JD 全文（用**抓取**能力或回退脚本）→ 抽 jd_profile → 精确 5 维打分 → 回传结构化结果」，
  JD 全文留在 worker 内不回传；`to_score_only` 复用已有 jd_profile 只打分。
- **失效验证**（精排 Top-N）：`verify_jobs.py` 查死链；`possibly_closed` 的走容错阶梯确认；失效则剔除、从次位递补。
- 每个 worker 必须原样回传任务中的 `dedup_key`、`base_record_version`、`jd_input_hash`，再附加 `jd_profile`、`match_score`、`verified`、`scored_from`。不得回传或覆盖 title/company/url/source 等搜索字段。
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
  → fetch_rendered.py <url>（headless，受 headless_budget 约束，缺浏览器自动跳过）
  → 全失败：标注「未验证」/「基于摘要评分」，不阻塞
```

## 护栏
- 抓取**不绕验证码、不模拟登录、不抓需付费/登录内容、尊重 robots/ToS**。
- 失败一律**降级不阻塞**；搜 0 结果/全失效时如实告知并建议放宽条件。
- 大块文本留子代理/文件，上下文只放路径与小 JSON。
- 不臆造职位或字段；CV 含 PII，数据落 `data/`（已 .gitignore）。
- 并行只用于搜索、抓取和评估计算；所有 `merge/update` 由编排者串行提交。脚本仍使用跨进程锁和原子替换防止误并发及中断损坏。
