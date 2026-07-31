# 运行时监控

Job Matcher 是本地 CLI skill，不需要常驻 Prometheus 服务。运行监控采用两个低依赖组件：

- `data/metrics.jsonl`：每次 `merge` / `update` 的结构化事件。
- `scripts/summarize_metrics.py`：按时间窗口汇总健康状态、比率、分位数和队列积压。

## 使用

```text
# 最近 7 天 Markdown 健康报告
python scripts/summarize_metrics.py --days 7 --format markdown

# 机器可读 JSON
python scripts/summarize_metrics.py --days 30 --format json

# 存在阈值违规时返回退出码 2，可用于定时任务或 CI
python scripts/summarize_metrics.py --fail-on-breach
```

没有运行数据时脚本仍会正常输出报告，状态为 `no_data`；无样本的比率和分位数显示为 `null` / `n/a`，不会被误判为 0 或 `healthy`。

## 事件字段

所有事件包含：

| 字段 | 说明 |
|---|---|
| `schema_version` | 指标事件 schema 版本，当前为 1 |
| `timestamp` | UTC ISO-8601 时间 |
| `operation` | `merge` 或 `update` |
| `ok` | 操作是否成功 |
| `duration_ms` | 命令端到端耗时 |
| `lock_wait_ms` | 等待职位主表锁的时间 |
| `stale_lock_recoveries` | 本次回收异常遗留主表锁次数 |

`merge` 事件还记录候选输入、批内去重、本轮新增、缓存命中、待评估、评估中、归档、主表大小和新建评估任务数。

`update` 事件还记录输入结果、成功更新、安全 rebase、幂等重试、拒绝、冲突、run 释放和任务状态数量。

失败事件只记录低基数 `failure_kind`：`input_validation`、`input_json`、`data_store_read`、`data_store_write`、`lock_timeout`、`data_store` 或 `unexpected`。不记录原始异常消息，避免路径或输入内容进入指标日志。

## 汇总指标

| 指标 | 定义 |
|---|---|
| `cache_hit_rate` | 缓存命中数 / 输入候选数 |
| `evaluation_success_rate` | (`updated` + `idempotent`) / `results_in` |
| `rejected_rate` | `rejected` / `results_in` |
| `conflict_rate` | `conflicts` / `results_in` |
| `duration_ms p50/p95/p99` | 成功和失败命令的端到端耗时分位数 |
| `lock_wait_ms p50/p95/p99` | 主表锁等待时间分位数 |
| `active_runs` | 当前仍有 pending task 的评估 run 数 |
| `pending_tasks` | 当前 pending task 总数 |
| `oldest_pending_age_minutes` | 最老活跃 run 的等待分钟数 |

分位数采用观测窗口内样本排序后的最近秩值。这里的延迟用于本机回归和异常发现，不是生产 SLA。

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

任何一项违规时汇总状态为 `degraded`，有事件且没有违规时为 `healthy`，没有事件时为 `no_data`。没有分母或样本时跳过相应比例/分位数阈值。

## 数据安全

写入函数使用字段白名单。以下数据不会进入 `metrics.jsonl`：

- CV/JD 正文
- `cv_hash`、`cp_hash`、`run_id`、`dedup_key`
- 职位 URL、公司名、职位名
- 原始异常消息

指标追加使用进程内线程锁和短时跨进程文件锁。写入失败时业务操作不会回滚，命令输出会返回 `metrics_recorded:false`，便于编排者告警。

## 当前边界

- 指标文件是本地 append-only JSONL，当前不自动压缩或清理。
- 文件锁只覆盖同一文件系统，不提供跨主机协调。
- 没有主动通知渠道；`--fail-on-breach` 可接入 Windows Task Scheduler、cron 或后续告警系统。
- 若将来改造成常驻服务，再考虑将相同低基数指标导出到 Prometheus/OpenTelemetry。
