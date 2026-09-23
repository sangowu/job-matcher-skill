# CV 抽取规则（cv_schema）

> 抽取者（编排者本人，或其委派的一个子代理）读本文件，从 CV 纯文本产出 `CVProfile` JSON，供 `validate_profile.py` 校验补全。
> 目标只为"检索职位 + 给职位打分"服务——只抽有用字段，无关细节不抽。

## 任务

读 CV 文本（路径由编排者给出），输出**一个 JSON 对象**（可用 ```json 包裹）。**只输出 JSON，不要解释**。

## 输出字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `summary` | string | 一句话专业定位（如"5 年高并发后端工程师"） |
| `preferred_roles` | string[] | 目标职位，中英文写法都可；CV 没写求职意向则**留空数组**并在 `missing` 登记 |
| `skills` | string[] | 技能，**归一化**（见下表） |
| `years_of_experience` | number 或 null | **与目标方向相关**的经历年限（见规则） |
| `seniority` | string | **只输出六档之一**：`intern` / `new_grad` / `junior` / `mid` / `senior` / `lead`。不要输出 levels 列表（脚本会补） |
| `target_locations` | string[] | CV 明确写出的目标求职地点；没有就留空 |
| `current_location` | string | CV 明确写出的现居地；不是目标地点，只作最后回退 |
| `preferred_locations` | string[] | 旧版兼容字段；新抽取优先写 `target_locations` / `current_location` |
| `languages` | object[] | `[{name, code, level}]`，如 `{"name":"中文","code":"zh","level":"母语"}` |
| `industries` | string[] | 行业（如 互联网、金融科技） |
| `education_level` | string | 最高学历（如 本科、硕士） |
| `current_title` | string | 最近/当前职位头衔 |
| `cv_language` | string | CV 正文主语言，内部使用 `en` / `de` / `zh-Hans` |
| `report_language` | string | 报告语言；未指定时等于 `cv_language` |
| `search_language` | string | 旧版兼容别名，等于 `cv_language`；不得再作为市场搜索语言来源 |
| `missing` | string[] | 抽不到的字段名清单 |

## 抽取规则

### 1. 经验年限 → 用「相关年限」定级
- `years_of_experience` 按**与 `preferred_roles` 相关**的经历累加，**实习经历折半计**。
- 例：做了 8 年财务、转行 1 年开发、目标是开发岗 → 填 `1`（相关年限），不是 9。
- 据此推断 `seniority`：综合相关年限 + 职责复杂度 + ownership/leadership。

### 2. seniority → 经历证据优先于头衔
- 头衔与实际经历冲突时，**以经历证据为准**。
- 例：CV 写"高级工程师"但只有 2 年、职责偏执行 → `seniority` 填 `junior`/`mid`，不盲从头衔。
- 判不准则填合理估计；六档关键词对照：intern(实习)/new_grad(应届)/junior(初级)/mid(中级)/senior(高级·资深)/lead(架构/principal/staff/manager/总监)。

### 3. 地点 → 拆分目标地点与现居地
- 明确求职地点写入 `target_locations`；现居地址单独写入 `current_location`。
- 只有现居地址时不要把它改写成目标地点；market plan 仅把它作为最后回退。
- CV 完全没有地点 → 两者留空（编排者会追问）。旧输入仍可保留
  `preferred_locations`，但用户本轮明确地点始终覆盖它。

### 4. 技能归一化
统一常见别名，便于后续匹配：

| 原始 | 归一为 |
|------|--------|
| JS / js | JavaScript |
| TS | TypeScript |
| K8s / k8s | Kubernetes |
| py | Python |
| golang | Go |
| postgres / pg | PostgreSQL |
| ML | Machine Learning |
| k8s / 容器编排 | Kubernetes |

（其余技能照写规范名。）

### 5. 报告语言与搜索语言分离
判断 CV 正文主要语言并写 `cv_language`；中文边界输入 `zh`/`zh-CN` 校验后统一为
`zh-Hans`。`report_language` 默认等于它。市场搜索语言由 `market_plan.py` 根据目标市场
生成，不再直接等于 CV 语言。保留 `search_language` 仅供旧流程兼容。

### 6. 不臆造
- CV 没有的信息**不要编造**（尤其薪资、不存在的技能、虚构经历）。
- 抽不到的字段：字符串填 `""`、数组填 `[]`、数字填 `null`，并把字段名加入 `missing`。

## 非简历检测
如果文本明显**不是简历**（合同、PPT、文章等），不要硬抽——输出 `{"error": "输入似乎不是简历", "detail": "<简述内容>"}`，让编排者提示用户。

## 输出示例
```json
{
  "summary": "5 年高并发后端工程师",
  "preferred_roles": ["后端工程师", "Backend Engineer"],
  "skills": ["Python", "Go", "PostgreSQL", "Kubernetes"],
  "years_of_experience": 5,
  "seniority": "senior",
  "target_locations": ["上海"],
  "current_location": "杭州",
  "preferred_locations": [],
  "languages": [{"name": "中文", "code": "zh", "level": "母语"}, {"name": "英文", "code": "en", "level": "流利"}],
  "industries": ["互联网"],
  "education_level": "本科",
  "current_title": "高级后端工程师",
  "cv_language": "zh-Hans",
  "report_language": "zh-Hans",
  "search_language": "zh-Hans",
  "missing": []
}
```
