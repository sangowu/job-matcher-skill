# 版本更新期望

> **本文记录"已知缺口 + 下一版期望",不是进度看板,也不是已完成工作的记录。**
> 已交付的工作在 [CHANGELOG.md](../CHANGELOG.md) 和各 phase 文档里;
> 这里只放**当前明确不做、但将来应该做**的事,以及做它需要满足的条件。
>
> 每条必须写清三件事:**为什么现在不做**、**做对需要什么**、**怎么算做完**。
> 写不出第三条的,说明还没想清楚,不该进这份文档。

## Remote 职位支持

### 现状:不搜索

v2.4.0 移除了 remote 相关的建模代码,发现流程**默认不搜索 remote 职位**。
`ats_pipeline.prefilter_jobs` 见到地点含 remote 词即跳过,无论它同时写了哪个地方。

### 为什么移除,而不是修好

原实现有一个**自信的错误答案**。`references/markets.json` 有一份 `remote_scopes`
目录(`ie` / `uk` / `de` / `eu` / `emea` / `global`),`normalize_location` 用它把
地点串解析成作用域。目录里没有的限定词会落到 `global`,也就是"全球开放":

| 地点串 | 移除前的答案 | 实际含义 |
|---|---|---|
| `Remote` | `global`,无市场限制 | 确实可能全球 |
| `Remote - US` | `global`,无市场限制 | **仅限美国雇佣** |
| `Remote - India` | `global`,无市场限制 | **仅限印度雇佣** |
| `Remote - Spain` | `global`,无市场限制 | **仅限西班牙雇佣** |

四者归一化结果完全相同。也就是说,**这个模块分不出"全球开放"和"限定在别处"**,
却对外宣称分得出。`normalize_location` 自己的 docstring 写的是
*"without guessing unknown places"* —— 它正在做自己声明不做的事。

这和"杜塞尔多夫不在城市目录里,于是 trivago 的职位匹配不到任何市场"是同一类缺陷:
**目录缺一条,就给出一个自信的错误答案,而不是诚实的"不知道"。**

补目录救不了它。见下。

### 为什么补目录救不了

2026-09-23 抽取 422 条标 remote 的真实职位,**51% 在 JD 正文里明写了地域/资格限制**,
而且限定的粒度比国家更细:

```
 53  based in Colorado, Hawaii, Illinois, Maryland, M…   ← 美国州一级
 25  based in New York, New Jersey, Washington State…
 20  authorized to work in the U.S.
  3  must reside in, and perform all work exclusively from within, the United States
  7  based in Colombia        3  based in India (Karnataka, Tamil Nadu, Telangana)
  2  based in Mexico          2  based in Japan
  4  based in Ireland         3  based in the UK
  2  based in the UK, Ireland, Germany, the Netherlands   ← 真正的多国 remote
```

根因是雇佣机制,不是标签写得潦草:雇主只能在**有法人实体、有 EOR 覆盖,
或愿意走承包商合同**的司法辖区付薪,还要承担当地税务与常设机构(permanent
establishment)风险。所以 remote 的作用域是**雇佣资格区域**,不是公司所在地,
也不是"不受地域影响"。

**关键结论:决定性信息在 JD 正文里,不在地点标签里。** 而 JD 正文按设计不进
主 agent 上下文(见 [WORKFLOW.md](../WORKFLOW.md) 的 JD 约束),只流向 merge
子进程和 run 级评估快照。所以"把 remote 做对"不是补几个别名,而是要动数据流。

### 做对需要什么

1. **资格抽取要发生在 JD 已经在的地方。** 候选位置是 merge 子进程或打分 worker ——
   它们本来就持有 JD 正文。不能为了这件事把正文提回主上下文。
2. **抽取结果必须是低基数的。** 例如 `eligible_markets: ["ie","uk"]` 或
   `eligibility: "unstated"`,而不是原文片段。这样它才能进 envelope、进主表、进指标。
3. **"未声明"必须是一等状态,不能塌缩成"全球"。** 49% 的职位 JD 里没写,
   那就是没写;把它当成"全球开放"正是这次移除的那个 bug。
4. **地点标签只作降级信号,不作判据。** `Remote - Ireland` 可以提高优先级,
   但不能单独作为"可申请"的依据。

### 怎么算做完

- [ ] `Remote - US`、`Remote - India`、`Remote`(无限定)三者归一化结果**互不相同**
- [ ] JD 里写了 `must reside in the United States` 的职位,对 `ie` 求职者被排除,
      且排除理由可追溯(不是静默丢弃)
- [ ] JD 里写了 `based in the UK, Ireland, Germany, the Netherlands` 的职位,
      对 `ie` 求职者**被保留**
- [ ] JD 未声明资格的职位进入一个显式的 `unstated` 状态,由用户或配置决定取舍,
      默认不当作可申请
- [ ] 全程没有 JD 正文进入主 agent 上下文,泄漏检查与现有 run 一致
- [ ] 有一次真实 run 的计数证据,说明 remote 通道带来了多少候选、排除了多少、
      以及其中多少是因为"未声明"

### 保留的痕迹

`CandidateEnvelope` 的 `location_normalized.remote_scope` 字段**保留但恒为 `null`**。
保留是为了不破契约、不让存量主表读不进来;恒为 null 是因为这个模块不再声称知道答案。
将来做对了,这个字段是现成的落点 —— 但届时它的取值应当来自 JD 抽取,不是地点标签。
