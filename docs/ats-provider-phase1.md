# ATS Provider Phase 1/2/3：架构、实现与接入门槛

状态：Phase 1 measured / Phase 2 implemented / Phase 3 controlled discovery-to-merge measured。基于 Job Matcher v2.3.0，更新日期 2026-08-26。

## 决策摘要

首批只研究 Ashby、Greenhouse Job Board API 与 Lever Postings API。它们提供无需登录的公开职位 GET 接口，但三者的分页契约不同：

- [Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api) 返回当前公开职位的单个 `jobs` 数组；`includeCompensation=true` 可附带结构化薪酬。`isListed=false` 的直链职位不进入聚合结果。
- [Greenhouse Job Board API](https://developer.greenhouse.io/job-board.html) 的公开 GET 不要求认证；`GET /v1/boards/{board_token}/jobs?content=true` 返回单个职位列表，Job Board 的 list jobs 契约未提供职位分页参数。`job-boards.eu.greenhouse.io` 公开页面也用于发现 board token，但 API 请求仍发送到官方 `boards-api.greenhouse.io`。
- [Lever Postings API](https://github.com/lever/postings-api) 只暴露 published 职位，支持 global/EU 实例，并用 `skip` + `limit` 分页；API 不提供跨公司全文搜索。

ATS 仍然是 Web Search 发现后的增强来源，但已有已验证公司标识时，ATS 拉取可以与下一批 Web Search 并发，不需要每轮重新等待 Web Search 才开始。

## Phase 1 发现与已解除阻塞

小样本中 412 条职位的 Provider ID 与规范化 URL 全部唯一，即强身份重复为 0；但现有 `company|normalized_title` 弱键会把其中 108 条归入已有标题组，潜在碰撞率 26.21%。这些记录可能是同公司同名但不同地点、团队或 requisition 的独立职位。

因此不能直接把全量 ATS 候选喂给 `merge_jobs.py`。Phase 2 已先把“记录身份”和“弱相似匹配”分开：

1. `record_id` 已成为职位记录与评估任务的稳定主键。
2. `identity_keys` 已保存 Provider job ID 等强键；通用规范化 URL 仍保留在 `url_keys`，但不冒充 Provider 强身份。
3. 现有 `dedup_key` 保留公司 + 标题归一化结果，只作为向后兼容弱键。
4. 两条记录都拥有强键且强键不相交时，不得仅凭弱键合并。
5. 一条记录缺少强键时，弱键命中还要校验兼容地点；之后把新发现的强键吸收到同一记录。

“把地点直接拼进现有 dedup key”暂不采用：地点文本会变化，多地点职位也会把同一 posting 人为拆开，不能替代稳定 Provider ID。

## 数据边界

继续保留一个下游职位主表 `data/jobs_table.json`，避免 Web、ATS 和浏览器结果被重复评估；但 ATS 公司标识属于控制面，不写进职位表。

Phase 2 已增加两个本地运行时文件：

- `data/ats_companies.json`：`company_key`、显示名、provider、board token、global/EU instance、状态、首次/最近验证时间、发现来源类别。
- `data/ats_sync_state.json`：每个 board 的最近成功时间、失败类别、页数、请求数和是否截断；不保存 JD、Cookie 或任意异常全文。

标识状态使用 `candidate -> verified -> unavailable`：

- Web Search 看到官方 ATS URL 时只创建 candidate。
- 对解析出的官方域名和 board token 做一次公开 GET；成功且 JSON 契约有效后才晋升 verified。
- 404、429、超时或单次网络失败不能生成永久公司标记；记录分类状态并按 TTL 重试。
- Web Search 不跳过整家公司，只抑制对已验证 ATS board 的重复列表抓取；新闻、未知公司和非 ATS 职位发现仍由 Web Search 负责。

Phase 1 的 `references/ats_phase1_boards.json` 只是可复现样本，不是生产公司标识库。

## 已实现数据流

```text
CV + 求职条件
  -> Web Search 发现未知公司/职位
  -> ATS registry 解析（known verified / new candidate / unknown）
      -> known verified boards：跨公司最多 3 并发
          -> Ashby/Greenhouse：单响应
          -> Lever：同 board 内顺序 skip/limit 翻页，最多 10 页
      -> new candidate markers：异步公开 GET 验证
      -> unknown：保留原 Web/JD/浏览器容错路径
  -> ATS 内存归一化
  -> 本地 title/location 初筛与每轮候选上限
  -> 强身份优先的统一 merge（单写入器）
  -> 只评估新增且相关的职位
```

跨公司 ATS 拉取、Web Search 和已派发的 JD 评估可以并发；同一 Lever board 的分页必须串行；最终主表写入仍必须串行。

## 独立预算

ATS 调用不计入 `max_websearch_calls`，也不复用浏览器的 3 页上限。生产路由由 `ats_enabled: false` 默认关闭；显式启用后仍受以下硬上限约束：

| 预算 | Phase 2 初值 |
|---|---:|
| `ats_max_concurrency` | 3 |
| `ats_boards_per_round` | 10 |
| `ats_requests_per_round` | 30 |
| `ats_page_size`（Lever） | 50 |
| `ats_max_pages`（Lever） | 10 |
| `ats_timeout_seconds` | 30 |
| `ats_registry_ttl_days` | 30 |

API 返回的是整板职位，不能把所有职位直接送给 LLM。必须先做确定性的 title/location 初筛，再受 `top_n + precise_buffer` 与单轮评估预算约束。

Greenhouse 的 `content=true` 整板响应可能超过 25 MB。实现会记录已读取字节数，并仅在 `response_too_large` 时使用同一全局请求预算重试一次不含正文的列表；预算不足或第二次失败时按该 board 失败降级，不扩大请求上限。

## 失败与法律边界

- 只调用供应商公开文档明确用于公开 careers page 的 GET endpoint，不调用申请 POST、Harvest/Hire/Partner 私有 API。
- 使用可识别 User-Agent、超时、页数/请求数上限；429/403 后停止该 board 本轮请求，不自动换代理或借远程浏览器绕过。
- ATS 失败时保留 Web Search 结果；只有已通过相关性筛选且确需 JD 的少量职位才进入浏览器兜底。
- 公开 GET 降低了相对抓取风险，但不等于零法律风险；开源使用者仍需遵守目标公司、ATS、地区和数据用途相关条款。

## Phase 2 验收状态

生产适配器的发布条件与当前状态：

1. **已完成**：`record_id` / 强身份迁移；本地回归证明不同 ATS job ID 的同名职位保持独立，并按各自 `record_id` 回写评估。
2. **已完成**：Fake Provider 覆盖 Ashby/Greenhouse 单响应、Lever 顺序分页与 EU host、unlisted Ashby、404/429/超时、全局请求预算、关闭开关和部分成功。
3. **已完成**：指标 schema v4 增加 PII-safe `ats` operation，记录 provider、请求/页数、收到/规范化/初筛/输出数量、耗时、截断与分类状态。
4. **已完成**：2026-08-26 公开小样本回归 6/6 board 成功，7 个请求接收并规范化 414 条职位，强身份重复率 0%，无截断/限流；脱敏证据不保存职位正文、标题和 URL。见 `docs/performance/ats-phase2-public-api-regression.*`。
5. **已完成**：PR #20 首轮 [GitHub Actions 32962486697](https://github.com/sangowu/job-matcher-skill/actions/runs/32962486697) 的 Ubuntu/Windows Python 3.10 均通过。正式路由已经写入 `WORKFLOW.md`，但默认开关保持关闭，只有使用者显式启用后才会发出 ATS 请求。

## Phase 3 受控端到端结果

2026-08-26 使用固定的 5 条 Web 候选作为对照组，并在生产硬上限内同步 3 个公开 board。Web-only 与 Web+ATS 分别写入隔离临时主表，再复用生产 `ats_pipeline.sync_registry` 与 `merge_jobs.py` 比较 discovery-to-merge 结果：

- 3/3 board 成功，5 个公开 GET 请求共读取 48,955,686 bytes；其中 1 个 Greenhouse board 因含正文响应超过 25 MB，消耗第 2 个请求降级到列表模式。
- ATS 接收并规范化 5,492 条公开职位，确定性初筛保留 22 条，受 `top_n + precise_buffer` 限制输出 20 条。
- 统一强身份 merge 后，Web-only 为 5 条唯一记录，Web+ATS 为 24 条；新增 19 条，避免 1 次重复评估，原 5 条 Web 记录全部保留。
- ATS 同步耗时 12,568.189 ms；Web-only merge 为 27.636 ms，Web+ATS merge 为 85.493 ms，ATS arm discovery-to-merge 总计 12,653.682 ms。

这只证明候选发现、身份去重与 Web 结果保留的机械链路。20 条 ATS 输出尚未进入 JD 评分或人工相关性审计，不能据此声称新增 19 条都是高质量职位；ATS 候选的 JD 正文也尚未直接交给评估 worker，因此没有测得浏览器兜底减少。低请求数也不等于低数据量，本次约 49 MB 的响应说明后续仍需关注整板体积。脱敏结果见 `docs/performance/ats-phase3-controlled-e2e.*`。

## 实现入口

- `scripts/ats_provider.py`：统一 Provider 协议、公开 HTTPS GET、三家 payload 规范化、Lever 顺序分页、线程安全请求预算和 Fake Provider。
- `scripts/ats_pipeline.py discover`：从 Web 候选识别 allowlist 官方 board，并更新 `candidate -> verified -> unavailable` 标识库。
- `scripts/ats_pipeline.py sync --profile <path>`：同步已到期 board，跨 board 有界并发，按 CV 做确定性初筛并输出 merge-ready 候选。
- `scripts/ats_pipeline.py run --profile <path>`：从 stdin 接收一批 Web 候选，串联 discover + sync。
- `scripts/benchmark_ats.py`：复用生产适配器的公开脱敏回归；`scripts/benchmark_pipeline.py` 另含完全离线的三 Provider Fake 基准。
- `scripts/benchmark_ats_e2e.py`：固定 Web 对照组与受限公开 ATS 的 PII-safe discovery-to-merge A/B；不保存职位标题、公司、URL、CV/profile 字段、board token 或异常全文。

标识库和同步状态由编排者每轮最多调用一次，文件写入不是给多个独立编排者同时竞争的分布式协调机制；职位主表仍只允许 `merge_jobs.py` 串行写入。
