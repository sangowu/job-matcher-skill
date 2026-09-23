# 多地区职位发现与合并实施 TODO

> **本文是设计规格，不是进度看板。** 下面 200 余个复选框是立项时写下的验收标准，
> 实现完成后从未回来勾选，因此**全部未勾不代表全部未做**——Phase 0/A/B/C/D/F 早已交付。
> 把它当待办清单读会严重误导。
>
> 权威状态看两处：已交付内容看 `CHANGELOG.md` 与 `docs/releases/`，
> 发布门禁看 `python scripts/shadow_gate.py status --live-smoke <file>` 的实时判定。
> 第 15 节的阶段状态由 `tests/test_docs.py` 对 `config.json` 做一致性校验，不会再悄悄过期。

状态：Phase 0/A/B/C/D/F 已交付（v2.4.0）；**Phase E 未达标**，多地区默认启用仍关闭。
首批测试市场：Ireland / UK / China / Germany

## 1. 目标

让不同地区的用户安装同一个 skill 后，系统能够根据**用户明确指定的目标市场**，稳定完成：

1. 解析目标国家、城市、远程范围和求职语言；
2. 从当地招聘平台、已验证 ATS board、大公司招聘门户和 Agent 开放搜索中发现职位；
3. 把所有发现管道的结果转换为同一候选契约；
4. 通过现有 `merge_jobs.py` 单写入器统一去重、缓存、建立评估快照并落库；
5. 获取 JD、验证职位有效性、完成五维评分；
6. 在 HTML 报告中按市场、地点、来源和验证状态过滤与展示；
7. 用可审计指标说明每个地区和来源实际贡献了什么，不把“未收集”误写成“没有职位”。

本阶段不追求“覆盖全球所有网站”，而是建立可扩展的数据模型、执行管道和四市场验证基线。

### 1.1 术语

- **冷启动（cold start）**：`data/source_registry.json` 不存在，只使用版本控制中的市场配置和来源种子生成计划；
- **已验证来源（verified source）**：`enabled=true`、`verified=true`，且验证时间未超过 TTL 的公开只读来源；
- **发现路径（discovery path）**：能够实际执行并返回有效候选数组（可以为空）的来源类型，不只是文档中存在一个名称；
- **Agent query slot**：一次明确语言、市场、角色和来源模板的 Web Search 调用；翻页占用新的 slot；
- **live verified**：已打开真实职位详情页，确认存在职位正文和可见申请路径，且没有关闭信号；
- **来源失败**：来源被实际执行，但因超时、HTTP/解析错误、登录、验证码或契约错误未产生可信结果；零职位不是来源失败；
- **pipeline run id**：整轮搜索、合并、评估、报告和指标共享的运行 ID，不等于 `eval_run.run_id`；
- **单写入器**：只有主编排器能够调用持锁的提交脚本修改共享主表、评估状态和来源注册表。

## 2. 完成定义

只有同时满足以下条件，才能称为“完成多地区实现”：

- [ ] Ireland、UK、China、Germany 四个市场都能从冷启动生成非空的地区检索计划；
- [ ] 搜索语言由目标市场决定，不再直接等于 CV 语言；
- [ ] 用户 query 中明确给出的目标地点可以覆盖 CV 中的现居地/默认地点；
- [ ] 每个市场至少有一种当地平台发现路径、一种公司/ATS 路径和 Agent 开放搜索路径；
- [ ] 地区资源管道与 Agent 搜索管道可以并行产生候选，但不能直接并发写 `jobs_table.json`；
- [ ] 两条管道的候选进入同一个 `merge_jobs.py merge`，跨来源重复职位只建立一个职位记录和一个评估任务；
- [ ] 报告中的职位均带有目标市场、来源类型、发现路径、链接验证状态和评分依据；
- [ ] 某一来源失败不会阻塞其他来源或整轮报告；
- [ ] 自动化测试覆盖四市场、双语搜索、地点归一化、来源选择、合并冲突和报告过滤；
- [ ] 至少完成一次四市场受控离线基准和一次显式触发的公开来源 live smoke；
- [ ] 不宣称市场级 recall；所有覆盖结论都附带来源清单、运行时间和证据边界。

### 2.1 四市场验收矩阵

| 市场 | 语言代码 | 最低已验证种子 | Agent Query 必备类型 | 离线验收 | Live smoke 最低执行 |
|---|---|---:|---|---|---|
| Ireland | `en` | 3 当地入口 + 10 公司/ATS | 通用角色+城市、当地站点定向、ATS 定向 | routing/merge/report 全通过 | 1 当地入口 + 1 公司/ATS + 1 Agent query |
| UK | `en` | 3 当地入口 + 10 公司/ATS | 通用角色+城市、当地站点定向、ATS 定向 | routing/merge/report 全通过 | 1 当地入口 + 1 公司/ATS + 1 Agent query |
| China | `zh-Hans`, `en` | 3 公开入口 + 10 公司/ATS | 中文当地平台、英文公司/ATS、至少一条中文通用搜索 | routing/merge/report 全通过 | 1 公开入口 + 1 公司/ATS + 中英各 1 Agent query |
| Germany | `de`, `en` | 3 当地入口 + 10 公司/ATS | 德语当地搜索、英语公司/ATS、至少一条德语通用搜索 | routing/merge/report 全通过 | 1 当地入口 + 1 公司/ATS + 德英各 1 Agent query |

