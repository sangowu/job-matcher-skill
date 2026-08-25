# 远程浏览器 Provider 与本地控制面板设计草案

状态：Confirmed / In implementation。2026-08-25 已确认 Kernel、临时 localhost 页面、系统密钥库 + 环境变量备用、3 页/10 会话/10 分钟/1 美元硬上限、Fake Provider CI 与“先远程浏览器、后 ATS”的分阶段范围。

目标版本：基于 Job Matcher v2.2.0。

## 1. 背景

当前 Job Matcher 以 Web Search 发现候选职位，并按以下容错阶梯获取 JD：

`WebFetch -> requests 静态抓取 -> 本机 Playwright headless -> 未验证降级`

本次改动拟增加远程隔离浏览器服务，使 Agent 能处理动态职位列表、结果翻页和 JD 页面；遇到登录、验证码或限流时，将单个浏览器会话交给用户处理，不阻塞其他职位任务。

## 2. 已确定边界

- 继续保持 Skill 的 agent 中立编排方式和脚本契约。
- 使用 BYOK：每位用户配置自己的浏览器服务 API Key。
- API Key 不写入 `config.json`、职位表、运行指标或 HTML 报告。
- 浏览器服务是增强/兜底管道，不替代 Web Search。
- ATS 与远程浏览器纳入同一总体设计，但分为两个可独立验证的阶段：先完成远程浏览器闭环，再接入 ATS 增强管道。
- ATS、普通静态抓取和远程浏览器最终写入同一个职位主表，由现有串行 `merge/update` 路径去重和提交。
- API 配置使用独立的“设置/数据源”页面，不放入 JD 详情面板或职位分页区域。
- 单个会话等待用户验证时，其他搜索、抓取和评分任务继续执行。

## 3. 推荐范围

### 3.1 第一阶段

第一阶段只完成可验证的最小闭环：

1. 本地设置页面：选择 Provider、输入 API Key、测试连接、设置预算。
2. 一个正式 Provider 实现，加一个仅用于测试的 Fake Provider。
3. 远程浏览器作为现有抓取阶梯的最后一级。
4. 支持单个职位详情页抓取，以及一个招聘列表的受限翻页。
5. Provider 返回 `user_action_required` 时，将 Live View URL 交给用户，完成后恢复该会话。
6. 增加无真实 API 调用、无费用的自动化测试和 CI 检查。

### 3.2 暂不包含

- 自动绕过验证码、反爬机制或登录限制。
- 默认启用住宅代理、IP 轮换或 stealth 模式。
- 自动提交职位申请。
- 在 CI 中调用真实云服务或保存真实 API Key。
- 一次性实现所有 Browserbase、Kernel、Anchor、Hyperbrowser 等供应商。
- 在远程浏览器第一阶段同时实现 ATS Provider；ATS 作为下一独立阶段实施。
- 将静态职位报告改造成常驻 Web 应用。

## 4. 建议架构

保持现有“Agent 编排 + 独立 Python 脚本 + 静态报告”结构，只增加两个边界：

### 4.1 本地控制面板

- 通过一个 setup 命令启动仅绑定 `127.0.0.1` 的临时页面。
- 页面负责 Provider 选择、API Key、地区、并发数、页面上限和费用上限。
- 保存并测试成功后可自动关闭，不要求常驻服务。
- 报告可提供“设置”入口，但不直接嵌入密钥表单。
- 用户接管优先打开供应商提供的临时 Live View URL；该 URL 不持久化。

### 4.2 Provider 适配层

Skill 和抓取流程只读取统一结果，不直接依赖供应商 SDK。建议最小返回结构：

- `status`: `ok | user_action_required | rate_limited | failed`
- `text`: 提取后的页面正文
- `links`: 列表页发现的职位链接
- `session_id`: 仅用于恢复当前会话
- `live_view_url`: 仅在需要人工操作时临时返回
- `usage`: 浏览器时长、步骤数或供应商额度消耗
- `reason`: 可公开给用户的失败原因

第一阶段优先使用供应商 HTTP API；只有供应商本身要求时才引入其 SDK。

## 5. 数据流

```text
CV + 求职条件
      |
      v
Web Search 并发发现候选职位
      |
      v
识别 ATS / 普通网页 / 动态网页
      |
      +--> ATS API -----------+
      +--> requests 静态抓取 -+--> 字段标准化 --> 串行 merge 去重
      +--> 远程浏览器服务 -----+                       |
                |                                      v
                +--> 用户接管后恢复               并发 JD 评分
                                                       |
                                                       v
                                                 串行生成报告
```

API Key 数据流独立：

```text
设置页面 -> 本地后端 -> 系统密钥库 -> Provider 适配器 -> 云服务
```

## 6. 并发与翻页

- Web Search query：沿用现有全局并发预算并行执行。
- 不同公司/网站的 ATS、静态抓取和远程浏览器任务：可并发。
- 同一个招聘列表内部的第 1、2、3 页：默认串行，避免重复和限流。
- 不同网站的列表翻页：可并发，但受单独的浏览器并发上限约束。
- JD 提取和评分：可并发。
- `jobs_table.json` 的 merge/update 与最终报告渲染：保持串行。
- 某个远程会话等待用户处理时，只暂停该会话，不占用评分 worker。

