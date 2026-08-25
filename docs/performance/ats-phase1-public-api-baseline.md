# ATS Phase 1 公开 API 小基线

日期：2026-08-25。基线代码版本：Job Matcher v2.3.0 分支。环境：Windows 11、Python 3.13.5。

## 方法

- 供应商：Ashby、Greenhouse、Lever，各 2 个公开公司 board。
- 地区覆盖样本：中国、美国、欧洲；地区数字只是 location 字符串信号，允许同一职位命中多个地区，不等同于签证或申请资格。
- 最大并发 3；Ashby/Greenhouse 单响应；Lever 每页 50，同 board 顺序翻页，最多 10 页。
- 只做公开 GET，共 7 次请求；不使用 API Key、不登录、不提交申请。
- JD、职位标题和职位 URL 只在内存中用于规范化/统计，不写入基线文件。

运行命令：

```text
python scripts/benchmark_ats.py --output docs/performance/ats-phase1-public-api-baseline.json --page-size 50 --max-pages 10 --max-workers 3 --timeout-seconds 30
```

## 最终结果

| Provider | 公司 | 职位 | 请求/页 | 耗时 ms | 截断 |
|---|---|---:|---:|---:|---|
| Ashby | Cohere | 147 | 1 | 399.396 | 否 |
| Ashby | Zapier | 8 | 1 | 201.097 | 否 |
| Greenhouse | Doctolib | 124 | 1 | 307.393 | 否 |
| Greenhouse | XPENG | 29 | 1 | 5695.630 | 否 |
| Lever | ShopBack | 78 | 2 | 4587.299 | 否 |
| Lever | Sword Health | 26 | 1 | 4795.081 | 否 |

汇总：

- 6/6 board 成功，412/412 条规范化成功，412 条均带 JD 正文信号。
- 7 次 GET、6,152,981 bytes；墙钟 5,903.400 ms，board 耗时 p50 399.396 ms、p95 5,695.630 ms。
- 地区字符串信号：中国 23、美国 118、欧洲 155。
- Provider job ID 与规范化 URL 均为 412 个唯一值，强身份重复率 0%。
- 现有公司 + 标题弱键只有 304 个唯一值；47 个碰撞组额外覆盖 108 条记录，潜在弱键碰撞率 26.21%。这不是“确认重复率”，而是当前 merge 可能误合并的上限信号。

原始脱敏指标：[`ats-phase1-public-api-baseline.json`](ats-phase1-public-api-baseline.json)。

## 探索运行与调整

第一次探索运行采用 3 页、20 秒限制：5/6 board 成功；JetBrains Greenhouse 全量响应超时，Shield AI Lever 在 3×50 后仍满页并被标记截断。该运行证明浏览器 3 页预算不能直接复用给 ATS，也证明大 board 需要独立超时策略。为保持小样本可复现并覆盖中国，最终样本改为 XPENG 与 ShopBack，Lever 上限提升到 10 页。

探索运行的脱敏记录：[`ats-phase1-exploratory-run.json`](ats-phase1-exploratory-run.json)。

## 结论

三个官方公开接口都能显著降低浏览器抓取成本，跨 board 并发也能把多个慢响应重叠。但当前统一表的弱身份模型是生产接入阻塞：必须先引入强身份/稳定 record ID，再把 ATS 归一化候选接入单写入器。不能用“412 条抓取成功”宣称召回率，因为样本没有完整外部 ground truth。