Live smoke 判定：

- **pass**：最低执行项均实际运行，至少一个已验证来源成功、必备 Agent query 成功，所有返回候选通过契约校验，且没有共享状态/合并错误；允许真实职位数为零，但必须显示覆盖边界；
- **fail**：市场计划、schema、语言路由、候选契约、合并或报告出现确定性代码错误；
- **inconclusive**：网络、限流、验证码、登录或外部站点故障导致最低执行项无法完成；不得把 inconclusive 当作 pass；
- 四市场固定 fixture 必须全部 pass；live smoke 可以因外部环境 inconclusive，但发布说明必须列出原因和未覆盖市场。

## 3. 固定架构决策

### 3.1 输入优先级

地点来源优先级固定为：

```text
用户本轮明确指定的目标地点
  > 用户保存的求职偏好（未来可选）
  > CV 中明确写出的求职地点
  > CV 现居地（仅作为最后回退）
```

- [ ] 将“现居地”和“目标求职地”拆成不同概念；
- [ ] query 明确写出新地点时，不再沿用 CV 地点；
- [ ] 完全没有目标地点时停下追问，不做国家猜测；
- [ ] 多市场请求保留用户给出的顺序，作为预算分配优先级。

### 3.2 语言模型

不得继续使用 `search_language = CV 语言` 作为唯一搜索语言。拆分为：

```json
{
  "report_language": "zh-Hans",
  "search_languages": ["en"],
  "target_markets": ["ie"],
  "target_locations": ["Dublin"]
}
```

- `report_language`：报告和解释使用的语言；优先级为“用户本轮明确指定 > 保存偏好 > CV 主语言 > `en`”；
- `search_languages`：目标市场招聘信息实际使用的语言；优先级为“用户本轮明确的职位语言限制 > 来源支持语言与市场默认语言的交集 > 市场默认语言”；
- 职位标题、公司名、技能名保留来源原文；
- JD 可以使用当地语言解析，最终解释翻译到 `report_language`；
- 不得为了翻译而修改职位硬性要求、薪资、地点或资格条件。

语言代码统一使用 BCP 47：`en`、`de`、`zh-Hans`。现有 `zh`/`zh-CN` 输入在边界处规范化为 `zh-Hans`，不得在内部状态混用。

新增 `references/role_taxonomy.json`：

- 每个角色族使用稳定 `role_family_id`；
- 保存按语言分组的正式标题和同义词，不保存完整 query；
- 未知角色只保留用户原文，并用目标市场语言生成通用 `jobs/careers` 包装词；不得低置信度自动翻译成另一个专业方向；
- 双语市场为每种语言生成独立 Query，不能把两种语言混进同一 Query；
- 单市场默认仍受全局 6 次 Web Search 上限约束：Ireland/UK 先执行 2 个英语通用 slot，再按来源优先级追加；China/Germany 先按“主要当地语言 1 条 → 英语 1 条”执行，再 round-robin 追加；
- 多市场请求先给每个市场分配一个通用 slot，再按用户市场顺序和语言顺序 round-robin；预算不足以覆盖每个市场时必须先要求用户缩小范围。

### 3.3 双发现管道、单写入器

```text
                           ┌─ 地区资源管道 ────────────────┐
CV + 用户目标 → 市场解析 ──┤  本地平台 / ATS / 公司门户     ├─ 候选数组
                           └─ Agent 开放搜索管道 ──────────┘
                              动态 query / 长尾来源发现
                                             │
                                             ▼
                                  主编排器串行 merge
                                             │
                          jobs_table + eval_run + source registry
                                             │
                                             ▼
                                    JD 精排与 HTML 报告
```

- [ ] 搜索/抓取 Worker 只返回候选或写独立临时文件；
- [ ] Worker 不直接修改 `jobs_table.json`、评估快照或共享来源状态；
- [ ] 主编排器是唯一提交者；
- [ ] 继续复用跨进程锁、原子替换、`record_id`、强身份键和 `jd_input_hash` 冲突保护；
- [ ] 同一批候选可一次 merge；候选分批到达时允许多次串行 merge；
- [ ] 继续使用 `in_evaluation` 防止两条发现管道重复派发同一职位。

唯一写入协议：

