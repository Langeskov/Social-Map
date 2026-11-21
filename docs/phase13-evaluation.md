# Phase 13 — Analysis 评估框架（Phase 13.0：Evaluation Framework & Dataset）

本文档描述 Social-Map Analysis 的**离线评估基础设施**：评估目标、合成数据集规范、
Scenario 结构、指标定义、离线运行命令、数据隐私规则与已知局限。
配套实现位于 `app/evaluation/`，合成数据集位于 `tests/evaluation/dataset/`。

> 本阶段只评估**模型输入（上下文与提示词）的构建质量**与评估契约本身。
> 模型输出的解析（13.1）与来源校验（13.2）尚不存在，相关指标在评估结果中一律
> 报告为 **N/A 并附原因**，绝不伪装成已验证。

---

## 1. 评估目标

1. 我们用什么数据判断模型分析是否正确？——**版本化的合成评估数据集**（虚构人物、
   关系、事件、证据、记忆 + 参考答案）。
2. 如何定义一条分析任务的标准上下文？——每个 Scenario 绑定一个 Fixture（输入宇宙）
   与一份 RequestSpec（目标、时间范围、开关、预算），经真实 ContextBuilder /
   PromptBuilder 构建。
3. 哪些输出质量可以自动判断？——结构、引用、来源集合、泄漏、上下文边界等**确定性**
   检查（见指标表）。
4. 哪些质量只能人工评价？——推断合理性、矛盾识别、表述恰当性、完整性（见指标表
   的 `needs_human` 列与每个 Scenario 的 `human_review` 清单）。
5. 如何保证不同模型使用相同输入？——固定 dataset/scenario 版本 + 固定 `as_of` +
   确定性物化 + 可复现上下文（掩码易变值后逐字节一致）。
6. 如何防止测试数据泄漏到生产事实库？——数据集只声明 `synthetic: true` 的虚构数据；
   加载器纯文件解析（无数据库）；物化器只写入调用方指定的临时目录；模型请求只包含
   ContextBuilder 产物，参考答案永不进入提示词（PROMPT_LEAKAGE 强制检查）。

## 2. 目录与组件

```text
app/evaluation/
    models.py      评估契约 DTO、指标注册表、结果结构（纯 stdlib）
    dataset.py     数据集加载与 fail-closed 校验（无 DB / 无网络 / 无 Qt）
    fixture.py     合成夹具物化器（经真实 Service 写入评估专属临时库）
    runner.py      离线 Evaluation Runner（真实 ContextBuilder / PromptBuilder）
    report.py      manifest.json / raw-results.json / metrics.json / report.md
    __main__.py    python -m app.evaluation 离线入口

tests/evaluation/
    dataset/
        dataset.json                      清单（版本、文件列表、synthetic 声明）
        fixtures/fixture-*.json           10 个合成输入宇宙（A–J）
        scenarios/scenario-*.json         17 个版本化评估任务
    test_dataset.py                       数据集 Schema / 唯一性 / 版本 / 引用完整性
    test_evaluation_contract.py           评估契约字段 / 指标注册表 / 泄漏原则
    test_evaluation_runner.py             离线运行 / 可复现 / 无网络 / N-A 语义
tests/services/test_token_estimate.py     Token 估算边界（6.5 复核配套）
```

架构边界：评估层只读 Domain 与 Service，不写事实、不写 Memory、不发网络请求、
不调用 LLM；Parser / Evaluator（后续阶段）同样不得产生事实写入。

## 3. Dataset 规范

### 3.1 版本

| 字段 | 值 | 含义 |
|---|---|---|
| `schema_version` | `1` | 数据集/scenario JSON 结构版本；加载器不识别的版本直接拒绝 |
| `dataset_version` | `1.1` | 当前合成数据集内容版本；scenario 必须声明一致的值 |

任何结构性修改都必须递增 `schema_version`（破坏性）或 `dataset_version`（内容），
并在本文件登记。

### 3.2 清单（dataset.json）

`schema_version`、`dataset_id`、`dataset_version`、`title`、`description`、
`synthetic: true`（强制）、`fixtures[]`、`scenarios[]`（相对路径列表）。

### 3.3 夹具（fixtures/*.json）

一个夹具 = 一个完全虚构的 Workspace 宇宙。允许的记录：

- `workspaces[{key, name}]`——至少一个；可含第二个 Workspace 用于隔离检查；
- `people[{key, workspace, display_name, real_name, gender, occupation,
  organization, location, birthday, notes, aliases[]}]`；
