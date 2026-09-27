# Phase D2 来源 smoke 与 Phase E shadow 门（仅显式运行）

> 两者都不在正常一轮里跑，也都不能影响正式报告。阶段背景见 `docs/multi-region-phase-d2.md` 与 `docs/multi-region-phase-e.md`。

- **Phase D2 仅显式 smoke**：需要来源诊断时才跑 `multi_region_smoke.py --live --output <count-only.json>`。计划固定在 `references/multi_region_smoke_plan.json`；每来源最多两次同站 HTTPS GET，单响应 512 KiB、8 秒超时、最多两次重定向；登录/验证码立即停止；`automation_allowed=false` 的来源必须零请求并记 `skipped_policy`。产物只留状态、失败类别和计数，不留公司/标题/URL/query/页面/JD 正文；外部失败不当作 pytest 回归。**不得把小样本解释为市场召回率，也不得据此默认启用来源。** `[R6-03]`
- **Phase E shadow 门**：shadow 编排不得把地区候选写入正式报告或改变排序；用 `references/shadow_compare_v2.schema.json` 临时输入，经 `shadow_compare.py` 在内存按强身份合并出 count-only 输出（不含身份键或业务正文），只有它能交给 `shadow_gate.py record`。`preliminary/inconclusive` 一律阻止 `default`；未过门禁的 `default` 配置无效。**不得用离线 fixture 或同一天重复执行冒充真实 shadow 覆盖。** `[R6-04]`