1. Worker 输出不可变候选批次和来源提案，不修改共享文件；
2. 主编排器为每个批次生成幂等 `batch_id`，重复提交同一批次不得重复增加 `seen_count` 或评估任务；
3. 主编排器先调用 `merge_jobs.py merge` 提交职位，再通过独立持锁命令串行提交来源状态；
4. `jobs_table.json`、eval run 和 source registry 分别原子替换；某一步失败时保留可重试 manifest，不伪装为整批成功；
5. 进程崩溃后根据 `batch_id`/manifest 判断已完成步骤，重试不能重复派发评估；
6. 共享状态提交顺序、结果和失败类型写入 PII-safe 指标。

## 4. 数据模型

### 4.1 市场配置：`references/markets.json`

新增版本控制、无 PII 的市场配置文件。建议结构：

```json
{
  "schema_version": 1,
  "markets": [
    {
      "market_id": "ie",
      "country": "Ireland",
      "country_aliases": ["Ireland", "Republic of Ireland", "IE"],
      "default_search_languages": ["en"],
      "cities": [
        {
          "city_id": "dublin",
          "name": "Dublin",
          "aliases": ["Dublin", "County Dublin", "Greater Dublin"]
        }
      ],
      "remote_scopes": ["Ireland", "EU", "EMEA"],
      "source_ids": ["irishjobs-web", "jobs-ie-web"],
      "query_templates": [
        "{role} jobs {city}",
        "site:{domain} {role} {city}"
      ]
    }
  ]
}
```

待办：

- [ ] 定义 JSON Schema 或等价 Python 校验器；
- [ ] 市场 ID 使用稳定的小写代码：`ie`、`uk`、`cn`、`de`；
- [ ] 城市别名与国家别名分开保存；
- [ ] 支持一个城市属于国家、行政区和远程范围的层级匹配；
- [ ] 不在该文件保存 CV、query、职位 URL、JD 或个人偏好；
- [ ] 无法识别的地点返回 `unknown`，不能自动归到错误市场。

### 4.2 来源种子：`references/source_seeds.json`

保存经过人工验证、可以公开分发的初始资源：

```json
{
  "source_id": "example-greenhouse",
  "display_name": "Example careers",
  "source_type": "ats_board",
  "provider": "greenhouse",
  "board_token": "example",
  "markets": ["ie", "uk"],
  "search_languages": ["en"],
  "enabled": true,
  "verified": true,
  "verification_method": "public_read_only_endpoint",
  "verified_at": "2026-09-17T00:00:00Z",
  "priority": 80
}
```

来源类型：

- `ats_board`：公开 ATS board/API；
- `company_careers`：大公司招聘门户；
- `local_job_board`：当地综合或垂直招聘平台；
- `public_sector_portal`：政府、公共部门或大学招聘门户；
- `web_query_template`：只通过 Web Search 使用的站点定向模板。

待办：

- [ ] `enabled: true` 和 `verified: true` 同时成立才进入确定性地区资源管道；
- [ ] 全球 ATS board 只登记一次，通过 `markets` 声明覆盖地区；
- [ ] 不为同一家跨国公司的同一 board 按国家复制多份；
- [ ] 每个来源记录验证方式、时间、优先级和允许的访问方式；
- [ ] 禁止在种子文件中存储 API key、Cookie、登录信息或个人数据；
- [ ] 对需要登录、验证码或禁止自动访问的资源，只保留 Web Search/人工入口，不直接抓取。

### 4.3 运行时来源注册表：`data/source_registry.json`

运行时注册表由公开种子和 Agent 新发现的来源合成，保存在已忽略的 `data/`：

- 状态：`candidate` / `verified` / `unavailable` / `disabled`；
- 状态迁移限定为：新发现 → `candidate`；验证成功 → `verified`；连续确定性不可用达到阈值 → `unavailable`；后续验证成功可恢复为 `verified`；`disabled` 只由本地配置/用户决定；
- 保存 `first_seen_at`、`last_seen_at`、`last_attempt_at`、`last_success_at`；
- 保存连续失败次数和 TTL；
- Agent 新发现的来源先进入 `candidate`，验证后才能进入稳定地区资源管道；
- 404/410 连续达到阈值后才标记 `unavailable`；
- 429、超时和临时网络错误不得永久禁用来源；
- 来源健康状态不包含职位名、URL、JD、CV 或 query。
- Worker 只能返回来源提案；主编排器通过持锁、幂等的 registry 提交命令应用状态迁移。

### 4.4 统一候选契约

所有发现路径输出同一结构：

```json
{
  "title": "",
  "company": "",
  "location": "",
  "location_normalized": {
    "market_id": "ie",
    "city_id": "dublin",
    "remote_scope": "",
    "confidence": "exact"
  },
  "url": "",
  "snippet": "",
  "date_posted": "",
  "salary": "",
  "source": "greenhouse",
  "source_id": "example-greenhouse",
  "discovery_route": "regional_registry",
  "search_language": "en",
  "observed_at": "",
  "identity_keys": [],
  "link_verification_status": "unknown"
}
```

`discovery_route` 枚举：

- `regional_registry`
- `agent_web_search`
- `ats_expansion`
- `company_careers`

待办：

