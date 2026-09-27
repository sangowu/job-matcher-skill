# WORKFLOW 规则的理由与实测记录

> 这里放**为什么**，`WORKFLOW.md` 放**做什么**。
> 两边用规则编号对应：`WORKFLOW.md` 里带 `[R4-01]` 的规则，在这里是 `### [R4-01]`。
>
> 这样分是为了成本：`WORKFLOW.md` 每轮都进上下文，而事故复盘、实测数字和被否掉的
> 做法只在改规则或排查时才需要读。拆分前 WORKFLOW.md 有 39,593 字符（约 15k token），
> 每轮固定支出，与职位数无关。本文件的条目是从当时的 WORKFLOW.md **原样搬过来**的，
> 没有重写——规则的措辞可能在 WORKFLOW.md 里被收紧了，但这里保留下判断依据的原话。
> `tests/test_docs.py` 校验两边的编号一一对应，不允许出现孤立条目。


### [O-04] 批间重叠为什么安全

- **批间重叠（有子代理时的推荐模式）**：第 N 批 `merge` 返回 `eval_run` 后，在**同一条消息里**
  同时发出「第 N 批的评估 worker」和「第 N+1 批的搜索 worker」——评估不等下一批搜索，
  搜索也不等上一批评估。快照机制兜底：重叠期间同一职位不会被重复派发（`in_evaluation`），
  JD 输入被搜索改动时 `update` 报 conflict 拒收旧结果，编排者中途死亡留下的超龄快照
  由下一次 `merge` 自动作废回收（`eval_run_stale_hours`，默认 2 小时）。
- `max_parallel_subagents` 是**全局**并发预算（搜索+评估 worker 共用）；
  重叠期建议 1 个搜索 worker、其余给评估（默认 3 → 1 搜 + 2 评）。


### [O-06] 档位下限为什么由 config 声明、型号由运行时自报

- 每次子代理调用前先运行 `subagent_metrics.py profile --role <role> --available-models <你能跑的型号>`。
  **config 只声明档位下限（`min_tier`），不写型号**：哪些型号存在是**你所在运行时的事实**，
  这里没有任何 Python 能观察到它。脚本用 `references/model_tiers.json` 把你自报的型号映射到档位，
  返回满足该角色下限的**最便宜**的一个。"最低档"是逐角色的，不是全局一个型号：
  evaluation worker 的返回结构要过 `analysis_contract` 强校验，把它降到 search worker 的档位
  是让 `rejected_rate` 变差，不是让成本变好。
- 目录里没有的型号**不按拼写猜档位**，只列进 `unresolved_models`；其余候选照常解析，运行不受影响。
  `model_source` 说明结果的来源：`catalog`（按档位选出）、`config`（profile 显式钉死）、
  `unresolved`（可用型号都够不到下限）、`runtime_inherited`（没传 `--available-models`）。
  后两者 `model` 为 `null`，继承当前模型即可，但调用后必须把实际模型/effort 和 `fallback_used`
  如实记录，不能把请求值冒充实际值。


### [S-00] 为什么 stdin 脚本有 30 秒上限




### [R3-01] multi_region_enabled / multi_region_rollout 管的是什么

**`multi_region_enabled` 与 `multi_region_rollout` 只管 Phase E shadow 门禁，不管本流程。**
  读它们的只有 `shadow_gate.py`；`market_plan.py plan` 只在 `compatibility` 里回显，
  `discovery_plan.py` / `discovery_batch.py` 完全不读。所以 `multi_region_enabled: false`
  时多市场计划照样产出并执行——实测 `false` + 四市场 rollout 全 `off` 的仓库默认配置下，
  一份 ie/de/cn 的 market plan 与 discovery plan 正常生成。本流程里决定哪些市场跑的是
  CVProfile/用户意图（第 3 步）与来源目录的资格（Phase B），不是这两个键。把它们当总闸读，
  会以为关着的东西其实在跑。下述 Phase C 双 route handoff 是旧入口，与这两个键同属 shadow 面。


### [R3-04] per_market 通道判定为什么按市场给

- **计划按市场说明每条通道为什么在或不在**：`per_market.<market>.{structured,browser,web_search}` 取
  `planned` / `route_off`（调用方自己的开关）/ `unavailable_in_market`（该市场没有任何合格来源提供这条通道）
  / `deferred`（有来源但没排进已派发的波次）。这就是"按地区自适应选管道"的映射，且由来源目录推导，
  不是另维护一张表：cn 没有任何 `ats_board` 来源，于是它的 `structured` 是 `unavailable_in_market`。
  某个市场三条通道都不是 `planned` 时计划会给出 `no discovery channel is planned for market <id>` 警告。
  把这份 `per_market` 逐市场并入第 6 步 `market_coverage[].channels`，报告才能把"该市场无此通道"
  和"这个市场本轮没有职位"分开显示。


### [R3-06] effective-profile 为什么是必须的一步

- `user_intent.roles` 覆盖 CV 默认角色；没有用户角色时才依次回退 `target_roles`、`preferred_roles`。plan 输出 `roles_source` 说明这三者里是哪一个答的，`target_roles` 是**同义词展开之前**的那份。**取得 plan 之后、把 profile 交给任何 `--profile` 之前，必须用同一份 `{cv_profile,user_intent}` 调 `python scripts/market_plan.py effective-profile`，把它输出的 profile 写盘并从此只传这一份。**它在 profile 上写入 `roles` 键——`job_prefilter.prefilter_jobs` 优先读的就是这个键，那个槽存在就是为了这次覆盖。不做这一步，本轮就有两个「目标角色」的答案：`market_plan` 按用户意图去搜，初筛按 CV 的`preferred_roles` 去筛，于是搜回来的全被以 `role` 丢弃——和 `job_prefilter.py` 统一三通道所修的是同一类毛病，只是高了一层。注意覆盖是**替换**不是追加：要同时看 AI 岗和泛化 SWE，`user_intent.roles` 里两者都要写。地点不做同样处理——plan 的地点是归一化到市场的 id，而`discovery_batch.py` 已用自己的 `market` 原因拒绝越界候选；把 `Dublin` 写进 `locations` 会连 Cork 一起丢掉。


