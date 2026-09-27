# Phase C 双 route 入口（兼容路径）

> 当前流程用 `discovery_plan.py` + `discovery_batch.py`（见 [`WORKFLOW.md`](../WORKFLOW.md) 第 4 步）。
> 这里是它取代的旧入口，仍然可用，只在维护 `candidate_handoff.py` 或读旧运行记录时需要。

- **旧 Phase C 多地区入口（兼容）**：同一条消息启动 `regional_registry` 与 `agent_web_search` worker，只回传 immutable route batch，不写主表/快照/注册表/指标；每条 route 必须回报终态，失败 route 候选为空但不阻断另一 route。两条都返回后把同一 `batch_id` + market/source plan + route batches 送入 `candidate_handoff.py`：**先 `merge_jobs.py merge --batch-id B`，再串行写 source registry，顺序不得交换**；registry 失败按 `data/candidate_runs/<batch_id>.json` 重试，只补 registry 提交。重复同一 batch 不增加 `seen_count` 或评估任务。
- CandidateEnvelope 结构见 `references/candidate_envelope.schema.json`；`raw_sources[]` 是 provenance 唯一事实源，`market_id` 只作元数据、不参与职位身份。老候选缺 `discovery_route` 走兼容路径，老表缺市场/来源字段下次 merge 惰性补 `unknown`，不做破坏性迁移。