- [ ] 在现有候选校验中接受新增字段；
- [ ] 定义正式 JSON Schema：必填 `title/company/url/source_id/discovery_route/observed_at`；可空 `location/snippet/date_posted/salary`；时间使用 UTC RFC 3339；文本和数组设置长度上限；
- [ ] `raw_sources[]` 为 provenance 唯一事实源，每次观察保存 `source_id`、`source_type`、`discovery_route`、`search_language`、`observed_at` 和验证状态；候选顶层单值不能覆盖历史来源；
- [ ] `source_type` 表示资源形态，`discovery_route` 表示本轮发现管道，`source_id` 指向注册表中的具体资源；
- [ ] `market_id` 不是职位身份键，不能导致同一全球职位被复制；
- [ ] 不同地区确实存在不同 requisition/job ID 时保持为不同职位；
- [ ] 发现 CandidateEnvelope 不携带 `jd_text` 或评分字段；
- [ ] 另定义内部 `EvalHandoff`：以 `record_id`、`jd_input_hash`、正文来源、截断标记和受限 `jd_text` 组成，只写 run-scoped 评估快照；
- [ ] `jd_text` 上限继续为 50,000 字符，任务完成/冲突立即删除；主表只保留内容 hash；
- [ ] `scored_from` 只由评估结果写回，不允许发现 Worker 伪造。

## 5. 市场解析与地点归一化

新增确定性脚本，例如 `scripts/market_plan.py`：

输入：

```json
{
  "cv_profile": {},
  "user_intent": {
    "roles": ["AI Engineer"],
    "locations": ["Dublin"],
    "report_language": "zh-Hans"
  }
}
```

输出：

```json
{
  "target_markets": ["ie"],
  "target_locations": ["dublin"],
  "report_language": "zh-Hans",
  "search_languages": ["en"],
  "regional_source_ids": [],
  "search_plan": [],
  "warnings": []
}
```

待办：

- [ ] 实现地点别名规范化；
- [ ] 实现城市 → 国家/市场层级关系；
- [ ] 实现 `Remote Ireland`、`Remote UK`、`Remote Germany`、`Remote EU/EMEA` 的范围判断；
- [ ] 区分 `remote`、`hybrid` 和 `onsite`；
- [ ] 多个市场时按用户顺序做 round-robin 查询预算分配；
- [ ] 每个目标市场至少分配一个 Agent query 槽位；
- [ ] 确定性来源同步使用独立请求预算，不占 `max_websearch_calls`；
- [ ] 目标市场无法识别时返回警告并追问，不静默回退到全球搜索。

## 6. 四个测试市场的语言与资源计划

下表中的平台和公司均为**待验证种子候选**。在准确 URL、公开访问方式、地区覆盖和 ToS 检查完成前，不得设置 `verified: true`。

| 市场 | 默认搜索语言 | 初始当地平台候选 | 公司/ATS 策略 | 关键风险 |
|---|---|---|---|---|
| Ireland (`ie`) | `en` | IrishJobs、Jobs.ie、publicjobs.ie、gradireland | Ireland 有职位的大公司公开 ATS board；Greenhouse/Ashby/Lever 优先 | Dublin 与 Ireland 层级、UK 结果混入、签证信息缺失 |
| UK (`uk`) | `en` | Reed、Totaljobs、CV-Library、jobs.ac.uk | UK 大公司门户和公开 ATS；需要时通过公司 careers 页面 | London/Greater London、remote UK、合同岗位、sponsorship |
| China (`cn`) | `zh-Hans` + `en` | BOSS直聘、猎聘、拉勾、前程无忧的公开索引入口 | 中文平台 Web Search + 跨国公司英文/中文 careers/ATS | 登录/验证码、反爬、城市中文别名、薪资单位、页面可访问性 |
| Germany (`de`) | `de` + `en` | StepStone DE、Bundesagentur für Arbeit、Indeed DE、XING 的公开入口 | 德国公司门户 + 跨国 ATS；德语和英语职位分别查询 | Berlin/Berlin-Brandenburg、EU remote、德语资历词、签证/Blue Card |

### 6.1 Ireland

- [ ] 建立 `ie` 市场、Dublin/Cork/Galway/Limerick 城市别名；
- [ ] 英语是默认搜索语言，不因为中文 CV 改成中文职位词；
- [ ] 验证至少 3 个当地平台入口；
- [ ] 验证至少 10 个 Ireland 有职位的大公司/ATS 来源；
- [ ] 加入 `Ireland`、`Dublin`、`Remote Ireland`、`EMEA remote` 测试；
- [ ] 明确 `Northern Ireland` 默认属于 UK 市场，不能并入 Ireland；
- [ ] 记录 work authorization/sponsorship 为评估信号，但不根据国籍做猜测。

### 6.2 UK