### [R3-07] 角色泛化为什么用技能门槛、为什么永不占第一个名额

- **角色泛化按 CV 技术栈自适应，上限 3 个标题。**`role_taxonomy.json` 的 `generalizes_to` 声明一个角色族可以被读作哪些邻族，每条边带一份 `skills` 门槛：只有 CV 的 `skills` 命中门槛，这条泛化才生效——同样写着 "AI Engineer" 的两份 CV 因此不会被搜成同一份。每个命中的邻族贡献它的首个标题，并从同族变体里占掉一个名额，**但永远不占第一个**："这份 CV 是什么"的答案不能被"它还能是什么"挤掉。命中的邻族标题同时写进 `generalized_roles`，`effective-profile` 把它们并入 `roles`：只放宽搜索不放宽初筛，泛化搜回来的会被以 `role` 原样丢弃。


### [R3-08] search_plan 与 role_plan 为什么拆开

- **`search_plan` 是 Web Search 预算的切片，`role_plan` 是完整展开。**`max_websearch_calls` 只管 Web Search，浏览器通道不花这笔预算；两者共读一份被切到 6 条的列表时，三市场一轮只剩每角色一个标题。现在 `role_plan`（只含 `market_id`/`language`/`role`/`location`，不含 query 串）承载完整展开，`discovery_plan.py` 的浏览器任务读它，Web Search 任务仍读 `search_plan`。浏览器任务的取词顺序**先按来源自己的 `search_languages` 分层、再按 profile 的角色排序**：`target_roles` 只有一种拼写，只按它排会让德语站先搜 `AI Engineer` 而不是 `KI-Ingenieur`。


### [R3-10] 为什么默认不搜 remote

- **默认不搜索 remote 职位。** 地点里带 remote 词的职位在初筛即跳过，无论它同时写了哪个地方。
  原因不是 remote 不好，而是**地点标签读不出它的雇佣资格区域**：`Remote - US` 和 `Remote`
  在标签层面无法区分，而决定性信息（"must reside in the United States"、
  "based in the UK, Ireland, Germany, the Netherlands"）只在 JD 正文里，实测 51% 的
  remote 职位这样写，且限定粒度细到美国州一级。JD 正文按本流程约束不进主 agent 上下文，
  所以这件事要做对需要改数据流，不是补几个别名。期望与验收标准见
  [`docs/roadmap.md`](docs/roadmap.md)。


### [R4-01] 通道顺序的实测依据

- **通道顺序按实测产出排**：结构化（公开 ATS API，单 board 一次请求、约 0.2–0.6 秒、自带 JD）独占第一个波次；浏览器由 `browser_first_wave`（默认 2）推迟；Web Search 由 `web_first_wave`（默认 3）排在最后。Web Search 原本与结构化同处首波，但实测到 2026-09-26 的六轮里：22 次调用、204 条原始结果、2 个新候选，最近五轮为 0；当前表 54 行里只有 3 行来自它。它留在计划里是因为运营方禁止自动化的来源只能由它触达，但不再用第一个波次去找不到东西。浏览器单任务是分钟级，且会在登录、模糊 consent 和自定义 combobox 上失败，因此它是兜底通道而不是主力。第一波产出已经足够时，既有的波次门禁根本不会放出浏览器任务。`browser_first_wave: 1` 可恢复三通道同时起跑的旧行为。


### [R4-02] 来源积累为什么只在本机、seed_promotion 做什么

- `data/source_registry.json` 在 `.gitignore` 内（与 CV、职位表、报告同目录，按 PII 规则整体屏蔽），所以 `board_harvest.py` 的积累只存在于本机：换机器或重装就清零，别的用户也享受不到。board token 是公开信息、不含 PII，只是被那条规则连坐。需要把积累固化进仓库时运行 `seed_promotion.py`：它只提升 `origin=agent`、`status=verified`、未过期，且能从 provider 身份确定性重建 `entry_url` 的来源（即公开 ATS board）；注册表按设计不存 URL，URL 无法重建的来源只记计数、不提升。提升是所有权转移——写入种子后本机记录的 origin 改为 `seed`，否则下一次 `merge_seeds()` 会因撞号报错。先用 `--dry-run` 查看将要提升的计数，再实际写入并按正常流程提 PR。


### [R4-03] board_harvest 如何从候选 URL 反推 board

- 一轮候选合并之后，可以用 `board_harvest.py --candidates <candidates.json>` 从候选 URL 反推公开 ATS board：Ashby/Greenhouse/Lever 的职位 URL 本身带着该公司 board 标识，一个职位即可换来整家公司的后续拉取。脚本只读候选的 `url` 字段，绝不把 URL、职位名、JD 或 CV 写入注册表。每个新 board 必须实拉复验一次才标 `verified`，市场归属由实际职位地点决定，不按公司总部推断；未应答、无职位或在受支持市场没有职位的 board 只记计数，不入库。单次运行的复验请求受 `--limit` 上限约束（默认 5），其余 board 留待下一轮。公司把 board 嵌进自家招聘页时，URL 里既没有厂商域名也没有 board token，只有 provider 和 job id（`gh_jid` / `ashby_jid`）：此时 token 由主机名猜出，再用那个 job id 去猜出的 board 上验证——**board 有应答不算数，必须在它返回的职位里找到这个 job id**，否则会把别家公司的 board 记成这家的（建目录时撞到过一次）。猜测每个都要一次请求，所以受独立的 `--hint-limit` 约束（默认 3）。手工策展只负责冷启动，目录靠这条路径增长。


### [R4-06] 波次提交与三通道统一初筛

