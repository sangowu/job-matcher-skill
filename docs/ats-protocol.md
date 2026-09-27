# ATS / 结构化通道协议

> 由 `scripts/ats_pipeline.py`、`scripts/ats_handoff.py`、`scripts/discovery_batch.py` 执行，编排者不手工做这些请求。改这些脚本、排查某个 board 的行为、或要新增一个结构化 provider 时读本文。
> 流程里的入口规则在 [`WORKFLOW.md`](../WORKFLOW.md) 第 4 步。

- 仓库默认 `ats_enabled: true`：公开 ATS board 是成本最低的发现通道（单 board 一次请求约 0.2–0.6 秒取回全量职位与 JD），且受独立硬上限约束。关掉它是用户/本地配置选择。
- **结构化通道不只有 ATS**：`public_read_only_endpoint` 同样走这条路（`amazon-jobs-ie` 用 `provider: amazon_jobs` + `board_token: IRL`，ISO-3 国家码）。**不带 `base_query`**——board 整份拉下来本地初筛，国家码是「读哪一份列表」不是搜索词。
- `ats_pipeline.py` 只允许官方公开 HTTPS GET，不需要 API key，**不调用申请、Harvest、Hire 或 Partner API**。默认请求 gzip；压缩响应的 wire bytes 与解压后 payload **各自**受 25 MB 上限约束，未知或损坏的编码按该 board 安全失败。
- Greenhouse 标识发现接受 `job-boards.greenhouse.io` 与 `job-boards.eu.greenhouse.io`，但两者都调官方 `boards-api.greenhouse.io`；**不要虚构 EU API host**。
- 在内存中规范化并按 CV 的 title/location/seniority 做确定性初筛（remote 一律跳过，见第 3 步）：单独的 `AI` 产品或团队后缀是低信息量 token，不能独立触发岗位匹配；`AI evaluation`、`AI systems`、`agent systems` 这类明确岗位短语仍可匹配。
- 角色初筛之后再按**计划的市场**筛一道：`markets_by_board` 由 `discovery_batch.py` 从每个 structured task 的 `markets` 传入，目录无法归入该市场的地点一律剔除并记入 `jobs_out_of_market`。同名城市按 `markets.json` 的 `foreign_administrative_areas` 判否；两字母代码只在**独立逗号分段**里算数（尾随邮编先剔除）；只有文本没点名任何受支持市场时该判定才生效。**被拉取了却没有计划市场的 board 是错误，不是「无范围」。**
- 候选的 `source_type`/`discovery_route` 按目录里该来源的真实类型写（`ats_board`→`ats_expansion`、`company_careers`→`company_careers`），**不得统一写成 ATS**——`discovery_batch.py` 按 task 的目录类型比对，一条不符整批作废。
- 最多输出 `top_n + precise_buffer` 个候选，再进入统一强身份 merge。
- **`ats_defer_jd`（默认 true）把正文推到这个名额之后**：Greenhouse 列表先按 `content=false` 读一遍，只有本轮真正保留的候选各花一次请求取自己的正文。Ashby、Lever、`amazon_jobs` 只提供一份自带正文的列表，忽略该开关。正文取不到的候选照常入库、`jd_text` 留空，由评估 worker 的容错阶梯自己读页面；请求预算耗尽是停止补正文，不是判该 board 失败。第二趟计入 `jd_requests` / `jd_fetch_failed` / `jd_fetch_skipped`，不并进 `requests` 总数。
- 可用正文清洗为纯文本并截断到 50,000 字符，只经本地评估快照临时交给 worker；主表只留 hash，状态/指标/benchmark 只留计数。Greenhouse `content=true` 响应超 25 MB 时可在同一全局请求预算内额外重试一次不含正文的列表，该 board 继续走网页抓取回退。`response_bytes` 记网络传输字节数，另记正文交接计数与 `content_fallback`。
- `data/source_registry.json` 存在时 `ats_pipeline.py` 只写它，旧 `data/ats_companies.json` 保持只读；通用 registry 不存在时才回退旧文件。`data/ats_sync_state.json` 与 `ats` 指标只存低基数状态/计数，不存职位名、URL、JD、CV、token 或异常全文。**连续三次 404/410 才标 unavailable**；429、超时和网络失败保留可重试状态。
- `benchmark_ats.py` / `benchmark_ats_e2e.py` 复用同一生产解析器做公开小样本回归，脱敏报告不进职位主表，**不得突破生产硬上限**。`[A-01]`
