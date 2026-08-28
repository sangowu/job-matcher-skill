# 运行时监控

Job Matcher 是本地 CLI skill，不需要常驻 Prometheus 服务。运行监控采用两个低依赖组件：

- `data/metrics.jsonl`：每次 `run_start` / `search` / `merge` / `update` / `subagent` / `browser` / `ats` / `round` / `run_finish` 的结构化事件。
- `scripts/summarize_metrics.py`：按时间窗口汇总健康状态、比率、分位数和队列积压。
- `scripts/render_html.py`：每次生成职位报告时自动计算 7/30 天快照并嵌入 HTML。

## 使用

```text
# 最近 7 天 Markdown 健康报告
python scripts/summarize_metrics.py --days 7 --format markdown

# 机器可读 JSON
python scripts/summarize_metrics.py --days 30 --format json

# 存在阈值违规时返回退出码 2，可用于定时任务或 CI
python scripts/summarize_metrics.py --fail-on-breach

# 子代理完成后记录请求值与实际值；运行时未接受 override 时用 inherited + fallback
python scripts/subagent_metrics.py record --run-id <R> --role search --ok \
  --model-effective gpt-5.6-luna --effort-effective low \
  --duration-ms 1200 --items-in 1 --items-out 8 --valid-items 6 --rejected-items 2

# 每个 Web Search 结果页完成后，只记录页序号与计数
python scripts/search_metrics.py --ok --run-id <R> --query-slot q1 \
  --page-number 1 --raw-results 10 --prefiltered 6 --new-candidates 4 --duration-ms 800
```

没有运行数据时脚本仍会正常输出报告，状态为 `no_data`；无样本的比率和分位数显示为 `null` / `n/a`，不会被误判为 0 或 `healthy`。

## HTML 报告展示

每次运行 `render_html.py` 时会读取同一份 PII-safe 指标和活跃评估清单，自动计算最近 7 天与 30 天的健康快照。职位报告顶部显示状态入口；点击后可在全屏监控层中切换窗口，查看关键指标和阈值告警。

这是生成时快照，不是常驻实时页面：打开报告后不会轮询本地文件，下一次生成报告时才会更新。摘要计算失败不会阻断职位报告，监控区只显示 `unavailable`，不会嵌入原始异常文本或本地路径。`render_html.py` 的 JSON 输出额外包含当前 7 天窗口的 `health_status` 与 `health_breaches`。

## 事件字段

所有事件包含：

| 字段 | 说明 |
|---|---|
| `schema_version` | 指标事件 schema 版本，当前为 5；汇总仍兼容已有 v1–v4 事件 |
| `timestamp` | UTC ISO-8601 时间 |
| `operation` | `run_start`、`search`、`merge`、`update`、`subagent`、`browser`、`ats`、`round` 或 `run_finish` |
| `ok` | 操作是否成功 |
| `run_id` | `round_timer.py start` 生成的随机流水线标识；与评估快照 ID 无关 |
| `duration_ms` | 命令端到端耗时 |
| `lock_wait_ms` | 等待职位主表锁的时间 |
| `stale_lock_recoveries` | 本次回收异常遗留主表锁次数 |

`merge` 事件还记录候选输入、批内去重、本轮新增、缓存命中、待评估、评估中、归档、主表大小、新建评估任务数，以及旧记录身份迁移数、强身份记录数、阻止的强身份冲突/歧义弱匹配数。`jd_handoffs` 与 `jd_handoff_chars` 只记录交接任务数和字符总数，不含正文或 hash。

`update` 事件还记录输入结果、成功更新、安全 rebase、幂等重试、拒绝、冲突、run 释放、任务状态数量和旧记录身份迁移数。

`run_start` 记录 skill 版本、可用时的 Git 短 revision、工作树是否有已跟踪改动，以及不含密钥的 `config.json` 指纹。`run_finish` 只记录期望/观测/缺失的低基数 operation 名称和计数。默认期望 `run_start/search/merge/round`；`evaluations > 0` 时自动期望 `update`，使用子代理、ATS 或浏览器时由编排者重复传入 `--expect` 声明。