- 当前波次每个 task 必须恰好回报一次 `succeeded`、`failed` 或 `skipped`。`failed` 必须使用低基数 `failure_kind`，失败/跳过任务的候选必须为空。`necessary_only` 下，只有当前 consent dialog 内唯一且由 `cookie_consent.py` 明确分类的 button 可以自动点击；`ask_every_time`、零/多匹配、登录、CAPTCHA、限流或其他需判断 consent 都是暂停状态，处理或明确放弃前不得提交整批。把原 DiscoveryPlan、`wave_id`、该波次**非 structured** 的 task results、可选来源更新以及 count-only progress 一次性交给 `discovery_batch.py`；**任何携带候选的批次都必须传 `--profile <cv-profile.json>`**，不只是含 structured 任务的波次：确定性初筛（`job_prefilter.py`）现在对三个通道一视同仁。此前这条规则只写在 `ats_pipeline` 里，browser 与 Web Search 只回报一个 `candidates_prefiltered` 数字，而这里唯一的校验是漏斗不得变宽——于是同一轮跑着两套规则，严格的 worker 和宽松的 worker 在此处无法分辨。现在 worker 自报数与本地规则保留下来的条数**并列上报**，差距大说明 worker 对规则的读法不同，值得看见而不是抹平；被丢弃的条数按 `role`/`location`/`seniority`/`market` 低基数原因记入 `candidates_dropped`。它先预校验 source batch 和每条 CandidateEnvelope 与所属 task 的 route/source/market/language，再用目录**重新推导**市场而不是采信 `location_normalized.market_id`——那个字段来自 worker，而 `Dublin, OH` 正是目录存在的理由：目录能定位时以目录为准（含 `location_type: "foreign"`），目录定位不了时保持沉默、以 worker 的读法为准，因为沉默不是反驳。再拉取本波次计划的 ATS board（只拉这些，不碰后续波次的）并同样校验，最后把全部通道候选合并为一次 `merge_jobs.py merge`，再提交 source registry；ATS 的 JD 正文只随候选进入 merge 子进程，不进标准输出、不进 manifest；重复 `batch_id` 同输入为 no-op，不同输入拒绝。


### [R4-11] consent 空壳为什么要看 visible

- **consent 容器必须带 `visible`**：站点在用户早已同意后，常在 DOM 里留下一个**不可见且没有任何按钮**的 `role=dialog` 空壳（2026-09-22 在一个公共部门站实测到，其 `aria-label` 还叫 `Cookie consent button`）。把空壳当成普通 consent dialog 送进分类器会得到零匹配 → `pause`，于是运行停在一个没人看得见的横幅上。分类器现在先看 `visible`：未显示则返回 `proceed`，不做任何点击。可见性只用于**决定是否停下**，绝不用于放宽点击边界——可见的空壳依然 `pause`，`ask_every_time` 依然优先。


### [R4-12] 站点兼容性与指标边界

- **站点兼容性与指标边界**：招聘站跳转到 task `ats_handoff_hosts` 列出的公开 ATS host 时，这不是越界，而是暴露了该公司的 board：按 `on_ats_handoff: record_board_then_stop` 停止在该站继续浏览，把跳转地址原样交给 `python scripts/board_harvest.py --candidates <urls.json>`（它能识别 board 根页，不只是职位详情页），任务回报 `succeeded` 且候选为空，该公司下一轮由结构化通道一次请求拉完。**来源的职位列表若不在自己的域名下，由目录的 `listing_hosts` 声明**，它会并入 task 的 `allowed_hosts`：publicjobs.ie 自己只放门户页，职位全在 `publicjobs.tal.net`，边界按 entry_url 的 host 推导就把唯一有职位的那一页拒了（2026-09-25 实测 `host_boundary` 失败）。放宽写在目录里、在计划里可见，不由浏览的人临时决定。**站点自己写明的节流按它的来**：目录的 `min_interval_ms` 记录该来源的最小间隔，`browser_control.py` 从目录读，取它与全局下限中较慢的一个——`publicjobs.tal.net` 的 robots.txt 写着 `Crawl-delay: 10`，按全局 5 秒读就是它书面要求速率的两倍。只能更慢，来源不能要求被读得更快。跳转到其它不在允许边界内的 host 仍记录 `host_boundary` 并跳过，不临时放宽；自定义 combobox 在 accessibility act 后没有可验证变化时记录 `search_control_unresponsive`，不得用页面脚本或硬编码 selector 绕过。当前 Agent 直接通过 BrowserOS Neo MCP 或授权用户浏览器执行时，本进程观察不到这些动作，必须由 Agent 用 `browser_control.py --provider browseros_neo|user_browser --metrics-run-id R action --action navigate|read|snapshot|act|extract|wait|create|close --status ok|failed|timeout|user_action_required|rate_limited|resumed [--timing measured|unavailable] [--duration-ms N] [--links-found N]` 逐个自报，否则 `round_timer.py` 仍会报告 `missing_operations=browser`。`action` 是纯指标路径：不需要凭据、不占会话预算，只写 provider、动作、结果与计数，不得写 URL、页面正文、会话 ID 或输入。**试跑一律带 `--data-dir <临时目录>`**，它把本次调用的 metrics、轮次预算、来源限速状态一起挪到该目录；不带它的一次试探性调用会直接落进生产 metrics 与生产限速状态，事后与真实轮次无法区分。只有**做成了事**的动作计入完整性：`status=ok` 才算。`failed`/`timeout` 不算——失败上报不能把坏掉的浏览器 route 伪装成已埋点；`user_action_required`/`rate_limited`/`resumed` 同样不算，它们报的是路线的生命周期而不是产出，一轮里每个浏览器任务都停在登录墙前也是什么都没拿到。这三个状态仍写成 `ok=True`——停在登录墙前是正确行为，不是故障，不进失败计数。`browser_control.py event`（写 `action=state`）只驱动本地面板，同样不计入完整性：报告自己在等不是干活。仍缺 `browser` 时 HTML 健康状态必须保持 `unknown`，不得把发现/merge 成功改写成指标完整。实测边界见 `docs/browseros-neo-production-trial-2026-09-22.md`。


### [R4-21] Web Search 页级计数为什么成为提交前提