- `relationships[{key, workspace, person_a, person_b, relationship_type,
  directionality, initial_state, effective_at, note, history[{state, effective_at, note}]}]`；
- `events[{key, workspace, title, event_type, record_kind(FACT|OBSERVATION),
  occurred_on|started_at|ended_at, location, description, participants[]}]`；
- `evidence[{key, workspace, event, kind, title, source_type, content_text,
  annotation, source_label, source_reference, captured_at, media[]}]`；
- `memories[{key, workspace, content, kind, person, valid_from, valid_until}]`；
- `bulk_events` / `bulk_evidence`——确定性批量展开（`{index}` 占位符、
  `occurred_on_step_days` 步进），单组上限 500 条，用于 Scenario J 的预算逼近。

所有 key 在夹具内唯一；引用（person/event/workspace）必须存在且同 Workspace。
`synthetic` 必须显式为 `true`，否则整个数据集拒绝加载。

### 3.4 契约：预期上下文与参考答案的语义分工

| 字段 | 语义 | 验证方式 |
|---|---|---|
| `expected_facts[]` | 参考答案事实清单（fact_id + 来源 + 表述） | 13.1/13.2 的参考答案比对（现为 N/A） |
| `required_sources[]` | 本次分析可验证引用的来源集合 | CONTEXT_BINDING（13.0 确定性） |
| `forbidden_claims[]` | 明确禁止的推断/断言 | 13.1+ 参考答案比对 + 人工 |
| `expected_uncertainties[]` | 应当识别的不确定性 | 13.1+ + 人工 |
| `human_review[]` | 只能人工评价的部分 | 人工复核（13.4 工作流） |
| `evaluation_dimensions[]` | 本场景适用的指标 | 未列入的指标不产生结果行 |
| `expected_context` | 对**模型输入**的确定性断言 | CONTEXT_EXPECTATIONS（13.0 确定性） |

`expected_context` 字段：

- `included[]` / `excluded[]`——按 **来源集合成员**（`snapshot.source_ids()` /
  `included_memory_ids`）断言；`excluded` 额外做记录标记文本缺席检查；
- `required_text[]` / `forbidden_text[]`——渲染上下文的文本断言；
- `expected_error`——`null` 或 `"CONTEXT_LIMIT"`（超限时必须显式拒绝并给出指引，
  不得静默截断）。

`sample_output`（可选，13.2 起；dataset_version 1.1）：

```json
"sample_output": {
  "label": "…",
  "content": "<固定模型回答（JSON 字符串可用）>",
  "expected_references": [
    {"ref_type": "EVENT", "source": {"type": "EVENT", "key": "call-night"},
     "expected_status": "VALID_FULL"},
    {"ref_type": "EVENT", "literal_id": "00000000-0000-4000-8000-0000000000de",
     "expected_status": "UNKNOWN_SOURCE"}
  ],
  "expected_missing_fact_references": 0
}
```

`content` 可嵌入 `<<TYPE:key>>` 占位符，Runner 在解析前替换为真实 UUID；
加载器保证占位符与 expected 引用都指向真实夹具来源、且确实出现在 content 中。
`REFERENCE_GROUNDING` 指标用它确定性验证：真实引用、有限曝光、跨 Workspace、
类型错误、未曝光、未知 UUID 与"FACT 条目缺少引用"。

**语义注意**：`included/excluded` 验证的是来源集合，不是文本出现。Phase 13.1 实现
来源曝光契约（§10）后，关系端点等 `IDENTITY_ONLY`／参与人 `PARTICIPANT_CONTEXT` 人物
已进入 `source_ids()`，数据集相应把次要人物重新纳入 `required_sources`（同时保留
`required_text` 文本断言）。

### 3.5 参考答案防泄漏原则（硬性）

`expected_facts` / `forbidden_claims` / `expected_uncertainties` 的文本是**参考答案**，
绝不能出现在模型请求里。两层检查：

1. 数据集级：参考答案文本不得是夹具数据的子串（`test_evaluation_contract.py`）；
2. 运行级：PROMPT_LEAKAGE 对真实 PromptBuilder 三段消息 + 渲染上下文做全串
   包含检查（`runner.py`）。

编写参考答案时请使用与上下文不同的表述（完整的判断句，不复制记录原文）。

## 4. Scenario 结构（6.2 / 6.3）