`search` 事件记录 query 槽位（如 `q1`）、搜索结果页、调用数、原始/初筛/去重/新增/缓存候选数、总耗时及可用时的首结果耗时，不接收 query、职位名、公司或 URL。

`subagent` 事件记录角色、请求/实际模型、请求/实际 reasoning effort、是否发生继承回退、耗时及输入/输出/有效/拒绝条数。若运行时提供 usage，还记录 input/output/cached/reasoning token 与实际或估算成本；不提供时字段为 `null`，汇总先报告覆盖率再给总量。

`browser` 事件记录 Provider、动作、耗时、页码/链接计数、接管、限流和估算费用。session id、Live View URL、页面 URL、键盘输入和截图不允许进入事件。

`ats` 事件按 board 同步记录 Provider、动作、成功/分类状态、请求/页数/字节数、收到/规范化/初筛/输出职位数、含 JD 的规范化/输出职位数、JD 截断数、响应截断、限流和 HTTP 状态。board id、company/token、职位名、URL、JD 和异常全文不允许进入指标事件。

失败事件只记录低基数 `failure_kind`：`input_validation`、`input_json`、`data_store_read`、`data_store_write`、`lock_timeout`、`data_store` 或 `unexpected`。不记录原始异常消息，避免路径或输入内容进入指标日志。

## 汇总指标

| 指标 | 定义 |
|---|---|
| `cache_hit_rate` | 缓存命中数 / 输入候选数 |
| `runs.complete/incomplete/stale_unfinished` | 完成闭环、缺必需事件、超时未收尾的 run 数 |
| `search.effective_candidates_per_call` | Web Search 新候选数 / 调用数 |
| `search.duration_ms` / `first_result_ms` | Web Search 总耗时与首结果耗时 p50/p95；后者另报覆盖率 |
| `evaluation_success_rate` | (`updated` + `idempotent`) / `results_in` |
| `rejected_rate` | `rejected` / `results_in` |
| `conflict_rate` | `conflicts` / `results_in` |
| `duration_ms p50/p95/p99` | 成功和失败命令的端到端耗时分位数 |
| `lock_wait_ms p50/p95/p99` | 主表锁等待时间分位数 |
| `active_runs` | 当前仍有 pending task 的评估 run 数 |
| `pending_tasks` | 当前 pending task 总数 |
| `oldest_pending_age_minutes` | 最老活跃 run 的等待分钟数 |
| `rounds.<mode>.p50_ms / p95_ms` | 按编排模式分组的整轮时长分位数 |
| `rounds.overlap_saving_pct` | (serial p50 − overlapped p50) / serial p50，缺任一模式时为 `null` |
| `subagents.success_rate` | 成功子代理调用 / 全部子代理调用 |
| `subagents.valid_item_rate` | 有效输出条数 / 总输出条数 |
| `subagents.fallback_rate` | 未按请求模型/effort 执行的调用 / 全部子代理调用 |
| `subagents.by_profile` | 按 role + 实际模型 + 实际 effort 分组的运行数、成功率、有效率、回退率与 p50/p95 |
| `subagents.usage_reported_rate` / `token_reported_rate.*` | 任一/各 token 字段可用的子代理事件比例；缺失字段总量为 `null` |
| `subagents.cost_reported_rate` / `actual_cost_usd` / `estimated_cost_usd` | 成本覆盖率，以及严格分开的实际/估算成本 |
| `browsers.success_rate` | 成功视觉动作 / 全部视觉动作 |
| `browsers.sessions_created` | 成功创建的远程浏览器会话数 |
| `browsers.handoffs` / `rate_limited` | 人工接管和限流事件计数 |
| `browsers.estimated_cost_usd` | 本窗口记录的估算费用合计；不是供应商账单 |
| `ats.board_runs` / `success_rate` | ATS board 同步运行数与成功比例 |
| `ats.requests` / `pages` | ATS 公开 GET 请求和已处理页数合计 |
| `ats.response_bytes` / `content_fallback` | 已读取公开响应字节数与 Greenhouse 超大正文响应降级次数 |
| `ats.jobs_received` / `jobs_emitted` | API 接收职位数与确定性初筛后输出数 |
| `ats.jobs_with_jd` / `jobs_with_jd_emitted` | 规范化职位与最终候选中可直接交接 JD 的数量 |
| `ats.jd_text_truncated` | JD 纯文本超过 50,000 字符而被截断的职位数 |
| `ats.by_provider` | 按 Provider 汇总运行数、成功率、请求、页数与职位计数 |