- **Web Search 的页级计数随 task result 一起交给 `discovery_batch.py`，不再单独调用指标脚本。**每个 succeeded 的 `web_search` task result 必须带 `pages`：每个结果页一条，含 `page_number`、`calls`、`raw_results`、`prefiltered`、`deduplicated`、`new_candidates`、`cached_candidates`、`duration_ms`（`first_result_ms` 可选）。缺 `pages` 的 Web Search 结果**无法提交候选**——这是刻意的：指标漏记曾经零代价（候选照常入表，只是本轮 `missing_operations=search`），现在漏记在结构上不成立。`discovery_batch.py` 校验后按 `query_slot`（由 task_id `web:N` 推出 `qN`）逐页写 `search` 事件；页级计数必须满足漏斗关系，且各页 `raw_results` / `prefiltered` 之和必须等于该 task 的 `candidates_raw` / `candidates_prefiltered`，对不上直接拒绝。不得把 query、hash、职位或 URL 放进 `pages`。


### [R4-22] 测不出时间就说测不出

- **测不出时间就说测不出，不要编。**你通过工具调用执行 Web Search，手上没有能围住这次调用的钟——两次 Bash 取时间戳之间隔着你自己生成 token 的时间，测出来的是回合耗时而不是搜索延迟（实测 11.6s vs 搜索本身约 1–3s）。这种页写 `"timing": "unavailable"`，同时把 `duration_ms` 和 `first_result_ms` 都显式置 `null`；默认是 `"timing": "measured"`，此时 `duration_ms` 仍必须是数字。两条约束保证它不是后门：**计数不跟着放宽**（漏斗与求和照常校验，`raw_results` 这些本来就数得出来），并且**声明会被记下来**——事件带 `timing`，批次结果带 `search_pages_untimed`，汇总里 `search.duration_ms.reported_rate` 让"整轮没计时"和"整轮没搜索"不再长得一样。没有任何机制能分辨"测不了"和"懒得测"，这里要的只是：不再**逼**你二选一地撒谎。


### [R5-05] 链接复验的时间戳与状态码语义

- **只查任务里 `needs_verification: true` 的行。**快照任务带 `verified` / `verified_at` / `needs_verification`：`alive` 且 `verified_at` 在 `verify_ttl_hours`（默认 24）内的行不再复验——这个判断由 `merge_jobs.py` 做，不是靠你记住。此前没有时间戳，于是每轮把整个 Top-N 重查一遍（20 条 URL，每条超时上限 10 秒），报告里一个月前的 `alive` 和刚查的 `alive` 长得一样。`verify_jobs.py` 现在按 host 分组：不同站点并发（上限 8），**同一站点仍然串行**，请求总数不变。回传时 `verified` 只能是 `alive`/`closed`/`unverified`/`unknown` 或 `null`（布尔仍被接受，存盘统一成枚举）；`null` 表示这轮没查，脚本会保留上一次的结论和时间戳，不会把没做过的检查盖上新时间。JD 正文 hash 变化会同时清掉旧结论与旧时间戳。状态码只回答职位还在不在：404/410 是没了；403/429/5xx 是服务器拒绝或伺候不了**这个客户端**，与职位死活无关，一律 `null`。两者判错的代价不对称——误判成失效会删掉真实职位，而误判成无法判定只是少一条证据。反爬站点因此永远拿不到 `false`，这是对的：普通 HTTP 客户端确实无法判定，要确认就走容错阶梯的下一层（浏览器）。2026-09-25 实测：irishjobs.ie 对 `verify_jobs.py` 的 UA 回 403，同一条职位在浏览器里正常打开、标题完整、无关闭字样。


### [R5-07] 评分契约为什么要读代码而不是凭记忆

- **结果形状以 `scripts/analysis_contract.py` 为准，写 worker 提示前先读它，不凭记忆编示例**（2026-09-26 实测：提示里给错了键名，8 条结果整批被拒，`rejected_rate` 直接顶破阈值）。顶层**只**允许上一条列的八个字段，多一个就整条拒收。`match_score` 里五个维度**平铺**，不是嵌套对象：`overall_score`、`title_score`、`skills_score`、`must_have_score`、`seniority_score`、`location_score`，权重 .25/.25/.25/.15/.10，`overall_score` 与加权和的误差必须 ≤ 0.2；再加 `recommendation`，取值 `strong_apply`/`apply`/`stretch_apply`/`low_priority`/`skip`，**只能等于或低于分数对应档**（≥85/≥70/≥60/≥20，以下为 `skip`）。`scored_from` 只接受 `jd` 和 `snippet`；`to_score_only` 复用的 `jd_profile` 也来自 JD，所以仍写 `jd`，`jd_profile` 为空时拒收。`jd_profile` 被校验的字段名是 `must_have`、`good_to_have`、`required_skills`、`years_required`、`work_mode`（`remote`/`onsite`/`hybrid`）、`job_type`；多出的键不校验但会原样存进主表，别把实质内容放在那里。


### [R6-03] Phase D2 smoke 的边界

- **Phase D2 仅显式 smoke**：需要来源诊断时才运行
  `python scripts/multi_region_smoke.py --live --output <count-only.json>`。计划固定在
  `references/multi_region_smoke_plan.json`；每来源最多两次同站 HTTPS GET，单响应 512 KiB、
  8 秒超时、最多两次重定向。登录/验证码立即停止，`automation_allowed=false` 的 China 来源
  必须零请求并记录 `skipped_policy`。产物只能保留状态、失败类别和计数，不得保留公司、标题、
  URL、query、页面或 JD 正文；外部失败不得当作 pytest 回归。不得把本次小样本解释为市场召回率，
  也不得据此默认启用来源。


### [R6-04] Phase E shadow 门禁