- [ ] 建立 `uk` 市场以及 London/Manchester/Edinburgh/Belfast 等初始城市别名；
- [ ] 验证至少 3 个当地平台入口；
- [ ] 验证至少 10 个 UK 有职位的大公司/ATS 来源；
- [ ] 处理 `UK-wide`、`Remote UK`、`Greater London`；
- [ ] Belfast/Northern Ireland 归 UK，但报告中保留原始地点；
- [ ] sponsorship 只根据 JD 明确信息判断，未说明时标记 unknown。

### 6.3 China

- [ ] 建立 `cn` 市场以及北京、上海、深圳、广州、杭州、成都等初始城市别名；
- [ ] 默认同时生成中文和英文 Query；中文职位用于本地平台，英文职位用于跨国公司 careers/ATS；
- [ ] 建立 AI/ML/LLM/后端等角色的中英文同义词表；
- [ ] 验证至少 3 个无需绕过登录/验证码即可发现职位的公开入口；
- [ ] 验证至少 10 个在中国招聘的大公司 careers/ATS 来源；
- [ ] 不自动登录、不解验证码、不使用 stealth/代理绕过限制；
- [ ] 对只能看到摘要的职位标记 `scored_from=snippet` 和 `verified=unknown`；
- [ ] 统一识别年薪/月薪、人民币单位和 `13薪/14薪`，但不在本阶段做复杂总包换算。

### 6.4 Germany

- [ ] 建立 `de` 市场以及 Berlin/Munich/Hamburg/Frankfurt/Cologne 等初始城市别名；
- [ ] 默认分别执行德语和英语 Query，不能把两种语言拼成一条低质量 Query；
- [ ] 建立常用德英职位词映射，例如 `Softwareentwickler` / `Software Engineer`；
- [ ] 验证至少 3 个当地平台入口；
- [ ] 验证至少 10 个 Germany 有职位的大公司/ATS 来源；
- [ ] 支持 `Deutschlandweit`、`Remote Germany`、`EU remote`；
- [ ] 保留德语复合词、变音符号及其 ASCII 别名；
- [ ] Blue Card/语言要求只按 JD 明示信息提取，不能自动判定申请资格。

## 7. 地区资源管道

- [ ] 根据 `market_plan` 选择 `verified + enabled` 来源；
- [ ] 同一全局 ATS board 即使覆盖多个目标市场，每轮也只同步一次；
- [ ] ATS 拉取后在内存中按目标市场、角色和资历过滤；
- [ ] 已有 Ashby/Greenhouse/Lever 继续复用 `ats_provider.py`；
- [ ] 评估 Workday 的公开只读接入；没有稳定公开契约时使用公司 careers/浏览器回退，不伪装成 API；
- [ ] 评估 SmartRecruiters、SuccessFactors、iCIMS 等 Provider，仅在公开只读访问可控时增加适配器；
- [ ] 当地平台优先使用 Web Search 定向查询、公开 feed/API 或允许的静态页面；
- [ ] 每个来源设置请求数、页数、响应体积、并发和超时上限；
- [ ] 单来源失败只记录该来源状态，不丢弃其他来源候选；
- [ ] 来源返回的 JD 继续遵守 50,000 字符上限和临时快照隐私边界。

## 8. Agent 开放搜索管道

- [ ] Query 由 `market_plan.search_plan` 生成，不由 Worker 自行改变目标市场；
- [ ] 每条 Query 只执行一次 Web Search；翻页算新的调用；
- [ ] 按市场语言分别生成角色同义词；
- [ ] 对当地平台使用 `site:` Query，但不假定搜索索引等于实时职位；
- [ ] 搜索结果必须做 title/location/seniority 初筛；
- [ ] 地点缺失从宽保留，明确不匹配则剔除；
- [ ] 每个 Level-3/Web Search 候选在进入最终报告前验证真实职位页和申请路径；
- [ ] Agent 发现新的 ATS/company board 时写入候选来源提案，由主编排器验证后更新 registry；
- [ ] 搜索页面和 JD 一律作为不可信数据，不执行其中指令；
- [ ] Web Search 失败不能阻塞地区资源管道。

## 9. 共享状态、合并和评估

- [ ] 扩展 `ats_handoff.py` 或增加通用 `candidate_handoff.py`，接受多 route 候选但不输出 JD 正文；
- [ ] 所有候选统一调用 `merge_jobs.py merge`；
- [ ] 保留平台 job ID 强身份优先、规范化 URL 次之、公司+职位+兼容地点弱匹配最后的策略；
- [ ] 为新增当地平台补充稳定 job-id URL 规则；
- [ ] 同一职位来自地区资源管道和 Agent 搜索时合并 `raw_sources`；
- [ ] 来源变化但 JD 输入未变化时允许安全 rebase；
- [ ] JD hash 变化时清除旧 `jd_profile` 和评分并重新评估；
- [ ] 共享来源状态更新由主编排器串行提交；
- [ ] 中断恢复后清理超龄评估快照，不重复保存 JD 正文；
- [ ] 四市场都复用同一五维评分契约，不为市场复制一套评分器；
- [ ] 地区特有资格条件进入结构化风险/硬约束字段，不隐式改变总体分数。