每个 scenario 必须声明：`scenario_id`（全局唯一）、`scenario_version`、
`dataset_version`、`title`、`description`、`fixture`（夹具 id）、`request`
（target_type / target_key / analysis_type / as_of / time_scope / include 开关 /
selected_* / custom_start / custom_end / max_context_tokens）、`expected_facts`、
`required_sources`、`forbidden_claims`、`expected_uncertainties`、`human_review`、
`evaluation_dimensions`、`expected_context`、`notes`。

`as_of` 固定为带时区的显式时间（本数据集为 `2026-06-15T12:00:00+00:00`），
保证时间过滤可复现；所有夹具时间都是绝对 ISO 时间。

### 4.1 场景覆盖（A–J，17 个 Scenario）

| ID | 场景 | 验证重点 |
|---|---|---|
| SCENARIO_A_COMPLETE | 完整数据 | 来源引用、事实复述、观察/事实分离、未来事件排除 |
| SCENARIO_B_SPARSE | 资料稀疏 | 明确承认信息不足，不补写内容 |
| SCENARIO_C_NO_SOURCE | 缺少来源 | 不为看似合理的目标编造事件/证据 |
| SCENARIO_D_CONFLICT | 矛盾记录 | 识别并保留矛盾，不单方采信 |
| SCENARIO_E_EPISTEMIC | 认识论边界 | FACT / OBSERVATION / INFERENCE 不混淆 |
| SCENARIO_F_RELATIONSHIP_CHANGE | 关系变化 | effective_at、补录记录、未来生效（计划中）、当前状态 |
| SCENARIO_G_LAST_7/30/90_DAYS, _ALL, _CUSTOM | 时间范围 | 五种范围的过滤结果与快照绑定 |
| SCENARIO_H_EVIDENCE_OFF / _ON | Evidence 边界 | 元数据 / 原文 / 用户注释 / 附件元数据四者分离 |
| SCENARIO_I_MEMORY_DEFAULT / _ON | Memory 边界 | 默认关闭；开启后仅当前有效记忆进入上下文 |
| SCENARIO_J_NEAR_LIMIT / _OVER_LIMIT | 超长上下文 | 预算估算（≈90% 上限）、超限显式拒绝 |

## 5. 指标定义（6.4）

结果永远按指标分列：**状态（PASS / FAIL / N/A）+ 失败原因 + 证据/样本路径 +
产生方式（DETERMINISTIC / REFERENCE_BASED / HUMAN / HUMAN_ASSISTED）**。
没有单一总分；综合分数不得取代分项。N/A 表示"本次没有能力评估"，绝不表示通过。

| 指标 | 类型 | 计算方法 | 参考答案 | 人工 | 离线可用（阶段） |
|---|---|---|---|---|---|
| PARSE_VALIDITY | Deterministic | 13.1 解析器解析 raw output | 否 | 否 | 13.1 起可评估在线输出；离线数据集无模型输出 → N/A |
| REQUIRED_SECTIONS | Deterministic | 解析后必需小节检查（13.1） | 否 | 否 | 同上 |
| CITATION_VALIDITY | Deterministic | 引用 UUID/类型/Run/Workspace/曝光核对（13.2） | 否 | 否 | 13.2 起可验证在线输出（离线样例见 REFERENCE_GROUNDING） |
| CROSS_WORKSPACE_LEAKAGE | Deterministic | 输出引用跨 Workspace 拒绝（13.2） | 否 | 否 | 同上（WORKSPACE_MISMATCH 状态） |
| FACT_COVERAGE | Reference-based | 对照 expected_facts 比对 | 是 | 否 | 否（N/A） |
| UNSUPPORTED_CLAIMS | Reference-based / Human | 自动检查引用合法性 + 人工确认语义 | 是 | 是 | 否（N/A） |
| UNCERTAINTY_HANDLING | Reference-based / Human | 对照 expected_uncertainties + 人工 | 是 | 是 | 否（N/A） |
| EPISTEMIC_SEPARATION | Reference-based / Human | 分节归属 + 人工确认 | 是 | 是 | 否（N/A） |
| COMPLETENESS | Human-assisted | 人工对照任务范围记录 | 是 | 是 | 否（N/A） |
| ROBUSTNESS | Deterministic / Human | 上下文边界（本阶段）+ 输出稳定性（13.4） | 是 | 是 | 否（N/A） |
| DATASET_INTEGRITY | Deterministic | 加载期 fail-closed 校验 | 否 | 否 | **是** |
| CONTEXT_BINDING | Deterministic | required_sources ⊆ 快照来源集合 | 否 | 否 | **是** |
| PROMPT_LEAKAGE | Deterministic | 参考答案不在请求中（全串包含检查） | 否 | 否 | **是** |
| TOKEN_ESTIMATE_BOUND | Deterministic | 估算 ≤ 预算；超限显式拒绝 | 否 | 否 | **是** |
| CONTEXT_ISOLATION | Deterministic | 异 Workspace 记录文本与 UUID 双缺席 | 否 | 否 | **是** |
| CONTEXT_EXPECTATIONS | Deterministic | expected_context 逐条断言 | 否 | 否 | **是** |
| EXPOSURE_MANIFEST | Deterministic | Manifest ↔ source_ids / 持久化来源 / 上下文标记一致 | 否 | 否 | **是（13.1 起）** |
| REFERENCE_GROUNDING | Deterministic | 固定样例回答的引用状态与"缺引用"计数对照声明 | 是 | 否 | **是（13.2 起）** |