- **Phase E shadow 门**：shadow 编排不得把地区来源候选写入正式报告或改变排序；新运行使用
  `references/shadow_compare_v2.schema.json` 临时输入，经 `shadow_compare.py` 在内存按强身份合并并
  计算 route 新增、交集、JD/链接覆盖和潜在 Top-N。正式 baseline 在同分时优先，跨 route 新职位
  由 `regional_registry` 优先归因；输出不得含身份键或业务正文。只有这个 count-only 输出才能交给
  `shadow_gate.py record`。`status` 按市场要求
  至少 3 次成功 run、跨 2 个 UTC 日期、确定性验收通过、双 route 成功且 live smoke 为
  `sufficient`；还必须有至少 3 次跨 2 日的 v2 已完成非空同 CV/市场旧流程 Top-N 基线，
  以及至少 1 个同时具备可用 JD 和有效链接的增量候选。v1 历史记录可读但不能满足新门槛；
  未执行基线必须写 `unavailable`，不能把空数组冒充完成。`preliminary/inconclusive` 一律阻止 `default`。`multi_region_rollout` 只能逐市场设为
  `off/shadow/opt_in/default`；总开关为 false 时有效模式全部为 off，未过门禁的 default 配置无效。
  不得用离线 fixture 或同一天重复执行冒充真实 shadow 覆盖。


### [R6-05] 同名重复发布为什么标注而不合并

- 同一家公司用**完全相同的职位名**发布的多条职位会互相标注（列表页加「重复发布」角标，详情页列出其余发布的地点与链接），但**不合并**：它们各有独立的 `gh_jid` 与投递链接，投其中一条不等于投另一条，删掉任何一条都会删掉一条真实的入口。判定用的是公司名 + 原样职位名（只规范空白与大小写），**不是 `dedup_key`**——后者是为 merge 设计的弱键，`normalize_title` 会去掉括号内容和破折号后缀，Intercom 的「Senior Data Scientist - AI Tooling / - Growth /（GTM）」三条会塌成同一个键，而那是三份不同的工作。


### [R6-07] 浏览器通道结果依赖登录态

- **浏览器通道读到的结果依赖登录态**。复用使用者自己已登录的浏览器是设计本身——这正是不模拟登录就能读到内容的办法，但代价是那份列表不是别人会看到的列表：2026-09-25 实测 irishjobs.ie 返回 `searchOrigin=membersarea`，LinkedIn/Indeed 同样处于登录态。计划里每个浏览器 task 带 `reproducibility: session_dependent`，报告对这些行标「登录态结果」并在顶部给出条数。不标出来的后果是：两轮因账号不同看到不同列表，会被读成职位在减少。


### [R7-01] finish 为什么要显式声明 --expect search

- `python scripts/round_timer.py finish --round-id <R> --orchestration overlapped|serial --batches N --evaluations N --jobs-reported N [--expect search] [--expect subagent] [--expect ats] [--expect browser]`
  —— `overlapped` 表示本轮真的把「第 N 批评估」和「第 N+1 批搜索」并行发出过，否则填 `serial`。只为本轮实际使用的可选管道追加 `--expect`。**`search` 现在也要显式声明**：读本轮 DiscoveryPlan 的 waves，只要**实际派发过**的某个 wave 里有 web_search task 就加 `--expect search`；计划把 Web Search 放在第 `web_first_wave` 波，而一轮可能在那之前就 `target_reached` 停下，那时它没搜过也不该被判为缺事件——2026-09-27 实测一轮在 wave 2 停下、27 个来源全部成功，却因为写死的期望被报成 `incomplete / missing_operations: search`。默认检查 `run_start/merge/round`，有评估时自动检查 `update`；缺事件时返回 `metrics_status: incomplete`，健康状态只能是 `unknown`。
  如实填写：这是唯一能实测重叠编排收益的数据来源，填错会让对比失去意义。


### [R7-02] 中断的一轮为什么用 abandon 而不是 finish

- 一轮如果被中断、永远不会走到 `finish`，用 `python scripts/round_timer.py abandon --round-id <R> --reason interrupted|superseded|rate_limited|operator_stopped|unknown` 将其收掉。**不要改用 `finish` 冒充**：`finish` 要求 `data/rounds/<R>.json` marker 存在，而被中断的一轮恰好是 marker 已丢的情形；它写的 `run_finish` 也等于声称这一轮上报过。`abandon` 只能用于已有 `run_start` 且尚未收掉的轮次，`reason` 是封闭集合（保持低基数，不接自由文本）。被收掉的轮次不再计作 `stale_unfinished`，也不计作`complete`，而是单独计入 `runs.abandoned` 并在健康报表里单行显示——一轮什么都没产出是读报告的人应该看到的事。


### [A-01] ATS 增强协议（完整）