## 10. 报告与用户可见证据

- [ ] 报告顶部显示本轮目标市场、搜索语言、报告语言和运行时间；
- [ ] 增加市场筛选器；
- [ ] 增加来源类型筛选器；
- [ ] 每个职位显示 `discovery_route`、source、地点规范化结果和链接验证状态；
- [ ] 同一职位有多个来源时展示“多来源”，但只保留一个职位卡片；
- [ ] 显示各市场已执行/失败/跳过的来源数量；
- [ ] `unknown`、未收集和来源失败不能显示为“0 个职位”；
- [ ] 地区资源覆盖不足时输出“初步结果/覆盖有限”，不输出永久性无职位结论；
- [ ] 保持现有中英 i18n，并让自然语言分析使用 `report_language`；
- [ ] 原始职位标题、公司、薪资和地点保持来源语言；
- [ ] 报告只渲染并打开一次。

## 11. 指标与可观测性

新增低基数、PII-safe 指标字段：

- `market_id`
- `source_type`
- `discovery_route`
- `search_language`
- `sources_planned`
- `sources_succeeded`
- `sources_failed`
- `candidates_raw`
- `candidates_prefiltered`
- `candidates_unique`
- `candidates_incremental`
- `jd_handoff_count`
- `live_verified_count`
- `top_n_contribution`

待办：

- [ ] 指标不得记录 query 原文、URL、职位名、公司名、CV hash、JD、board token 或异常全文；
- [ ] 分别计算地区资源管道和 Agent 搜索的新增贡献；
- [ ] 记录两条管道的重复交集；
- [ ] 记录每个 route 最终进入 Top-N 的数量；
- [ ] 记录每个市场的链接失效率和 JD 完整率；
- [ ] 只有实际并行发生时才把 orchestration 标为 `overlapped`；
- [ ] 指标缺失时健康状态为 unknown，不得假装为零。

## 12. 自动化测试

### 12.1 市场配置与解析

- [ ] `markets.json` schema 校验；
- [ ] 重复 market/source/city ID 拒绝；
- [ ] 未知 source 引用拒绝；
- [ ] Ireland/UK/China/Germany 冷启动计划均非空；
- [ ] 用户 query 地点覆盖 CV 地点；
- [ ] 无地点时返回需要用户输入；
- [ ] 多市场预算按顺序 round-robin 分配；
- [ ] 无法识别地点不被静默归类。

### 12.2 搜索语言

必须包含以下回归用例：

| CV/报告语言输入 | 目标市场 | 期望内部搜索语言代码 |
|---|---|---|
| `zh` | Ireland | `en` |
| `en` | China | `zh-Hans`, `en` |
| `zh-CN` | Germany | `de`, `en` |
| `en` | Germany | `de`, `en` |
| `de` | UK | `en` |

- [ ] 验证不同语言生成独立 Query；
- [ ] 验证报告语言不改变搜索语言；
- [ ] 验证 `zh`/`zh-CN` 只在输入边界出现，内部统一为 `zh-Hans`；
- [ ] 验证语言 × 来源类型 × query template 的路由，而不只断言语言列表；
- [ ] 验证职位原文不被错误翻译后用于身份去重；
- [ ] 验证中德英同义角色映射不会把低信息量 token 单独当成匹配。

### 12.3 地点匹配

- [ ] Dublin ↔ County Dublin；
- [ ] Ireland 不等于 Northern Ireland；
- [ ] Belfast → UK；
- [ ] London ↔ Greater London；
- [ ] 北京 ↔ Beijing；上海 ↔ Shanghai；深圳 ↔ Shenzhen；
- [ ] München ↔ Munich；Köln ↔ Cologne；
- [ ] `Remote UK` 不匹配只把 Germany 作为目标市场的用户；
- [ ] Remote EU/EMEA 根据明确规则匹配，不根据国籍猜测；
- [ ] location 缺失保留为 unknown。

### 12.4 来源注册表

- [ ] 只有 `enabled + verified` 来源进入确定性计划；
- [ ] candidate 来源不自动启用；
- [ ] TTL 到期重新验证；
- [ ] 临时超时保持可重试；
- [ ] 连续 404/410 达阈值后标记 unavailable；
- [ ] 一个全球 board 覆盖多个市场时每轮只请求一次；
- [ ] 来源注册表不含 PII/JD/query。

### 12.5 双管道合并

- [ ] 地区资源与 Agent 搜索同时返回同一职位，只建立一条记录；
- [ ] 两个不同 job ID、相同公司和标题的职位不被误合并；
- [ ] 多语言标题但相同强身份可以合并；
- [ ] 多市场职位只评估一次；
- [ ] 正在评估的职位进入 `in_evaluation`；
- [ ] JD 输入改变时拒绝过期 Worker 结果；
- [ ] 单一来源失败不影响其他来源落库；
- [ ] 所有共享写入保持串行且原子。

### 12.6 报告

