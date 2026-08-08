# job-matcher

[English](README.en.md) | **中文**

版本文档：[更新记录](CHANGELOG.md) · [v2.2.0 发布说明](docs/releases/v2.2.0.md) · [v2.1.0 发布说明](docs/releases/v2.1.0.md) · [v2.0.0 发布说明](docs/releases/v2.0.0.md)

> 一个 **agent skill（Claude Code 与 Codex 通用）**：输入**简历(CV) + 求职意向**，自动抽取简历字段、用 **web 搜索实时检索**匹配职位，生成一份**可交互的 HTML 报告**。

是 [JobRadar](https://github.com/sangowu/JobRadar) 的**轻量版**——纯 agent 原生能力（web 搜索 + 子代理 + Python 脚本），**零外部服务依赖**，借鉴 JobRadar 的 schema、算法与界面风格。

---

## ✨ 功能

- 📄 **简历解析**：支持 PDF / DOCX / TXT / MD，或直接粘贴文本（不做 OCR）。
- 🧠 **结构化抽取**：抽取目标职位、技能、资历(seniority)、地点、语言等，自动按相关年限定级。
- 🔎 **实时职位检索**：基于 WebSearch 自适应分批搜索，按 CV 语言切换市场（中/英）。
- 🎯 **5 维匹配打分**：title / seniority / skills / location / must-have，输出五档投递建议（强烈投递→跳过）。
- 🗂️ **增量缓存**：CV、JD、匹配分三层缓存；多来源同职位自动聚合；换 query 自动失效重算。
- 📊 **可交互报告**：两栏布局（左职位列表 30% + 右详情 70%）+ 评分徽章 + 深色模式 + 排序/筛选/搜索 + 7/30 天运行健康快照 + 中英 i18n，自包含单文件 HTML。

## 🏗️ 架构

- **主 agent = 编排者**：调脚本、融合 query、追问用户、spawn subagent。
- **subagent 承担重上下文工作**（CV 抽取 / 搜索 / 打分）：大块原始文本留在 subagent，主上下文只搬「路径 + 小 JSON」，保持整洁。
- **Python 脚本承担确定性工作**：解析、校验、去重聚合缓存、失效验证、渲染。
- **并行计算、串行提交**：搜索和评估 worker 可重叠运行；`jobs_table.json` 只有一个写入路径，使用评估快照、跨进程锁和原子替换防止丢失更新。

```
CV + query
   │ [脚本] extract_cv          → 纯文本 + cv_hash
   │ [缓存检查]                 → 命中则跳过抽取
   │ [subagent] 抽取 CVProfile  → [脚本] validate_profile
   │ [主agent] 融合 query       → search_plan + candidate_profile
   │ [并行 subagent] WebSearch+解析+初筛 → [脚本] merge_jobs(去重/聚合/缓存+评估快照)
   │ [并行 subagent] 粗排→精排抓JD+5维打分 + 失效验证 → [脚本] 条件化回写
   │ [脚本] render_html         → report_*.html（自动打开）
   ▼
可交互 HTML 报告
```

**容错阶梯**（失效验证 & JD 抓取共用）：`WebFetch → requests 静态抓 → playwright headless（复用系统默认浏览器，不另下载）→ 标注未验证不阻塞`。

## 📁 结构

```
job-matcher/
├── SKILL.md              # 触发描述 + 编排入口
├── WORKFLOW.md           # agent-中立完整流程
├── config.json           # 配置旋钮
├── docs/monitoring.md     # 运行指标、阈值与健康汇总
├── references/           # subagent 按需读取的指令
│   ├── cv_schema.md          # CV 抽取规则
│   ├── scoring_rubric.md     # 5 维打分 + 五档阈值
│   └── search_playbook.md    # fan-out / 分市场 / 自适应分批
├── scripts/              # 确定性 Python 脚本
│   ├── extract_cv.py         # 解析 CV → 文本 + hash
│   ├── validate_profile.py   # 校验 + seniority→levels 映射
│   ├── analysis_contract.py  # 校验 JDProfile/MatchScore worker 输出
│   ├── merge_jobs.py         # 单写入器：去重/缓存/评估快照/条件化回写
│   ├── runtime_metrics.py    # PII-safe JSONL 指标与健康计算
│   ├── summarize_metrics.py  # 7/30 天 Markdown/JSON 健康报告
│   ├── verify_jobs.py        # 失效职位状态码检测
│   ├── fetch_rendered.py     # headless 渲染兜底（复用系统浏览器）
│   ├── render_html.py        # 渲染 HTML 报告
│   └── _jobutil.py           # 共享：归一化/去重键/URL 规范化
├── assets/template.html  # 静态报告模板（Tailwind + 纯 JS）
└── data/                 # 运行时数据（.gitignore，含 PII）
```

## 🚀 使用

克隆到个人 skill 目录（目录名用 `job-matcher`，与 skill 名一致）：

```bash
# Claude Code
git clone https://github.com/sangowu/job-matcher-skill ~/.claude/skills/job-matcher
# Codex
git clone https://github.com/sangowu/job-matcher-skill ~/.agents/skills/job-matcher
```

agent 会自动识别。然后在对话里：

> 这是我的简历 `D:\cv.pdf`，帮我找远程后端职位

或直接粘贴简历文本 + 求职意向。skill 会走完整流程并在浏览器打开报告。

## ⚙️ 配置

`config.json` 集中所有旋钮：

| 键 | 默认 | 说明 |
|----|------|------|
| `top_n` | 15 | 最终展示职位数 |
| `precise_buffer` | 5 | 精排多抓缓冲 |
| `max_parallel_subagents` | 3 | 批内并行上限 |
| `max_websearch_calls` | 6 | WebSearch 总次数上限 |
| `stop_threshold` | 12 | 净有效职位达标停止 |
| `jd_ttl_days` | 30 | JD 缓存有效期 |
| `seniority_mode` | balanced | strict / balanced / stretch |
| `enable_headless_fallback` | true | headless 兜底开关 |
| `headless_budget` | 3 | 每次运行 headless 上限 |
| `table_lock_timeout_seconds` | 10 | 等待主表写锁的最长秒数 |
| `stale_lock_seconds` | 120 | 回收异常遗留锁的时间阈值 |
| `monitoring_default_window_days` | 7 | 默认健康报告窗口 |
| `monitoring_thresholds` | 见配置 | 冲突、拒绝、成功率、锁等待和积压阈值 |

运行时只保留一个职位主表 `data/jobs_table.json`。每轮待评估职位写入最小化快照 `data/eval_runs/<run_id>.json`；worker 完成后由主 agent 串行回写评估字段，成功完成的快照会释放，仅在 `history.jsonl` 留下不含 CV/JD 正文的摘要。每次 merge/update 另写一条 PII-safe `data/metrics.jsonl` 事件。

## 📈 运行监控

```text
python scripts/summarize_metrics.py --days 7 --format markdown
python scripts/summarize_metrics.py --days 30 --format json
python scripts/summarize_metrics.py --fail-on-breach
```

报告包含吞吐/缓存、评估成功/拒绝/冲突率、命令与锁等待 p50/p95/p99，以及活跃 run、pending task 和最老积压时间。每次生成 HTML 时会自动嵌入 7/30 天静态快照，可从顶部状态入口查看；默认阈值违规时状态为 `degraded`。CLI 的 `--fail-on-breach` 同时返回退出码 2。字段定义、隐私边界和接入方式见 [运行时监控文档](docs/monitoring.md)。

## 🔧 依赖

- Python 3.10+
- 必需：`pdfplumber` `python-docx` `requests`
- 可选：`playwright`（headless 兜底，复用系统已装的 Chromium 系浏览器，无需 `playwright install`）

```bash
pip install pdfplumber python-docx requests
pip install playwright   # 可选
```

---

*Built with [Claude Code](https://claude.com/claude-code).*