仓库默认 `ats_enabled: true`：公开 ATS board 是成本最低的发现通道（实测单 board 一次请求约 0.2–0.6 秒即可取回全量职位与 JD），且受独立硬上限约束；关闭它是用户/本地配置选择。结构化通道不只有 ATS：`public_read_only_endpoint` 同样走这条路，`amazon-jobs-ie` 用 `provider: amazon_jobs` + `board_token: IRL`（ISO-3 国家码）读 amazon.jobs 的 `search.json`。**不带 `base_query`**——board 是整份拉下来本地初筛，这才使结果可复现、也省掉写查询词；只给这一个来源发搜索词会让它的口径与其它来源不同且不可重复，国家码是“读哪一份列表”，不是搜索词。实测 2026-09-26：3 次请求、1.5 秒、380 KB，取回 208 条爱尔兰职位且每条都带完整 JD。`ats_pipeline.py` 只允许官方公开 HTTPS GET，不需要 API key，不调用申请、Harvest、Hire 或 Partner API。客户端默认请求 gzip；压缩响应的 wire bytes 与解压后 payload 都必须独立受 25 MB 上限约束，未知或损坏的编码按该 board 的安全失败处理。Greenhouse 标识发现同时接受 `job-boards.greenhouse.io` 与 `job-boards.eu.greenhouse.io` 的公开职位页，但两者都调用官方 `boards-api.greenhouse.io` 公共 Job Board API；不要虚构 EU API host。它在内存中规范化并按 CV 的 title/location/seniority 做确定性初筛（remote 职位一律跳过，见第 3 步）：单独的 `AI` 产品或团队后缀是低信息量 token，不能独立触发岗位匹配；`AI evaluation`、`AI systems`、`agent systems` 等明确岗位短语仍可匹配。角色初筛之后再按**计划的市场**筛一道：`markets_by_board` 由 `discovery_batch.py` 从每个 structured task 的 `markets` 传入，只保留市场目录能归入该市场的职位。市场范围只有计划知道：`prefilter_jobs` 的地点条件读的是 CV profile，而 `extract_cv.py` 对这类 CV 常把 `target_locations` 归入 `missing`，空列表被读成“不限地点”（2026-09-26 实测：134 条符合角色的职位里 120 条不在爱尔兰，占满名额又在校验处整批报错）。目录无法归属市场的地点一律剔除，剔除数记为 `jobs_out_of_market`；**同名城市按 `markets.json` 的 `foreign_administrative_areas` 判否**：目录里没有美国/加拿大市场，`Dublin, OH`、`Dublin, CA`、`Berlin, CT`、`London, ON` 因此全部以 `confidence: exact` 归入 ie/de/uk——城市别名无人制衡。该字段是一份**封闭的反向目录**（50 州 + 13 省 + DC，集合本身不变化）：漏一条只是保留今天的行为，不会产生新的错误答案，这与 v2.4.0 移除 remote 作用域目录的风险方向相反（那里默认值是自信的 “global”）。两字母代码只在**独立的逗号分段**里算数——`IN` 在 `, IN` 里是印第安纳，在 `Hybrid work in Dublin` 里是英文词；尾随邮编先剔除。只有当文本没有点名任何受支持市场时该判定才生效：`Berlin, DE` 的 `DE` 同时是特拉华和德国，`United Kingdom; Dublin; United States; New York` 是真正的多国职位——有佐证就以佐证为准。420 条真实地点串回归零变化。被拉取了却没有计划市场的 board 是错误，不是“无范围”。候选的 `source_type`/`discovery_route` 按目录里该来源的真实类型写（`ats_board`→`ats_expansion`，`company_careers`→`company_careers`），不得统一写成 ATS——`discovery_batch.py` 按 task 的目录类型比对，一条不符整批作废。最多输出 `top_n + precise_buffer` 个候选，再进入统一强身份 merge。**`ats_defer_jd`（默认 true）把正文推到这个名额之后**：Greenhouse 的列表先按 `content=false` 读一遍，只有本轮真正保留的候选各花一次请求取自己的正文。board 是整份拉下来本地初筛，正文也就整份付费——2026-09-26 实测下载了 12,561 份正文、入库 54 条候选，其中 99.6% 下载完即丢弃。一个 GitLab board 带正文 364,941 字节、不带 11,330 字节，而列表里的 title/location/id/URL 正是初筛和身份键需要的全部字段。8 个真实爱尔兰 board 的 A/B（2026-09-27）：2,728,022 → 149,965 字节（-94.5%），请求 8 → 14，候选集合与每条 JD 长度完全一致。正文取不到的候选照常入库、`jd_text` 留空，由评估 worker 的容错阶梯自己读页面——它已经被这一轮选中，丢掉它比慢一点更贵；请求预算耗尽同样是停止补正文而不是判该 board 失败。Ashby、Lever、`amazon_jobs` 只提供一份自带正文的列表，没有“少要一点”的开关，它们忽略该开关保持原样。第二趟的请求数与失败数记为 `jd_requests` / `jd_fetch_failed` / `jd_fetch_skipped`，不并进 `requests` 的总数——“这一轮为多少份正文付了费”是延迟正文要回答的问题，总数答不了。可用正文会清洗为纯文本并截断到 50,000 字符，随后只经本地评估快照临时交给 worker；主表只留 hash，状态/指标/benchmark 报告只留计数。若 Greenhouse `content=true` 响应超过 25 MB，可在同一全局请求预算内额外重试一次不含正文的列表；该 board 的任务继续走网页抓取回退。记录的 `response_bytes` 是网络传输字节数；另记录正文交接计数与 `content_fallback`，预算不足则按失败降级。通用 `data/source_registry.json` 存在时，`ats_pipeline.py` 只写该文件，旧 `data/ats_companies.json` 保持只读；通用 registry 不存在时才回退旧文件。`data/ats_sync_state.json` 和 `ats` 指标只保存低基数状态/计数，不保存职位名、URL、JD、CV、token 或异常全文。连续三次 404/410 才标记 unavailable；429、超时和网络失败保留可重试状态。`benchmark_ats.py` 复用同一生产解析器做公开小样本回归，但其脱敏报告不进入职位主表；`benchmark_ats_e2e.py` 只在显式提供固定 Web 候选与本地 profile 时做受限 discovery-to-merge A/B，仍不得突破生产硬上限。


### [G-01] robots 为什么不能用 urllib.robotparser

- 抓取**不绕验证码、不模拟登录、不抓需付费/登录内容、尊重 robots/ToS**。robots 用 `python scripts/check_robots.py --url <URL>` 查,**不要用 `urllib.robotparser`**:它按文件顺序取第一条命中,而 RFC 9309 §2.2.2 规定取最长路径命中(同长度 Allow 胜)。Microsoft 招聘站写的是 `Disallow: /` 后跟 `Allow: /careers`——标准库读成全禁,实际是开放 `/careers`,照标准库会拒掉运营方明确开放的来源。**查的必须是真正要访问的 URL**:Accenture 允许 `/careers/jobsearch`、禁止 `/careers/jobsearch?`,只查落地页会把它判成可用。robots.txt 返回 404 表示没发布规则(不是默许、也不是拒绝);返回 403/429/5xx 是查不出来,记 `null`,不当作允许。十个 `company_careers` 来源的实测见 `docs/company-careers-robots-2026-09-26.md`。


### [G-02] robots/ToS 的唯一例外需要两把钥匙