- [ ] 市场、来源、验证状态筛选器正确；
- [ ] 无候选与来源失败显示不同状态；
- [ ] 多来源合并职位只渲染一次；
- [ ] 中文报告可显示英语/德语 JD 分析；
- [ ] 英文报告可显示中文职位原始字段；
- [ ] HTML/JSON/URL 注入防护继续通过；
- [ ] 只打开一次报告。

## 13. 四市场受控基准

建立不依赖网络的固定 fixture：

- 每个市场至少 10 条候选；
- 同时包含当地平台、ATS、公司门户和 Web Search route；
- 包含跨 route 重复、同标题不同 job ID、失效链接、缺地点、remote/hybrid；
- China 和 Germany fixture 同时包含当地语言与英语职位；
- 固定真实职位数作为去重 ground truth；
- 不包含真实 CV、个人信息或受版权限制的完整 JD。

验收指标：

- [ ] 强身份职位去重 recall = 100%；
- [ ] 不同强身份误合并 = 0；
- [ ] 四市场 routing fixture = 100% 正确；
- [ ] 五个语言路由回归用例 = 100% 正确；
- [ ] Web-only 与 regional+Web 两组都保留全部固定 Web 候选；
- [ ] regional+Web 产生预期的新增唯一候选；
- [ ] 重复候选不增加评估任务；
- [ ] PII/JD 泄漏扫描 = 0；
- [ ] 所有指标事件都能归属于 pipeline run id。

## 14. Live smoke 规则

Live smoke 必须显式运行，不进入默认 CI：

- [ ] 每个市场选择少量经过验证的公开来源；
- [ ] 使用固定角色集合和固定地点；
- [ ] 每个来源设置严格请求/页面/响应体积/超时上限；
- [ ] 记录来源成功率、候选漏斗、链接有效率、JD 覆盖和耗时；
- [ ] 不保存公开报告中不必要的公司名、职位名、URL 或 JD；
- [ ] China 遇到登录/验证码立即停止该来源，不绕过；
- [ ] 外部失败标为环境/来源状态，不把它误判成代码回归；
- [ ] 结果只能支持“这些来源在这次运行中的表现”，不能支持市场级 recall 声明。

## 15. 分阶段交付

### 15.0 实际交付状态（2026-09-23 对账）

逐项比对仓库后的结论。下面各 Phase 小节保留原始验收标准原文，不再作为进度使用。

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| Phase 0 契约与基线 | 已交付 | `docs/multi-region-phase0-baseline.md`、`references/candidate_envelope.schema.json`、`multi_region_enabled` flag |
| Phase A 市场与语言 | 已交付 | `references/markets.json`、`scripts/market_plan.py`（`validate_markets()` 即「等价 Python 校验器」）、`report_language` 与 `search_languages` 已拆分 |
| Phase B 来源注册表 | 已交付 | `references/source_seeds.json`、`scripts/source_registry.py`（含 `rollback-legacy`）、旧 `ats_companies.json` 迁移与回滚均有测试 |
| Phase C 双管道单写入器 | 已交付 | `scripts/discovery_plan.py` / `discovery_batch.py`、指标携带 `market_id`/`source_type`/`discovery_route`、旧记录惰性归一化为 `market_status: unknown` |
| Phase D 报告与质量验证 | 已交付 | `docs/multi-region-phase-d1.md` / `-d2.md`、报告模板的市场/来源/验证状态展示、离线 benchmark 测试 |
| **Phase E 发布门禁** | **未达标** | 见 15.1 |
| Phase F 文档与发布 | 已交付 | `WORKFLOW.md`、`SKILL.md`、中英 README、`CHANGELOG.md`、`docs/releases/v2.4.0.md` |

### 15.1 Phase E 的实际缺口

门槛：每市场 3 次成功 shadow run，且跨至少 2 个不同日期。
`data/multi_region_shadow_runs.json` 目前只有 2 次运行（2026-09-18、2026-09-19）：

| 市场 | 成功次数 | 日期数 | live smoke | 还差什么 |
| --- | --- | --- | --- | --- |
| ie | 2 / 3 | 2 ✓ | inconclusive | 1 次成功 run + 一次结论明确的 live smoke |
| uk | 2 / 3 | 2 ✓ | sufficient | 1 次成功 run |
| cn | 1 / 3 | 1 | sufficient | 2 次成功 run，且落在不同日期 |
| de | 1 / 3 | 1 | sufficient | 2 次成功 run，且落在不同日期 |

cn 在首次运行中为 `policy_skip`、de 为 `network_error`，所以两者只攒到 1 次。
`eligible_markets` 为空，四个市场因此全部维持 `off`——门禁按设计生效，缺的是证据而非代码。

### Phase 0：锁定契约、基线和兼容策略

