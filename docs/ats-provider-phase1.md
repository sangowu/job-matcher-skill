# ATS Provider Phase 1：架构与接入门槛

状态：Phase 1 measured / identity gate implemented / production adapters pending。基于 Job Matcher v2.3.0，日期 2026-08-25。

## 决策摘要

首批只研究 Ashby、Greenhouse Job Board API 与 Lever Postings API。它们提供无需登录的公开职位 GET 接口，但三者的分页契约不同：

- [Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api) 返回当前公开职位的单个 `jobs` 数组；`includeCompensation=true` 可附带结构化薪酬。`isListed=false` 的直链职位不进入聚合结果。
- [Greenhouse Job Board API](https://developer.greenhouse.io/job-board.html) 的公开 GET 不要求认证；`GET /v1/boards/{board_token}/jobs?content=true` 返回单个职位列表，Job Board 的 list jobs 契约未提供职位分页参数。
- [Lever Postings API](https://github.com/lever/postings-api) 只暴露 published 职位，支持 global/EU 实例，并用 `skip` + `limit` 分页；API 不提供跨公司全文搜索。

ATS 仍然是 Web Search 发现后的增强来源，但已有已验证公司标识时，ATS 拉取可以与下一批 Web Search 并发，不需要每轮重新等待 Web Search 才开始。

## 当前接入阻塞

小样本中 412 条职位的 Provider ID 与规范化 URL 全部唯一，即强身份重复为 0；但现有 `company|normalized_title` 弱键会把其中 108 条归入已有标题组，潜在碰撞率 26.21%。这些记录可能是同公司同名但不同地点、团队或 requisition 的独立职位。

因此不能直接把全量 ATS 候选喂给当前 `merge_jobs.py`。生产接入前必须先把“记录身份”和“弱相似匹配”分开：

1. `record_id` 已成为职位记录与评估任务的稳定主键。
2. `identity_keys` 已保存 Provider job ID 等强键；通用规范化 URL 仍保留在 `url_keys`，但不冒充 Provider 强身份。
3. 现有 `dedup_key` 保留公司 + 标题归一化结果，只作为向后兼容弱键。
4. 两条记录都拥有强键且强键不相交时，不得仅凭弱键合并。
5. 一条记录缺少强键时，弱键命中还要校验兼容地点；之后把新发现的强键吸收到同一记录。

“把地点直接拼进现有 dedup key”暂不采用：地点文本会变化，多地点职位也会把同一 posting 人为拆开，不能替代稳定 Provider ID。

## 数据边界

继续保留一个下游职位主表 `data/jobs_table.json`，避免 Web、ATS 和浏览器结果被重复评估；但 ATS 公司标识属于控制面，不写进职位表。

建议下一阶段增加两个本地运行时文件：

- `data/ats_companies.json`：`company_key`、显示名、provider、board token、global/EU instance、状态、首次/最近验证时间、发现来源类别。
- `data/ats_sync_state.json`：每个 board 的最近成功时间、失败类别、页数、请求数和是否截断；不保存 JD、Cookie 或任意异常全文。

标识状态使用 `candidate -> verified -> unavailable`：

- Web Search 看到官方 ATS URL 时只创建 candidate。
- 对解析出的官方域名和 board token 做一次公开 GET；成功且 JSON 契约有效后才晋升 verified。
- 404、429、超时或单次网络失败不能生成永久公司标记；记录分类状态并按 TTL 重试。
- Web Search 不跳过整家公司，只抑制对已验证 ATS board 的重复列表抓取；新闻、未知公司和非 ATS 职位发现仍由 Web Search 负责。

Phase 1 的 `references/ats_phase1_boards.json` 只是可复现样本，不是生产公司标识库。

## 推荐数据流

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

## 建议的独立预算

ATS 调用不计入 `max_websearch_calls`，也不复用浏览器的 3 页上限。下一阶段建议先用以下硬上限进入测试，而不是立即作为稳定默认值发布：

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

## 失败与法律边界

- 只调用供应商公开文档明确用于公开 careers page 的 GET endpoint，不调用申请 POST、Harvest/Hire/Partner 私有 API。
- 使用可识别 User-Agent、超时、页数/请求数上限；429/403 后停止该 board 本轮请求，不自动换代理或借远程浏览器绕过。
- ATS 失败时保留 Web Search 结果；只有已通过相关性筛选且确需 JD 的少量职位才进入浏览器兜底。
- 公开 GET 降低了相对抓取风险，但不等于零法律风险；开源使用者仍需遵守目标公司、ATS、地区和数据用途相关条款。

## Phase 2 验收门槛

进入生产适配器实现前必须同时满足：

1. **已完成**：`record_id` / 强身份迁移；本地回归证明不同 ATS job ID 的同名职位保持独立，并按各自 `record_id` 回写评估。
2. Fake Provider 测试覆盖三种分页、404/429/超时、EU Lever、unlisted Ashby 和部分成功。
3. 指标 schema 增加 PII-safe `ats` operation，至少记录 provider、请求/页数、收到/规范化/初筛/去重数量、耗时、截断与失败类别。
4. 小样本回归保持 6/6 board 成功或对失败给出可复现分类，不保存职位正文、标题和 URL。
5. 远端 Ubuntu/Windows CI 通过后，才允许把 ATS 路由写进 `WORKFLOW.md` 的正式运行步骤。