N/A 行必须带原因（如"结果解析器在 Phase 13.1 提供"）。**禁止**把人工评价项
伪装成程序已验证；**禁止**用 0 分代替 N/A。

## 6. 离线运行命令

```powershell
# 评估相关测试（数据集、契约、Runner、Token 估算边界）
& .\.venv\Scripts\python.exe -s -m pytest tests/evaluation tests/services/test_token_estimate.py -q -ra

# 离线运行整个（或指定）数据集，生成 manifest / raw-results / metrics / report
& .\.venv\Scripts\python.exe -m app.evaluation --dataset tests\evaluation\dataset --output evaluation-runs
& .\.venv\Scripts\python.exe -m app.evaluation --dataset tests\evaluation\dataset --scenario SCENARIO_A_COMPLETE
```

- Runner 使用真实 ContextBuilder / PromptBuilder，**不需要真实 Provider、不访问网络**；
- 每次运行有唯一 Run ID；`evaluation-runs/` 已列入 `.gitignore`（评估产物不入库）；
- 失败样本可单独定位：结果行带 `scenario_id` 与数据集文件路径（含内容摘要）。

## 7. 数据隐私规则

1. 评估数据集**只允许完全虚构数据**：清单与每个夹具必须显式 `synthetic: true`，
   加载器拒绝未声明的输入；`test_dataset.py` 另有联系数据标记（邮箱/URL/密钥样式）
   巡检作为绊线。
2. 加载器纯文件解析，不接触任何数据库；物化器只写调用方指定目录（默认用户数据
   目录不被触碰，有测试固定）。
3. 参考答案永不进入模型请求（§3.5 两层检查）。
4. 不得把真实用户数据写入评估产物；`evaluation-runs/` 不提交。
5. Token 估算、报告与日志不记录 API Key、Authorization、完整 HTTP 头或响应体。

## 8. Token 估算复核（6.5 结论）

复核对象：`app/services/context.py` 的 `estimate_tokens` 与 `max_context_tokens` 守卫。

**发现的问题**：原实现 `ceil(len(text)/2)`（`CHARS_PER_TOKEN=2`）附带注释声称
"CJK 约 1.5 字符/token，因此 2 字符/token 永不低估混合上下文"。按其自身数据推算：
CJK 实际约 `len/1.5 ≈ 0.67·len` tokens，而估算为 `0.5·len`——**低估约 25%**，
注释的数学方向是反的。英文（约 4 字符/token）反而高估 2 倍（安全方向）。

**已实施的保守策略**（`estimate_tokens`）：

- CJK / 全角字符（`unicodedata.east_asian_width ∈ {W, F}`）：**1 字符 = 1 token**；
- 其余字符：维持 2 字符/token（对英文仍是高估）；
- 空串为 0；混合文本按两类分别计数。

**守卫行为**（保持不变，符合"不允许静默截断"）：估算超过 `max_context_tokens`
时抛出带用户指引的 `DomainError`（提示缩小时间范围/取消勾选/减少选中），
拒绝构建而不是截断；边界精确到 1 token（`test_token_estimate.py` 固定）。

**仍然存在的局限**（如实声明）：

- 离线估算是**保守近似**，不是任何真实 tokenizer 的计数；emoji、罕见字形、
  代码/URL 密集文本仍可能偏离。真正的用量只能来自 Provider 的 usage 元数据
  （Phase 13.3 记录；输入估算值与实际值必须分开记录）。
- `max_context_tokens`（默认 24000）是**调用方预算**，应当按所选模型的上下文窗口
  扣除输出预留后设置；当前没有 Provider 级窗口自动配置（13.3 将结合元数据评估）。