分位数采用观测窗口内样本排序后的最近秩值。这里的延迟用于本机回归和异常发现，不是生产 SLA。

## 整轮计时（serial vs overlapped）

脚本级 `duration_ms` 只覆盖单次 merge/update 调用，相比编排者在两次调用之间做的搜索与评估工作
可以忽略不计——因此它无法回答「批间重叠到底有没有用」。`scripts/round_timer.py` 给整轮（首次搜索
到出报告）计时，写入一条 `round` 事件：

```text
python scripts/round_timer.py start          # -> {"run_id": "round-...", "round_id": "round-..."}
python scripts/round_timer.py finish --round-id <R> --orchestration overlapped \
    --batches 3 --evaluations 14 --jobs-reported 12
```

`--orchestration` 只接受 `serial` / `overlapped`，须如实填写——这是唯一能实测重叠收益的数据来源。
`round` 事件字段：`run_id`、`round_duration_ms`、`orchestration`、`batches`、`evaluations`、`jobs_reported`，
同样只有时长与计数，不含任何 CV / JD / 职位 / 查询文本。

整轮时长**不计入** `duration_ms` 分位数，避免污染脚本级数字。汇总输出按模式给出
`rounds.serial.p50_ms` / `rounds.overlapped.p50_ms` 与 `overlap_saving_pct`；两种模式都有样本前
`overlap_saving_pct` 显示 `n/a`，不会拿单边数据编出一个收益。

## 默认健康阈值

阈值位于 `config.json` 的 `monitoring_thresholds`：

| 指标 | 默认阈值 |
|---|---:|
| `conflict_rate` | ≤ 2% |
| `rejected_rate` | ≤ 1% |
| `evaluation_success_rate` | ≥ 98% |
| `lock_wait_ms p95` | ≤ 100 ms |
| `oldest_pending_age_minutes` | ≤ 30 分钟 |
| failed events | 0 |
| data-store write failures | 0 |
| malformed metric events | 0 |
| malformed active manifests | 0 |
| unfinished run age | ≤ 120 分钟 |

任何一项数值阈值违规时汇总状态为 `degraded`。已完成 run 缺少必需事件，或未完成 run 超过 `unfinished_run_age_minutes_max` 时，`metrics_status` 为 `incomplete`、健康状态为 `unknown`，绝不显示 `healthy`。没有事件时为 `no_data`。进行中的未超时 run 标为 `collecting`，不会立即污染历史健康判定。

## 数据安全

写入函数使用字段白名单。以下数据不会进入 `metrics.jsonl`：

- CV/JD 正文
- `cv_hash`、`cp_hash`、评估快照 ID、`dedup_key`（只允许随机 pipeline `run_id`）
- 职位 URL、公司名、职位名
- 原始异常消息
- API Key、Cookie、session id、Live View URL、页面 URL、查询/键盘输入和截图内容

指标追加使用进程内线程锁和短时跨进程文件锁。写入失败时业务操作不会回滚，命令输出会返回 `metrics_recorded:false`，便于编排者告警。

## 当前边界

- 指标文件是本地 append-only JSONL，当前不自动压缩或清理。
- 文件锁只覆盖同一文件系统，不提供跨主机协调。
- 没有主动通知渠道；`--fail-on-breach` 可接入 Windows Task Scheduler、cron 或后续告警系统。
- HTML 中展示的是报告生成时的 7/30 天静态快照，不会自动刷新或提供历史趋势图。
- 若将来改造成常驻服务，再考虑将相同低基数指标导出到 Prometheus/OpenTelemetry。