- [ ] 固定 JSON 格式、市场 ID、语言代码、来源数量和 Web Search 总预算；
- [ ] 在实现前提交四市场 fixture、ground truth 和当前单管道基线结果；
- [ ] 冻结 CandidateEnvelope、EvalHandoff、source registry 状态机和 provenance schema；
- [ ] 记录现有 Ireland 搜索、`jobs_table.json`、缓存、报告和 ATS registry 的兼容基线；
- [ ] 定义 feature flag：新地区资源管道默认 shadow/opt-in，旧 Web Search 流程保持可用；
- [ ] 定义回滚：关闭 feature flag 后不删除新 registry，不改变旧 jobs table 的可读性。

### Phase A：市场与语言基础

- [ ] 新增 `markets.json`、schema/validator；
- [ ] 新增 `market_plan.py`；
- [ ] 拆分 `report_language` 与 `search_languages`；
- [ ] 修正 query 地点覆盖规则；
- [ ] 完成四市场路由、语言和地点单元测试。

### Phase B：来源注册表

- [ ] 新增 `source_seeds.json`；
- [ ] 新增运行时 registry 合并/验证命令；
- [ ] 调研并人工验证四市场首批来源；
- [ ] 首次运行时把现有 `ats_companies.json` 原子导入通用 registry，并记录 migration marker；
- [ ] 一个发布周期内保留旧文件只读回退，不删除或覆盖；迁移成功后只写新 registry；
- [ ] 为重复导入、迁移中断、旧文件损坏和回滚增加测试。

### Phase C：双管道与统一合并

- [ ] 地区资源计划与 Agent search 同时启动；
- [ ] 统一 CandidateEnvelope；
- [ ] 所有候选通过现有单写入器；
- [ ] 增加 route/source/market 指标；
- [ ] 旧 jobs table 采用附加字段和惰性归一化：缺少 market/provenance 的旧记录显示 unknown，不要求破坏性重写；
- [ ] 旧评分缓存键保持可读；只有目标市场/候选约束实际变化时才触发重评；
- [ ] 完成双管道合并与中断恢复测试。

### Phase D：报告与质量验证

- [ ] 增加地区/来源/验证状态展示与过滤；
- [ ] 完成四市场固定 fixture 基准；
- [ ] 完成公开来源 live smoke；
- [ ] 输出每市场 evidence boundary；
- [ ] 根据真实 Top-N 贡献决定哪些来源默认启用。

### Phase E：Shadow/opt-in 发布门

- [ ] 每个市场至少完成 3 次成功 shadow run，且覆盖至少两个不同日期；
- [ ] shadow 结果不改变正式报告排序，只记录增量候选、重复交集、JD 覆盖和潜在 Top-N 贡献；
- [ ] 每个市场的确定性代码验收必须 pass；外部 live smoke 若 inconclusive，不能默认开启该市场；
- [ ] 关闭 feature flag 后现有 Ireland/通用 Web Search 行为与基线一致；
- [ ] 达成门槛后逐市场启用，不能一次性全局开启。

### Phase F：文档与正式发布

- [ ] 更新 `WORKFLOW.md` 的市场解析和双管道路由；
- [ ] 更新 `SKILL.md` 的能力映射和降级策略；
- [ ] 更新中英文 README；
- [ ] 更新配置项说明；
- [ ] 增加迁移说明、发布说明和 CHANGELOG；
- [ ] 运行完整 pytest、Ruff、四市场离线 benchmark；
- [ ] 先以 opt-in/shadow 模式发布，不直接替换默认搜索；
- [ ] 达成验收指标后再考虑默认开启地区资源管道。

## 16. 明确不做

- 不自动申请职位；
- 不登录招聘网站；
- 不绕过验证码、付费墙、robots 或访问限制；
- 不维护无法验证的“大公司大全”；
- 不把抓取数量当作推荐质量；
- 不把未收集、网络失败或来源关闭解释为当地没有职位；
- 不为了多地区支持复制四套 merge、评分或报告逻辑；
- 不在共享来源配置和指标中保存 CV、JD、query 或其他 PII；
- 不在缺少 ground truth 时宣称市场级 precision/recall。

## 17. 已锁定的首版实施决策

- 市场、来源种子、角色同义词和 schema 均使用 JSON，与现有配置/运行数据保持一致；
- 产品内部使用稳定市场 ID `ie`、`uk`、`cn`、`de`，UI 显示完整地区名称；
- 首版新增 Provider 不以 Workday adapter 为阻塞项：优先复用现有三种 ATS，Workday 先走公司门户/Web Search/允许的浏览器回退；
- 每个测试市场首批门槛固定为“3 个已验证当地公开入口 + 10 个已验证公司/ATS 来源”；
- 首版继续使用全局 6 次 Web Search 硬上限；确定性来源使用独立预算；预算不足覆盖所有目标市场时要求用户缩小范围；
- 新增通用 `data/source_registry.json`，按 Phase B 迁移旧 `ats_companies.json`，不在原文件上继续扩展地区模型；
- 每市场至少 3 次成功 shadow run、跨至少两个日期后，才允许评估默认开启；
- 任何偏离以上决定的实现必须在 PR/设计说明中记录原因、迁移影响和新验收标准。