- 没有引入 tokenizer 依赖（工程约束：不加大型评估框架/新依赖）。

## 9. 可复现性契约

同一数据集字节 + 同一 Scenario 重复运行：

- **掩码后逐字节复现**：渲染上下文、指标状态与结论、来源键绑定；
- **声明的易变值**（掩码后比较，`mask_volatile_text`）：`run_id`、运行时间戳、
  Memory 的 `confirmed_at`（渲染为「确认于 …」，由服务时钟决定）、物化行 UUID
  （Phase 13.1 起每个实体在上下文中带 `[TYPE:<uuid>]` 标记，标记即唯一的
  UUID 来源）。因此原始摘要 `rendered_digest` 天然逐次不同，跨运行比较用
  `masked_digest`；逻辑内容（键、文本、固定时间、结构、曝光等级）必须一致。

`EvaluationRunResult.volatile_fields` 在每次运行结果中显式声明上述字段；
报告中的 `masked_digest` 用于跨运行比较，`rendered_digest` 保留原始字节摘要。

## 10. 来源曝光契约（Source Exposure Contract，13.0 补充决策）

> 本契约于 13.0 收口时由项目负责人补充确认：采用**扩展 AnalysisSource 覆盖范围 +
> 记录实体曝光程度**的方案；不采用"维持旧来源集合、在 13.2 临时增加特殊人物引用
> 类别"的方案，也不允许无差别扩大来源集合。实现分别在 13.1（Manifest 联动）与
> 13.2（正式验证器），本文件此节只把决策纳入正式评估契约。

### 10.1 核心语义

- **AnalysisSource** 表示某数据库实体在本次 Analysis 的 ContextSnapshot 中被
  **明确提供或表示**；
- AnalysisSource 成员资格**不代表**该实体完整资料已提供，也**不代表**模型关于
  该实体的结论已经有依据；
- 引用合法性（13.2）与自然语言结论的语义支持是两件事，后者只能人工复核。

### 10.2 Context Entity Manifest（13.1 实现）

引入版本化的 **Context Entity Manifest**，记录实体类型、UUID、上下文角色与
**实际曝光程度**；同一份 Manifest 必须同时驱动：

1. ContextSnapshot 中的实体 ID 标记；
2. `source_ids()`；
3. `AnalysisService._persist_sources()`（`analysis_*_source` 四表）；
4. Phase 13.2 的来源验证。

曝光枚举**按实体类型区分**（不允许把所有曝光等级混为一个通用状态）：

| 实体类型 | 曝光等级 | 含义 |
|---|---|---|
| PERSON | `PROFILE` | 提供了完整人物档案行（姓名/职业等） |
| PERSON | `IDENTITY_ONLY` | 仅以姓名/身份出现（如关系端点），未提供档案 |
| PERSON | `PARTICIPANT_CONTEXT` | 以事件参与人身份出现（含角色/备注） |
| RELATIONSHIP | `RELATIONSHIP_FACT` | 提供了关系类型/方向等关系事实 |
| EVENT | `EVENT_DETAIL` | 提供了事件明细行 |
| EVIDENCE | `METADATA_ONLY` | 仅提供证据元数据，原文/注释未发送 |
| EVIDENCE | `CONTENT_INCLUDED` | 提供了证据原文（含用户明确开启的情形） |
| MEMORY | `USER_CONFIRMED_CONTENT` | 提供了用户确认的 Memory 内容 |

### 10.3 来源生成规则

- 实际提供身份信息的人物进入 Person 来源集合（`PROFILE`）；
- 只以姓名出现的关系端点以 `IDENTITY_ONLY` 进入 Person 来源集合；
- 实际出现的事件参与人以 `PARTICIPANT_CONTEXT` 进入 Person 来源集合；
- 未在上下文中出现、也没有可识别身份标记的实体**不进入**来源集合；
- 不得扫描整个 Workspace 把所有人物加入来源集合；
- 同一实体在多个上下文区域出现时**合并角色并记录实际曝光等级**。

### 10.4 Prompt 可识别性

所有允许被结构化引用的实体，必须在实际发送给模型的上下文里有明确的类型与 UUID
标记（如 `[PERSON:<uuid>] 鲍勃（仅身份信息，未提供完整档案）`）；不得只把 UUID
写入数据库而不暴露给模型，也不得允许模型引用未出现在 Manifest 中的 ID。
对身份有限曝光的实体，上下文必须明确告知本次没有提供其完整档案。