- **robots/ToS 的例外只有一条,且需要两把钥匙。**运营方明确禁止自动化访问的来源(如 LinkedIn 的 `User-agent: * / Disallow: /`、Indeed 条款点名 "bots, scrapers, spiders, AI or Agentic AI"),在 `source_seeds.json` 里标 `automation_allowed: false` + `requires_risk_ack: true`,并在 `constraints` 写明原因。这类来源**只有**在使用者把它的 `source_id` 写进自己的 `data/browser_provider.json` 的 `risk_acknowledged_sources` 后才会进入计划 —— 目录记录"该来源拒绝自动化",本地设置记录"本机仍然照做",两者缺一都不启用,而 `data/` 不入版本库,所以任何提交都无法替别人打开它。计划输出的 `risk_accepted_sources` 必须在波次执行前向使用者展示。`discovery_plan.py` 读取 `source_plan.risk_accepted_sources` 决定这类来源能否进入浏览器通道:只认目录标了 `requires_risk_ack` **且**本机确认过的来源——本机单方面点名打不开任何来源,`public_read_only_page` 的要求也不因确认而放宽。生成的任务带 `requires_risk_ack` 与该来源的 `constraints`,执行者据此知道自己正在动的是哪一类来源。**前四条禁令不在例外范围内**:仍然不绕验证码、不模拟登录、不伪装 IP/User-Agent、不规避检测——这些既是运营方条款明文禁止的,也与"读取已授权会话看得到的内容"是两回事。


### [G-03] 限速只加在真正发出请求的动作上

- **限速只加在真正发出请求的动作上。**`create` / `navigate` / `act` / `extract` 计入,`read` / `snapshot` / `wait` / `close` 不计入,也不推进该来源的时间戳——让一次本地读取推进时间戳,下一次真实请求就会按一个「从没发出过任何请求的动作」起算的间隔白等。2026-09-25 在 Indeed 结果页实测(数 `performance.getEntriesByType('resource')` 里该来源的条目):连续两次 snapshot 加两次 read 共 102ms、**0 个请求**,页面空闲 4 秒同样 0 个,而一次卡片点击产生 **14 个请求**。按动作全部限速时,一页 25 条职位里 95% 的时间花在等待上。执行前用 `browser_control.py pace --source-id S --action A` 问,本地观察会直接返回 `wait_ms: 0`。


### [G-04] 一次点击不等于一个请求

- **一次点击不等于一个请求,按请求计量。**间隔数的是动作,站点数的是请求,两者差一个数量级:实测一次点击 14 个请求,5s/动作的真实速率约 2.8 req/s,不是看上去的 0.2 req/s。因此另有一条以站点单位表达的上限 `browser_max_requests_per_minute`(默认 120,**只能调低不能调高**),按滚动 60 秒窗口计。请求数在页面里量:动作前后各数一次同源 `performance.getEntriesByType('resource')` 条目,差值就是这个数,上报时传 `--requests N`;问等待时也传,`pace` 会把间隔还差多久与预算还差多久取较大值返回。不传时按 `browser_assumed_requests_per_action`(默认 10,**只能调高不能调低**)计费——默认值是这样选的:120 / 10 = 每分钟 12 个动作 = 每 5000ms 一个,两条上限同时到顶,所以不量不会变慢,量了只会更准。超预算的动作记为 `failure_kind=browser_request_budget_exceeded`,与超速同样不计入轮次完整性;两者分开命名,因为超预算的那些每一个都守住了间隔。


### [G-05] 限速对所有浏览器来源生效

- **限速对所有浏览器来源生效。**同一 `source_id` 的两个动作间隔不得低于 `browser_min_source_interval_ms`(默认 5000,只能调高不能调低)。动作前先问 `python scripts/browser_control.py --provider P pace --source-id S --action A --requests N` 拿到应等毫秒数;上报时传 `--source-id S --requests N`。**批量上报时必须带 `--occurred-at-ms`**(动作发生的 epoch 毫秒)——间隔按动作发生时刻判定,不按上报时刻;不带它等于声称动作就发生在此刻,真实间隔 5 秒的一串动作会被误判为超速,而真实间隔 0.1 秒、拖延上报的一串会被误判为合规。**确实没量到时刻的动作传 `--timing unavailable`**,不要让它默认成此刻——默认成此刻等于声称它发生在那些其实更晚的动作之后,一条没量到的时刻会把整批的时间线弄乱(2026-09-25 实测产生过一条假的 `browser_paced_too_fast`)。这样的动作不参与限速判定,也不推进该来源的时间戳(判一个猜测已经不对,把猜测存下来更糟:之后每个动作都会拿它当基准),但仍然算作浏览器已产出,因为页面确实取到了,缺的只是秒表。它与 `--occurred-at-ms` 互斥——有时刻就报时刻,没有就说没有,不能既给数又不认。未计时动作免于限速,所以 `metrics.browsers.untimed` 会计数,跟 `search_pages_untimed` 同理:看不见的豁免没人会去审。间隔不足的动作仍会写入事件(保持可见),但记为 `failure_kind=browser_paced_too_fast` 且**不计入轮次完整性**——本进程拦不住 Agent 的浏览器调用,能做的是让超速有代价。`browser_jitter_ms`(默认 2000)在最小间隔**之上**叠加随机等待,只会让等待变长,强制下限保持确定。它的用途是分散请求、降低对被读取站点的瞬时负载,**不是用来伪装流量**:检测机制看的是 TLS 与浏览器指纹,不是两次页面加载的时间间隔。

### [R4-07] 被拒候选的标记为什么不是缓存、也不省 token

需求要的是「已过滤的 JD 要有标记，防止重复分析」。做之前先确认了一件事：**这个标记省不了 token**。
被初筛拒掉的候选**根本没进 merge**——worker 上报它的时候，那份 token 已经花掉了；而 worker
下一轮还会把同一条读回来，因为「上次读到第几页、哪些 id 看过」不在任务里。真要省那部分，得在
任务层按来源记住分页位置，那是另一件事，不是负缓存能救的。结构化通道更不需要：board 整份拉下来
本地筛，`ats_defer_jd` 只给留下来的候选取正文，被拒的一条请求都不花。

所以它做成标记和可见性，不做成缓存：

- **可见性**：`candidates_dropped` 只说本轮按原因丢了几条。同一轮丢 10 条新的，和把上轮那 10 条
  重新丢一遍，报出来一模一样——而这正是「浏览器通道花了几分钟什么也没拿到」要回答的问题。
  `candidates_dropped_repeat` 把两者分开。浏览器每轮重读同一个站的列表，所以这个数主要反映
  browser / Web Search 两条通道在多大程度上重复劳动，也是调 `browser_first_wave` 或某来源
  priority 的依据。
