# 远程视觉浏览器协议（Kernel BYOK）

> `remote_browser_enabled` 默认 `false`，这是容错阶梯最后一层的抓取兜底，只有显式开启后才需要读本文。流程入口见 [`WORKFLOW.md`](../WORKFLOW.md) 的容错阶梯。

1. 未配置时运行 `browser_setup.py`；密钥缺失或连接测试失败即跳过远程层，不阻塞整轮。
2. 使用第 0 步的 `run_id` 创建会话：`browser_control.py --metrics-run-id R create --round-id R --url U`。后续 screenshot/click/type/press/scroll/event/close 命令也传同一个 `--metrics-run-id`。控制脚本在调用 Provider **之前**原子预留并发、单轮会话数和估算费用预算；默认每次预留 `browser_cost_limit_usd / browser_session_budget`。
3. `screenshot` 保存到 `data/browser_sessions/`，browser worker 读取图片并用 `click/type/press/scroll` 操作。不要引入本机 Playwright 来控制远程会话。
4. 单个招聘列表最多 `browser_max_pages` 页；用 `browser_workflow.py` 的状态契约逐页观察、去重链接、再点击下一页。单站串行，多站并行。
5. 识别到验证码、登录、限流或人工确认时，返回 `user_action_required` 或 `rate_limited`，立即暂停该任务；不得自动解验证码、启用 stealth 或轮换代理。
6. 若 `browser_allow_handoff` 为 true，把本次 `create` 返回的临时 Live View URL 告诉用户。用户处理后在同一 session 继续截图；等待超过 `browser_handoff_timeout_minutes` 就关闭并标记未验证。等待期间其他 worker 继续。
7. 无论成功或失败都调用 `close --round-id R --session-id S`；关闭会释放并发槽，但已创建会话数和估算费用仍计入本轮硬上限。
8. 用 `browser_control.py event --status ...` 记录页数/链接计数、接管等待、限流和估算费用；动作本身自动记录 Provider 与耗时。不得记录 session id、Live View URL、页面 URL、输入文本、Cookie 或截图内容。