## 7. 配置与密钥

推荐使用系统密钥库：Windows Credential Manager / macOS Keychain，通过跨平台密钥库封装访问。

`config.json` 只保存非敏感设置，例如：

- 是否启用远程浏览器兜底
- 默认 Provider 名称
- 最大远程浏览器并发数
- 单个列表最大页数
- 单轮最大远程会话数
- 单轮额度/费用硬上限
- 是否允许人工接管

密钥读取失败时应降级到现有抓取阶梯，而不是阻断整个 Job Matcher。

## 8. 失败与人工接管

1. Provider 返回验证码、登录、限流或人工确认状态。
2. 当前任务写入内存中的等待状态，并立即释放其评估 worker。
3. 向用户展示供应商的临时 Live View URL 和失败原因。
4. 用户完成处理并确认继续。
5. 使用同一 `session_id` 恢复；超时则关闭会话并把职位标记为未验证。
6. 其他职位继续搜索、抓取、合并和评分。

Live View URL、Cookie、认证状态和页面正文不得写入运行指标。

## 9. 测试与 CI

自动化测试不连接真实供应商：

- Provider 接口契约测试。
- Fake Provider 的成功、限流、人工接管、恢复和超时测试。
- 配置校验、预算硬上限和密钥不落盘测试。
- 列表翻页上限、重复链接去重和停止条件测试。
- 多任务并发、单任务暂停及主表串行提交测试。
- 报告不包含 API Key、Live View URL 或浏览器认证数据的回归测试。
- 保持现有完整测试集、Ruff 和文档一致性检查通过。

真实 Provider 只做本地手动 smoke test，必须由用户提供 API Key 并明确允许产生外部请求或费用。

## 10. 性能基线、版本指标与日志

每个实施里程碑和正式版本都必须记录同一套 PII-safe 指标，不能只报告“测试通过”：

- Skill 版本、Git commit、操作系统、Python 版本和测试时间。
- 固定数据集名称、职位数、迭代次数、并发参数和缓存状态。
- merge、update、render 及确定性核心端到端耗时的 p50/p95。
- Web Search、ATS、静态抓取、远程浏览器各自的任务数、成功率、降级率和缓存命中率。
- 远程会话数、浏览器时长、人工接管次数/等待时间、限流次数和额度/费用。
- 最终有效职位数、去重率、已验证比例和评分完成率，防止用减少工作量换取表面提速。
- 失败类型和阶段，但不记录 CV/JD 正文、API Key、Cookie、Live View URL 或用户认证信息。

性能结论必须使用同一个固定小型数据集做前后对比，并同时满足：

1. 输出职位数、去重结果和评分完整性不退化。
2. 使用相同的迭代次数、并发设置和缓存状态。
3. 同时报告绝对值与相对变化，不用单次最快结果代表整体性能。
4. 真实云服务结果单独标记为 smoke/observational，不能与确定性本地基线混为一谈。

初始基线先测 v2.2.0 的确定性核心管道（15 个合成职位，merge -> update -> render），不调用真实 Web Search、ATS 或云浏览器。实现后再增加 Fake Provider 并发基线和经用户授权的真实 Provider 小流量 smoke test。

初始测量已完成，摘要及逐次原始记录见 [`docs/performance/v2.2.0-small-baseline.md`](performance/v2.2.0-small-baseline.md) 和 [`docs/performance/v2.2.0-small-baseline.json`](performance/v2.2.0-small-baseline.json)。

## 11. 分阶段实施建议

1. Provider 契约、配置 schema 和 Fake Provider。
2. 临时本地设置页面与系统密钥库。
3. 首个真实 Provider 和单页 JD 抓取。
4. 受限招聘列表翻页。
5. Live View 人工接管与恢复。
6. 运行指标、文档、完整测试和 CI。
7. 在上述闭环独立验证后，接入 ATS 标识库、ATS Provider 和 ATS 列表分页。

每一阶段均可独立回退；未配置远程服务时，现有 v2.2.0 流程保持不变。

## 12. 已确认决策

本轮实现按以下决定执行：

1. 首个 Provider 只实现 Kernel；Fake Provider 只用于本地测试和 CI。
2. 配置面采用仅绑定 `127.0.0.1` 的一次性 localhost 页面，不引入常驻 Electron/FastAPI 应用。
3. API Key 优先进入系统密钥库；无 UI 环境允许 `KERNEL_API_KEY`，环境变量优先。
4. 单站最多 3 页，单轮最多 10 个会话，并发最多 2。
5. 人工接管等待最多 10 分钟；超时关闭并标记未验证。
6. 单轮估算费用硬上限 1 美元；在创建远程会话前预留预算。
7. 第一版只通过 setup 命令打开设置页，不修改 JD 展示面板。
8. CI 只使用 Fake Provider；真实 Kernel smoke test必须由用户提供密钥并在调用时明确授权。
9. 搜索用 `gpt-5.6-luna/low`；CV 抽取用 `gpt-5.6-terra/medium`；评估和视觉浏览用 `gpt-5.6-terra/high`，全部默认 `fork_turns=none`。运行时不支持覆盖时回退继承并记录实际值。
10. 实现完成后先运行本地 CI 等价检查；提交、推送、PR 和远端 CI 仍需用户在操作时授权。