- **不做成缓存是硬约束**：以 `role` 拒掉一条是「这份 profile 拒它」，不是永久判决。CV 通过
  `generalizes_to` 拿到 `Backend Engineer` 之后，上一轮以 `role` 被拒的后端职位必须被重新考虑。
  一个被当作缓存查的 store 会悄悄保留旧答案，把泛化那件事撤销掉——所以初筛对每条候选每轮照跑，
  这个 store 的答案只改变计数。测试钉住了这一点。
- 存的东西：url key + `role`/`location`/`seniority`/`market` 四个原因之一 + 时间戳 + 次数。
  没有标题、公司、JD、CV、query。TTL 与 `jd_ttl_days` 一致（同一个"这条信息还算新吗"的尺度）。
- 标记写失败只报 `refusals_recorded:false`，不影响候选：一个诊断不该有权让一批候选失败。

### [R3-09] 为什么角色词表必须只有一份

改之前有两份：`references/role_taxonomy.json`（`market_plan` 用来展开 query，4 个族）和
`job_prefilter._ROLE_FAMILIES`（初筛用来认标题，7 个族）。**一份决定一轮去搜什么，另一份决定一轮
留下什么**，而且族 id 都不一致（`data` 对 `data_engineering`）——任一边改动都不会让另一边知道。
这与 WORKFLOW 里记着的「一轮有两个目标角色的答案」是同一类缺陷，只是低一层。

合并后：

- `match_terms` 是短语，按子串匹配；`match_tokens` 按整词匹配，所以不能写成短语——`ai` 作为子串
  藏在 `maintenance` 和 `training` 里，而 `AI Platform Engineer` 需要按整词认出那个 `ai`。校验会
  拒掉写成短语的 token。
- `match_only_families` 只认标题、不生成 query：这个技能不搜产品经理，但一条标着产品经理的职位
  必须以正确的原因被拒，而不是碰巧被拒。这样既统一了词表，又不会让 `resolve_role_family` 把
  `AI Platform Engineer` 解析成 platform 去搜错的词。
- 词表读不出来时**抛错而不是回退到空**：空词表会让初筛只剩 token 重合，等于悄悄放宽；一个静默
  放宽的过滤器比一轮直接停下更糟。
- 顺带补上需求 4 的例子：`backend` 的 `match_terms` 现在包含 `python engineer` / `python developer`
  这类以技术栈命名的标题。此前泛化生成了 `Backend Engineer` 的查询，搜回来的
  `Senior Python Engineer, Platform` 却因为"不属于任何族"被初筛以 `role` 丢掉——搜索付了钱，
  过滤器把结果扔了。短语匹配保证 `Python Trainer` 仍然被拒。

### [R3-11] 加一个市场为什么要动「境外行政区」目录

`foreign_administrative_areas` 是一份**封闭的反向目录**：50 个州 + 13 个省 + DC，存在的理由是
「这些地方没有任何市场覆盖」。`Dublin, OH`、`Dublin, CA`、`Berlin, CT`、`London, ON` 原本全部以
`confidence: exact` 归入 ie/de/uk，因为目录里没有美洲市场，限定词没有东西可比对，城市别名独赢。

加 us 市场之后，这份目录的前提对一半条目不再成立：俄亥俄不再是「别处」，而是一个轮次可以被限定到的
地方。三种做法里只有一种是对的：

- **留在境外目录**：us 市场存在、城市登记了，`Dublin, OH` 仍被判境外 → 该市场的职位被静默丢掉。
  而且 #87 加的那条校验（市场自己的城市不得出现在境外目录里）会直接拒绝这个配置——"New York"
  既是州名也是城市名。这条校验就是为了在这里报错。
- **从境外目录删掉、不做别的**：`Dublin, OH` 的 `OH` 没人认领，城市别名 `dublin` 独赢 → 归 ie。
  正是这份目录当初要防的错误答案。
- **移交给 us 市场**（采用）：50 州 + DC 成为 us 的 `administrative_areas`，13 个加拿大省留在境外目录。
  限定词现在回答的是「归哪个市场」，而不只是「是不是境外」。

实现上是**限定到**而不是**短路**：短路会把 `Seattle, WA` 的城市也丢掉（限定词和城市其实一致）。
限定到 us 之后再挑，`Seattle, WA` 保住 seattle，`Dublin, OH` 因为匹配到的爱尔兰城市不在 us 而落到
市场级（`location_type: country`，城市为空——目录里只有 6 个美国城市，而美国有几千个）。

印证规则也跟着从「是否印证了任何市场」改成「印证的是哪个市场」：`Dublin, Ohio, United States` 里
"United States" 印证的恰好就是限定词已经指出的那个市场，按旧读法它会让限定词让位、把职位交还给
爱尔兰的城市别名。只有文本点名**另一个**市场才让位——这才是 `Berlin, DE` 判成德国而不是特拉华的
那条规则。

### [R5-06] 复验结论为什么需要独立的写入口

P7 给行加了 `verified_at` 与 `needs_verification`，但 `verified` 的唯一写入路径仍然在**评估结果**里，
而评估快照按设计只接受一次结果（重复提交报 `task already completed with a different result`）。
于是在 worker 之外复验的一轮无处安放答案：2026-09-27 实跑查了 15 条链接、全部 `alive`，表里却仍是
`verified: null`，报告把它们显示成未验证。

存活是**行的属性**，不是某次评估的属性。`merge_jobs.py verify` 因此只做一件事：

- 输入 `[{record_id, verified, reason?}]`，`verified` 接受契约里的词或布尔。
- 只动 `verified` 与 `verified_at`，不动评分、不动 `jd_profile`；带别的键直接拒绝。
- **不新建行**：认不出的 `record_id` 原样报在 `unknown_records` 里，而不是造一行。
- `verified: null` 什么都不写——"什么都没查到"不等于"查了但没定论"，给没做过的检查盖时间戳更糟。
- `closed` 也只是写下来，不删行：删哪些由读表的那一轮决定。