### 10.5 Phase 13.2 验证状态（本阶段不实现）

引用验证至少区分：`VALID_FULL`、`VALID_LIMITED`、`INVALID_FORMAT`、
`UNKNOWN_SOURCE`、`NOT_IN_RUN`、`WORKSPACE_MISMATCH`、`TYPE_MISMATCH`、
`SOURCE_NOT_EXPOSED`。`MemoryService.candidate_references()` 与 AnalysisPage 候选
引用展示必须使用一致的验证结果，不得保留只返回 accepted Boolean 的过时语义。
**引用合法不等于语义有据。**

### 10.6 历史兼容与数据库边界

- 不修改历史 AnalysisRun、不回填旧 AnalysisSource、不重建历史 ContextSnapshot；
- 旧 Snapshot 没有 Manifest 时沿用旧来源验证规则，不自动升级为 `VALID_FULL`；
- 新 Manifest 带版本标识；
- **优先复用已有四类 `analysis_*_source` 表**；本决策不要求新增表或 Alembic
  migration；若实现中发现必须新增字段，先报告原因与最小迁移方案再动手。

### 10.7 评估契约影响（13.1 已落地）

- 指标注册表新增 `EXPOSURE_MANIFEST`（确定性：Manifest 与 source_ids / 持久化
  来源 / 上下文标记一致性）；**13.1 已启用**，离线运行对每个场景检查；
- 数据集把 `IDENTITY_ONLY` / `PARTICIPANT_CONTEXT` 人物重新纳入
  `required_sources`，并以 Manifest 断言其曝光等级；
- 决策 §7 要求的测试在 13.1/13.2 分阶段落地：Manifest／标记／持久化一致性、
  历史兼容、多角色合并、Evidence 曝光诚实性已完成（`tests/services/test_entity_manifest.py`）；
  候选引用"有限信息状态"属于 13.2 验证器范围，尚未实现。

## 11. 已知局限

1. **模型输出指标**：13.1 提供解析器（`PARSE_VALIDITY` / `REQUIRED_SECTIONS` 可评估
   在线输出），13.2 提供引用验证器（`CITATION_VALIDITY` / `CROSS_WORKSPACE_LEAKAGE`
   可验证在线输出；离线运行用 `REFERENCE_GROUNDING` 对三个 Scenario 的固定样例回答做
   确定性验证）。`FACT_COVERAGE` 等语义对照仍待人工/后续；
2. ~~来源集合边界（既有契约）~~ **已由 13.0 补充决策取代**：采用 §10 的来源曝光
   契约——次要人物按 `IDENTITY_ONLY` / `PARTICIPANT_CONTEXT` 进入来源集合并由
   Manifest 记录曝光程度（13.1 实现）。13.0 数据集在 13.1 完成前按旧契约编写。
3. **语义质量不可自动判定**：推断是否合理、两份材料是否构成矛盾、语言是否夸大
   ——正则和关键词扫描不能证明语义，这些一律留给人工（Scenario 的 `human_review`）。
4. **LLM Judge 不是裁判**：后续阶段若引入 Judge，只能作为实验工具，结论与原始
   输出、自动评估、人工复核四者分开保存，不写回事实层。
5. **在线评估未实现**（13.4 范围）：真实模型对比必须由用户显式触发，固定
   Dataset / Scenario / ContextSnapshot / Prompt 版本后才可比较。
6. J 场景的批量记录是**合成体积**（13.1 带实体标记重新校准后 J_NEAR 估算
   21524 / 24000 ≈ 90%；13.0 时为 21527），只用于验证预算行为，不代表真实数据分布。

## 12. 变更记录

| 阶段 | 内容 |
|---|---|
| 13.0 | 建立评估框架、合成数据集（A–J / 17 Scenario）、指标注册表、离线 Runner、Token 估算保守化 |
| 13.0 补充 | 来源曝光契约（Source Exposure Contract）纳入正式评估契约；`EXPOSURE_MANIFEST` 指标登记（N/A 待 13.1） |
| 13.1 | 结构化结果契约 + 解析器；Context Entity Manifest 联动（标记 / source_ids / 持久化 / 数据集 required_sources）；`EXPOSURE_MANIFEST` 启用；可复现性改为掩码后逐字节比较 |
| 13.2 | 引用验证器（9 种状态，Manifest + AnalysisSource 联合校验）；候选引用状态化（移除 accepted 布尔）；结果对话框逐条验证；数据集 1.1 + 样例输出；`REFERENCE_GROUNDING` 启用 |
