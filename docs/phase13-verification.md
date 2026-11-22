# Phase 13 验收记录（Analysis Quality & Evaluation）

> 本文件按子阶段追加。历史阶段（Phase 1–12.5）的验收记录在各自文件中，**不修改**。

分支：`phase-13-analysis-quality-evaluation`（起点：Phase 12.5 稳定基线 `7e8819b`）。

---

## Phase 13.0 — Evaluation Framework & Dataset

**目标**：建立可重复、可离线运行的 Analysis 评估基础设施：合成评估数据集（A–J）、
版本化评估契约、指标注册表、离线 Runner、Token 估算复核。

### 完成内容

1. **离线评估框架**（`app/evaluation/`，全新包，7 个模块）：
   - `models.py`：评估契约 DTO（EvaluationDataset / EvaluationScenario / RequestSpec /
     ExpectedContext / ExpectedFact / ForbiddenClaim / ExpectedUncertainty /
     HumanReviewItem / 夹具规格）、16 项指标注册表、结果结构（MetricResult 带
     PASS/FAIL/N/A + 原因 + 证据 + 产生方式）；
   - `dataset.py`：fail-closed 加载器（schema 版本、ID 唯一、引用/类型完整性、
     合成来源声明、未知字段拒绝、内容摘要），纯文件解析——无数据库、无网络、无 Qt；
   - `fixture.py`：合成夹具物化器——**全部经真实 Service** 写入评估专属临时库
     （`initialize_database` + `create_application_services`），绝不触碰生产数据目录；
   - `runner.py`：离线 Runner——真实 ContextBuilder / PromptBuilder，确定性检查
     DATASET_INTEGRITY / CONTEXT_BINDING / PROMPT_LEAKAGE / TOKEN_ESTIMATE_BOUND /
     CONTEXT_ISOLATION / CONTEXT_EXPECTATIONS；模型输出指标一律 N/A + 原因；
   - `report.py`：manifest.json / raw-results.json / metrics.json / report.md；
   - `__main__.py`：`python -m app.evaluation --dataset … --output …` 离线入口。
2. **合成评估数据集**（`tests/evaluation/dataset/`，dataset_version 1.0）：
   10 个虚构夹具 + 17 个版本化 Scenario，覆盖 A 完整数据、B 资料稀疏、C 缺少来源、
   D 矛盾记录、E FACT/OBSERVATION/INFERENCE 边界、F 关系变化（ACTIVE/PAUSED/ENDED/
   补录/未来生效）、G 五种时间范围（7/30/90/ALL/CUSTOM）、H Evidence 边界
   （元数据/原文/用户注释/附件元数据）、I Memory 边界（默认关闭/开启/过期/未生效）、
   J 超长上下文（近限 21527/24000 ≈ 90% 与超限显式拒绝；13.1 带标记后重校准为
   21524，见 13.1 小节）。
3. **Token 估算复核（6.5）与修复**：原 `CHARS_PER_TOKEN=2` 的注释声称"永不低估 CJK"，
   实际按其自身 1.5 字符/token 数据低估约 25%。已改为保守策略（CJK/全角 1 字符=1 token，
   其余 2 字符/token），边界测试固定"超预算显式拒绝、不静默截断"。详见
   [phase13-evaluation.md](phase13-evaluation.md) §8。
4. **文档**：`docs/phase13-evaluation.md`（新建）、`docs/analysis.md`（新增"离线评估"节）。

### 修改文件

- 新增：`app/evaluation/{__init__,__main__,models,dataset,fixture,runner,report}.py`、
  `tests/evaluation/{test_dataset,test_evaluation_contract,test_evaluation_runner}.py`、
  `tests/evaluation/dataset/`（1 清单 + 10 夹具 + 17 场景）、
  `tests/services/test_token_estimate.py`、`docs/phase13-evaluation.md`
- 修改：`app/services/context.py`（estimate_tokens 保守化 + 注释修正）、
  `docs/analysis.md`、`.gitignore`（忽略 `evaluation-runs/`）
- 删除：无

### 边界与数据库变更

- Domain / Service / Repository / UI 边界：**无变化**。评估层为只读消费者
  （ContextBuilder / PromptBuilder / Service 物化合成数据到评估专属临时库）。
- 数据库：**无迁移、无 schema 变化**。评估运行不写生产库（测试固定）。
- 事实保护：评估不改任何事实、不建 Memory、不发网络请求、不调用模型。

### 新增测试（41 项）

- `tests/services/test_token_estimate.py`（8 项）：比率常量、空串/单字符、ASCII 保守、
  CJK 保守（含旧算法回归）、混合文本、全角/假名、守卫边界（预算=估算通过、-1 拒绝）、
  中文上下文计数。
- `tests/evaluation/test_dataset.py`（12 项）：加载与 A–J 覆盖、ID 唯一、合成声明、
  批量展开确定性、摘要稳定/内容敏感、schema/重复 ID/版本不符/未知来源 key/来源类型
  不符/未知指标/缺失 synthetic/未知夹具/未知字段拒绝、无真实联系数据标记、
  加载不触碰数据库。
- `tests/evaluation/test_evaluation_contract.py`（7 项）：6.3 契约字段、fact_id 唯一、
  指标注册表（10 项必含）、模式/N-A 政策、维度合法、参考答案不进夹具数据、
  参考答案与 required_text 不混用。
- `tests/evaluation/test_evaluation_runner.py`（14 项）：17 场景离线运行与预期结果、
  无失败、13.0 指标 PASS + 模型指标 N/A、FAIL≠N/A（含失败样本定位）、可复现性
  （掩码后一致 + 无 UUID 场景原始一致）、来源绑定、近限预算、超限显式拒绝、
  无网络访问、不触碰生产库。

### 测试迁移

无删除、无改写。5 项 Windows symlink 环境 Skip（`tests/storage/test_media_store.py:275`）
保持原义，未伪装为通过。

### 测试运行命令与结果

基线（Phase 13.0 开工前，分支创建后即运行）：

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

- 1516 collected / 1511 passed / 5 environment-skipped / 0 failed（退出码 0），
  与 Phase 12.5 验收记录一致。

13.0 完成后全量回归：

- **1557 collected / 1552 passed / 5 environment-skipped / 0 failed（退出码 0）**
- 5 项 skipped 仍为 `tests/storage/test_media_store.py:275` 的 Windows symlink 环境 Skip。
- 增量核对：1516（基线）+ 41（新增）= 1557 ✓，无新增失败。

新增测试子集：

```powershell
& .\.venv\Scripts\python.exe -s -m pytest tests/evaluation tests/services/test_token_estimate.py -q -ra
```

- 41 collected / 41 passed / 0 skipped / 0 failed（退出码 0）。

### Git 提交

| Commit | 说明 |
|---|---|
| `c9c1282` | fix(analysis): conservative CJK token estimate in ContextBuilder |
| `48c255f` | feat(evaluation): establish offline evaluation framework |
| `ea83598` | test(evaluation): add synthetic analysis scenarios |
| `5409e7c` | docs(evaluation): define evaluation dataset and metrics |
| （本文档提交） | docs(phase13): record Phase 13.0 verification |

### 与原始计划不同的实现及理由

1. **数据集目录调整为 `tests/evaluation/dataset/{fixtures,scenarios}`**（原建议
   `fixtures/ scenarios/ expected/` 平铺）：expected 内容并入 scenario 契约字段
   （expected_facts 等本就是 6.3 契约的一部分），避免同一信息两处存放。
2. **`included/excluded` 只断言来源集合成员**，次要人物改用 `required_text` 断言：
   现行 `source_ids()`（进而 `analysis_*_source`）不含关系端点/参与人（见下节）。
3. **可复现性掩码扩展到物化行 UUID**：渲染文本含 `id=…`/`event=…` 的运行期随机
   UUID，与时间戳一并声明为易变值；逻辑内容（key/文本/固定时间/结构）逐字节复现，
   无 UUID 的场景原始摘要也完全一致。
4. **Scenario J 增加 bulk_events/bulk_evidence 确定性批量展开**（上限 500/组），
   避免在 JSON 中手写上百条重复记录；展开规则确定、有测试固定。

### 已知缺陷 / 需要项目负责人决策的发现

**发现：来源集合不含次要人物（既有语义边界，非 13.0 引入）**

- 真实代码路径：`ContextBuilder._person_context` 只把目标人物写入
  `scope["person_ids"]`（关系端点以"对方=…"文本出现；`_event_context` 的
  person_ids 是参与人，但 PERSON 目标的事件参与人不入 person_ids）；
  `AnalysisService._persist_sources`（`app/services/analysis.py`）按
  `snapshot.source_ids()` 落 `analysis_*_source`。
- 影响：模型上下文中可见鲍勃/凯萝等次要人物，但按 Phase 13.2 的验证规则
  （"必须属于本次 Analysis Sources"）他们的 UUID 将**无法被合法引用**。
- 直接照提示词把次要人物放进 required_sources 会造成评估永远失败的假象。
- 最小变更替代方案（**本阶段未实施，等待决策**）：(a) 13.2 引用校验把"上下文文本
  可见的次要人物"作为受限引用类别；或 (b) 扩展 `source_ids()` 纳入参与人/端点
  （需同步调整 `tests/services/test_context_builder.py` 的既有断言与来源语义文档）。
  两者都是语义变化，按约定先报告。

其余已知局限（N/A 指标、离线估算非真实 tokenizer、语义质量需人工）见
[phase13-evaluation.md](phase13-evaluation.md) §10。

### 尚未完成的工作（属后续子阶段）

- 13.1 结构化结果契约与解析器；13.2 来源引用校验；13.3 可追溯性与元数据；
  13.4 多模型评估与人工复核。本阶段不提前实现其功能。

### 下一阶段（13.1）的前置条件

- ✅ 评估框架与数据集就绪（本阶段）。
- ✅ 13.0 的 runner 为 13.1 预留解析器接入点：PARSE_VALIDITY / REQUIRED_SECTIONS
  指标已在注册表声明（当前 N/A），raw output 保留原则已写入契约文档。
- ⚠️ 13.1 动工前建议先对"来源集合边界"（上节）给出决策，因其影响 13.2 的引用
  校验语义，而 13.1 的结构化契约中 references 字段与之相邻。

---

## Phase 13.0 补充决策 — Source Exposure Contract

项目负责人在 13.0 收口时补充确认：采用**扩展 AnalysisSource 覆盖范围 + Context
Entity Manifest 记录实体曝光程度**的方案（不使用"旧来源集合 + 特殊人物引用类别"，
也不允许无差别扩大来源集合）。详见 [phase13-evaluation.md §10](phase13-evaluation.md)。

**本阶段（13.0）完成的契约形式化**（纯契约，无行为变化）：

- `docs/phase13-evaluation.md` 新增 §10 来源曝光契约：核心语义、Manifest 字段与
  按实体类型的曝光枚举、来源生成规则、Prompt 可识别性、13.2 验证状态、历史兼容、
  数据库边界（复用四类来源表、无 migration）、评估契约影响；
- 指标注册表新增 `EXPOSURE_MANIFEST`（确定性；13.0 为 N/A，13.1 启用），
  注册表规模 16 → 17；§5 指标表与 §12 变更记录同步；
- 已知局限 §11.2 的"来源集合边界"由本决策取代，实现分阶段：13.1 = 结构化引用
  契约 + Manifest 联动；13.2 = 正式验证器（`VALID_LIMITED` 等状态、候选引用一致性）。

**兼容性确认**：补充内容为文档与注册表条目，不改变任何运行时行为；
`tests/evaluation` + `test_token_estimate` + `test_context_builder` 共 58 项通过。

**结转**：13.1 需把 `[TYPE:<uuid>]` 标记写入渲染上下文、Manifest 驱动
`source_ids()`/`_persist_sources()`、数据集恢复次要人物 `required_sources` 并启用
`EXPOSURE_MANIFEST`；13.2 实现验证状态与候选引用界面一致的有限信息展示。

---

---

## Phase 13.1 — Structured Analysis Contract + Context Entity Manifest

**目标**：把非结构化文本回答升级为可解析、可验证、可显示的结构化结果契约；同时按
13.0 补充决策实现 **Context Entity Manifest 联动**（标记 / source_ids / 持久化来源 /
数据集）。**不实现 13.2 的正式验证器**（`VALID_LIMITED` 等状态、候选引用一致性）。

### 完成内容

1. **结构化结果契约与解析器**（`app/domain/entities/analysis_result.py`、
   `app/services/analysis_result_parser.py`）：
   - DTO：`AnalysisResult`（raw_text / schema_version / parse_status / 四节 entries /
     unparsed_content / warnings）、`AnalysisEntry`（content / references /
     confidence_description / notes）、`AnalysisReference`（type + UUID）；
   - 解析器纯 Python（无 Qt / DB / LLM / 网络）：合法 JSON、夹杂文本/代码围栏、
     缺字段、类型错误、非法引用、重复条目、多语言标题、旧版 Markdown、纯文本、
     空输出、超大输出；`parse_status` 六态；未识别内容进 `unparsed_content`，
     告警进 `warnings`，**原文永不改写**。
2. **Prompt 输出契约**：输出模板改为 JSON schema 1（四个数组 + references +
   confidence_description），系统提示要求引用只能使用上下文中的 `[TYPE:<uuid>]`
   标记且不得编造 UUID；旧版四段 Markdown 仍可解析（历史结果与 Fake 响应兼容）。
3. **Context Entity Manifest**（`app/services/entity_manifest.py` + `context.py`）：
   - 版本化 Manifest（`version="1"`）记录实体类型、UUID、角色与**按类型**的曝光等级
     （PERSON：PROFILE / IDENTITY_ONLY / PARTICIPANT_CONTEXT；EVENT：EVENT_DETAIL /
     EVENT_REFERENCE；EVIDENCE：METADATA_ONLY / CONTENT_INCLUDED；RELATIONSHIP /
     MEMORY 各自枚举）；
   - 渲染上下文为每个实体写入 `[TYPE:<uuid>]` 标记；有限曝光人物注明"仅身份信息/
     参与人信息，未提供完整档案"；
   - `ContextSnapshot.source_ids()` 与 `included_memory_ids` 改由 Manifest 驱动；
     `_persist_sources()` 未改代码即自动落到四张 `analysis_*_source`；
   - 多维排序改用"去掉标记后的文本"作 key，保证行序不随随机 UUID 抖动；
   - 旧 Snapshot（无 Manifest）回退旧 scope 规则，不回填、不修改历史 Run；
   - 发现并处理 `history.event_id` 仅以链接出现的情形：渲染为
     `[EVENT:<uuid>]`，曝光等级 `EVENT_REFERENCE`，保证上下文无未标记 UUID。
4. **UI**（`analysis_dialog.py` / `analysis_page.py`）：结果对话框默认展示结构化
   小节（引用标注"引用（未校验）"），新增"模型原始输出"只读区、解析状态与解析提示
   （提示不视为模型失败）；解析失败降级为原文展示；页面详情展示结构化摘要与
   解析状态；旧答案按旧版四段渲染，行为不变。
5. **评估层**：`EXPOSURE_MANIFEST` 指标启用（Manifest ↔ 标记 ↔ source_ids ↔ 一致性、
   Evidence 曝光不夸大、无未标记 UUID）；数据集恢复 `IDENTITY_ONLY` /
   `PARTICIPANT_CONTEXT` 人物到 `required_sources`；J 场景按标记后的体积重新校准
   （85 个批量事件 / 25 条批量证据）；可复现性改为"掩码后逐字节一致"（标记本身是
   运行期 UUID）。

### 修改文件

- 新增：`app/domain/entities/analysis_result.py`、`app/services/analysis_result_parser.py`、
  `app/services/entity_manifest.py`、`tests/services/test_analysis_result_parser.py`、
  `tests/services/test_entity_manifest.py`
- 修改：`app/services/context.py`（Manifest/标记/source_ids/渲染排序）、
  `app/services/prompt.py`（JSON 契约）、`app/ui/analysis_dialog.py`、
  `app/ui/analysis_page.py`、`app/evaluation/models.py`（EXPOSURE_MANIFEST 启用）、
  `app/evaluation/runner.py`（Manifest 检查）、`app/evaluation/report.py`、
  `tests/evaluation/dataset/scenarios/*`（required_sources/included）、
  `tests/evaluation/dataset/fixtures/fixture-j-large.json`（体积校准）、
  `tests/evaluation/test_dataset.py`、`tests/evaluation/test_evaluation_runner.py`、
  `tests/services/test_analysis_service.py`、`tests/ui/test_analysis_ui.py`、
  `docs/analysis.md`、`docs/phase13-evaluation.md`
- 删除：无

### 边界与数据库变更

- Domain / Service / UI 边界：保持（UI→Service→Repository；解析器无副作用）。
- **无数据库迁移、无 schema 变化**：Manifest 存入现有 `analysis_run.context_snapshot`
  JSON（新增 `entity_manifest` 键，带版本）；旧行读取时回退。
- 事实保护：分析运行不改任何事实行（测试固定 Person/Relationship/Event/Evidence 计数）；
  Manifest 只描述"模型看到了什么"，不代表结论有依据。

### 新增/修改测试

- 新增 24 项：`test_analysis_result_parser.py`（14）——JSON 契约、围栏/夹杂、缺字段、
  类型错误、非法引用、重复条目、未知字段、字符串条目、旧版 Markdown、纯文本、
  空/超大、确定性、未校验展示；`test_entity_manifest.py`（10）——PERSON/RELATIONSHIP/
  EVENT 曝光矩阵、异 Workspace 隔离、Manifest↔标记↔持久化一致性、多角色合并、
  Evidence 曝光诚实、版本与回环、旧快照兼容且不被改写。
- 修改（迁移）6 个测试文件：`test_analysis_service.py`（来源集合含 IDENTITY_ONLY
  端点；提示词断言改为 JSON 契约）；`test_analysis_ui.py`（新增 2 项结构化 UI 测试：
  小节/原始输出/解析状态；解析告警可见但非失败）；`test_evaluation_runner.py`
  （可复现性掩码语义）；`test_dataset.py`（J 批量数量）；`test_export_service.py`、
  `test_memory_service.py`（来源集合含端点，未削弱任何事实断言）。
- 数据集：A/B/C/J 场景恢复次要人物 `required_sources`；J 批量体积校准。
- 5 项 Windows symlink 环境 Skip 保持原义。

### 测试运行命令与结果

子集：

```powershell
& .\.venv\Scripts\python.exe -s -m pytest tests/services/test_entity_manifest.py tests/services/test_analysis_result_parser.py tests/services/test_context_builder.py tests/services/test_analysis_service.py tests/evaluation tests/ui/test_analysis_ui.py -q -ra
```

全量回归（13.1 完成后）：

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

- **1583 collected / 1578 passed / 5 environment-skipped / 0 failed（退出码 0，8:22）**
- 增量核对：1557（13.0 完成后）+ 26（13.1 新增：解析器 14 + Manifest 10 + UI 2）
  = 1583 ✓；5 项 Windows symlink 环境 Skip 保持原义。

### Git 提交

| Commit | 说明 |
|---|---|
| `120feea` | feat(analysis)：结构化结果契约 + 解析器 + Prompt 契约 + UI |
| `34a2cf4` | feat(analysis)：Context Entity Manifest 驱动来源与标记（含评估层与测试迁移） |
| （本文件提交） | docs(phase13)：记录 Phase 13.1 |

### 与原始计划不同的实现及理由

1. **Manifest 存入 `context_snapshot` JSON，不新增表/列**：符合决策"优先复用四类
   来源表、无 migration"；旧快照无 Manifest 时回退旧规则。
2. **`EVENT_REFERENCE` 曝光等级**（决策列表之外）：历史行可能只暴露事件 UUID 链接
   （`history.event_id`，现有数据可经备份恢复产生）；不新增等级会迫使渲染出未标记
   UUID 或虚构 `EVENT_DETAIL` 曝光。按类型枚举的原则允许该最小扩项。
3. **可复现性改为掩码比较**：13.1 起实体标记本身含运行期 UUID，原始渲染文本不可能
   逐字节稳定；`masked_digest` 比较 + 显式易变字段声明是唯一诚实方案。
4. **解析状态/结构化结果不单独持久化**：从原始 `result` 文本在内存中解析（决策
   7.8 的默认方案），避免出现第二个事实来源；需要重解析时结果可重复。

### 已知缺陷 / 结转 13.2

- **引用验证器未实现**：解析出的 references 只是结构，尚未核对存在性/归属/类型/
  曝光等级；UI 明确标注"引用（未校验）"。
- **候选引用语义过时（13.2 范围）**：`MemoryService.candidate_references()` 仍只扫描
  原始文本中的 `[TYPE:uuid]` 标记并返回 `accepted` 布尔；结构化 JSON 输出中的
  references 不会被它识别，也未区分 `VALID_FULL`/`VALID_LIMITED`。这与决策 §4
  一致地留给 13.2 统一（不得保留过时语义）。
- **Prompt 层未启用 provider `response_format`**：仅提示词约定；Provider 能力声明
  与响应元数据属 13.3/13.4。
- 结构化解解析对超大输出只保留原文（`TOO_LARGE`），不做分段解析。

### 下一阶段（13.2）的前置条件

✅ 结构化 references 与 Manifest 就绪；`source_ids()`/持久化来源由同一 Manifest 驱动；
✅ 旧快照兼容路径有测试；✅ 暴露等级（含 EVENT_REFERENCE）可查询。
13.2 需要：8 种验证状态、Manifest+AnalysisSource 联合校验、候选引用 API 状态化
（替换 accepted Boolean）并与 AnalysisPage 展示一致、有限曝光样本 UI 状态。

---

## Phase 13.2 — Source Grounding & Verification

**目标**：让分析中的来源引用拥有明确、可重复验证的依据；引用合法性必须由真实
AnalysisRun + AnalysisSource + Context Entity Manifest 共同判定，且失败原因不可合并为
一个布尔值。语义支持（推断是否合理、矛盾是否成立）仍归人工，不由确定性规则冒充。

### 完成内容

1. **引用验证器**（`app/services/reference_verification.py` + 领域模型
   `app/domain/entities/reference_validation.py`）：`ReferenceVerifier` 组合
   `AnalysisSource`（本次运行来源集合）+ `Context Entity Manifest`（曝光等级）+
   跨 Workspace 只读查找（`ReferenceLookupRepository`），输出 9 种互斥状态及原因：
   `VALID_FULL`、`VALID_LIMITED`、`INVALID_FORMAT`、`UNKNOWN_SOURCE`、`NOT_IN_RUN`、
   `WORKSPACE_MISMATCH`、`TYPE_MISMATCH`、`DELETED_SOURCE`、`SOURCE_NOT_EXPOSED`。
   - 跨 Workspace 判定与"不存在"严格区分；类型伪造（用 Person UUID 冒充 EVENT）单列；
   - 删除状态先于来源归属；旧快照无 Manifest → 只确认来源归属并标 `VALID_LIMITED`，
     **不升级为 VALID_FULL**、不回填；
   - 查询只读：验证过程写零行（测试固定）。
2. **候选引用状态化**：`MemoryService.candidate_references()` 改用同一验证器，同时覆盖
   结构化 JSON references 与旧版 `[TYPE:<uuid>]` 标记；返回 `ValidatedReference`
   （status + reason），**移除 accepted 布尔**；旧 `CandidateReference` 类删除。
3. **UI**：结果对话框从 Service 取 `EntryReferenceCheck`，逐条显示引用状态与原因
   （元数据行给出"完整/有限/无效"计数），不再出现"未校验"占位；候选引用表显示
   "有效（完整曝光）/ 有效（有限曝光）/ 无效（状态 · 原因）"，只有有效行可跳转，
   有限曝光行明确标注"仅能引用所示信息"。
4. **评估集成（8.7）**：数据集 1.1 为 A/D/G7 增加固定样例回答（`sample_output`，
   `<<TYPE:key>>` 占位符在运行期解析为真实 UUID）；新增
   `REFERENCE_GROUNDING` 确定性指标：对照场景声明的期望状态，自动检测真实引用、
   有限曝光、跨 Workspace、类型错误、未曝光、未知 UUID 与 FACT 条目缺引用数量。
5. **文档**：`docs/analysis.md`、`docs/memory.md`、`docs/phase13-evaluation.md`。

### 修改文件

- 新增：`app/domain/entities/reference_validation.py`、
  `app/services/reference_verification.py`、`app/repositories/reference_interfaces.py`、
  `app/storage/reference_repositories.py`、`tests/services/test_reference_verification.py`
- 修改：`app/domain/entities/{__init__,memory}.py`、`app/services/{analysis,
  analysis_repositories,memory}.py`、`app/ui/{analysis_dialog,analysis_page}.py`、
  `app/evaluation/{__init__,models,dataset,runner}.py`、数据集（18 个 JSON，v1.1）、
  `tests/evaluation/{test_dataset,test_evaluation_contract,test_evaluation_runner}.py`、
  `tests/services/test_memory_service.py`、`tests/ui/{test_analysis_ui,test_memory_ui}.py`、
  `docs/{analysis,memory,phase13-evaluation}.md`
- 删除：无（`CandidateReference` 类被状态化 DTO 取代）

### 边界与数据库变更

- 新增只读仓储契约 `ReferenceLookupRepository`（跨 Workspace 存在性查询）+ SQL 适配器；
  **无数据库迁移、无 schema 变化、无新表**。
- 验证只读：不改事实、不写 AnalysisSource、不写 Memory、不回填历史 Run。
- 验证器是候选引用 / 对话框 / 评估 Runner 的**唯一**判定来源（决策 §2 单一 Manifest、
  §4 一致验证结果）。

### 新增测试（15 项）

- `test_reference_verification.py`（9）：VALID_FULL/LIMITED 按曝光等级、
  INVALID_FORMAT/UNKNOWN_SOURCE/TYPE_MISMATCH、WORKSPACE_MISMATCH、DELETED_SOURCE、
  SOURCE_NOT_EXPOSED、旧快照 NOT_IN_RUN 且不升级、逐条目多引用、只读且无法伪造状态、
  候选服务与验证器一致。
- `test_dataset.py`（+3）：样例输出与夹具绑定、未知占位符拒绝、期望引用必须出现在样例内容中。
- `test_evaluation_runner.py`（+2）：样例引用状态全覆盖 PASS、错误期望 → FAIL 且原因可读。
- `test_analysis_ui.py`（+1）：真实引用在结果对话框中显示 `VALID_FULL` 与计数。

### 测试迁移（4 个文件）

`test_memory_service.py`（2 项：状态化断言）、`test_memory_ui.py`（2 项：重命名 + 有限曝光
展示/可跳转断言）、`test_analysis_ui.py`（结构化用例改为断言验证状态、新增真实引用用例）、
评估测试（数据集 v1.1 契约与指标计数 17→18）。未削弱任何事实/隐私断言；
5 项 Windows symlink 环境 Skip 保持原义。

### 测试运行命令与结果

子集（服务层 + 受影响 UI + 评估）：

```powershell
& .\.venv\Scripts\python.exe -s -m pytest tests/services tests/evaluation tests/ui/test_analysis_ui.py tests/ui/test_memory_ui.py tests/ui/test_phase11_analysis_memory_settings.py -q -ra
```

- 515 passed / 0 failed（2:39）。

全量回归（13.2 完成后）：

- **1598 collected / 1593 passed / 5 environment-skipped / 0 failed（退出码 0，8:29）**
- 增量核对：1583（13.1 完成后）+ 15（13.2 新增：验证器 9 + 数据集 3 + 评估 2 +
  UI 1）= 1598 ✓；5 项 Windows symlink 环境 Skip 保持原义。

### Git 提交

| Commit | 说明 |
|---|---|
| `89d8124` | feat(analysis)：共享引用验证状态 + 候选引用状态化 + 对话框/UI + 评估集成 |
| `17d6fb3` | test(analysis)：验证状态全覆盖、候选展示与 grounding 评估测试 |
| （本文件提交） | docs(phase13)：记录 Phase 13.2 |

### 与原始计划不同的实现及理由

1. **新增只读 `ReferenceLookupRepository`**：区分 WORKSPACE_MISMATCH 与 UNKNOWN_SOURCE
   需要跨 Workspace 存在性；现有事实仓储都是 workspace 作用域。该契约为只读、无迁移，
   是 8.2 要求的必要最小实现。
2. **`EVENT_REFERENCE` 已存在的曝光等级直接参与状态映射**（EVENT_REFERENCE →
   VALID_LIMITED），无需额外状态。
3. **数据集版本 1.1 而非 1.0**：新增可选 `sample_output` 结构并修改内容，按契约递增
   dataset_version；schema_version 不变（新增字段向后兼容）。
4. **对话内验证展示**：13.2 决策只点名候选引用一致性，但解析后的结果同样需要可解释
   依据；复用同一 Service 方法（`AnalysisService.reference_checks`）而非在 UI 判定。

### 已知局限 / 结转

- **语义支持仍不可自动判定**：`VALID_*` 只证明引用合法；句子是否被材料支持、两份
  材料是否矛盾、因果是否成立仍为人工复核（Scenario `human_review` 与 8.6 边界）。
- `FACT_COVERAGE` / `UNCERTAINTY_HANDLING` / `EPISTEMIC_SEPARATION` 等参考答案指标
  仍为 N/A（需参考答案比对/人工；13.4 在线评估时接入）。
- 离线运行只有 3 个 Scenario 带固定样例回答；其余场景的引用验证在在线评估（13.4）
  时由同一验证器覆盖。
- 候选引用仍按固定顺序列出（结构化顺序优先）；无排序/过滤 UI。

### 下一阶段（13.3）的前置条件

✅ 引用验证器与状态词表稳定；✅ 候选/对话框/评估共用同一判定；✅ 数据集 v1.1 有固定
样例回答与期望状态；✅ 历史兼容与只读约束有测试。13.3 需要：Request/Response 元数据
（Provider、模型、Prompt 版本/hash、上下文 hash、usage、finish_reason、duration）
与隐私边界；若需要持久化先给出字段缺口分析与最小迁移方案。

---

## Phase 13.3 — Reproducibility & Telemetry

**目标**：让用户能回答"当时用了什么模型、哪个 Prompt 版本、哪些上下文、模型实际返回哪个
版本、消耗多少 Token、是否因长度限制提前结束、相同输入能否重建"，同时不新增无用列、
不重写历史运行、不泄漏凭据。

### 9.7 要求的实施前分析（字段缺口与迁移决策）

1. **当前字段缺口**：`AnalysisRun` 已有 provider_name / model_name / scope /
   context_snapshot / result / error_message / created_at / started_at / completed_at /
   duration_ms；`ChatResponse` 已解析 content / model / finish_reason / usage / raw_id，
   但 `AnalysisService` 只保存 content——返回模型、finish_reason、usage、重试次数、
   Prompt/上下文/请求哈希均未持久化。
2. **不新增字段的替代方案**：(a) 写入现有 `AnalysisRun.scope` JSON 的版本化
   `telemetry` 键（**采用**）——scope 已是运行元数据 JSON，读侧忽略未知键；
   (b) 写入 `context_snapshot` JSON——拒绝：该字段语义是"模型当时看到的上下文"；
   (c) 读取时重算——只能重算估算与哈希，无法重算 Provider 实际返回的
   model/finish_reason/usage。
3. **新字段的必要性**：Response 元数据无法由快照/结果重建，但**现有 scope TEXT 列
   已足够承载**，因此不需要新列。
4. **数据兼容策略**：新运行写入 telemetry；旧运行无该键 → 读取为 `None`、UI 显示
   "未记录（旧记录）"，不回填、不重写。scope 请求部分仍不可改写（仓储层校验，只允许
   追加/替换 telemetry）。
5. **SQLite migration 方案**：**无迁移**。不新增 Alembic revision，不改已发布迁移，
   不改事实表/来源表。若未来确需独立列，按惯例单独提出并需显式批准的最小迁移。
6. **旧 AnalysisRun 读取**：`AnalysisRunView.telemetry` 防御式解析（非 dict / 无 version
   → None）；读取本身不写库；历史 result/context_snapshot 不受影响。
7. **备份/恢复影响**：telemetry 属于 `analysis_run.scope` 行内文本，随现有备份/恢复
   原样复制；无新表/新文件；导出把 scope 作为运行元数据带出（仅哈希/计数/ID，安全）。
8. **数据库回滚策略**：schema 未变 → 无回滚动作；功能向后兼容（旧代码忽略未知键，
   旧数据新代码读为未记录）。

### 完成内容

1. **响应元数据归一化**（`app/services/llm/response_metadata.py`）：usage 缺失 →
   `available=False` 且各字段 `None`（不补 0、不推算 total；`input_tokens`/`output_tokens`
   仅重命名）；finish reason 保留原始值并归一为
   `STOP` / `LENGTH` / `CONTENT_FILTER` / `TOOL_CALLS` / `OTHER` / `UNAVAILABLE`。
2. **运行遥测**（`app/services/analysis_telemetry.py`）：版本化 `telemetry` 块，含
   Request Identity（provider、请求模型、Prompt 版本/hash、上下文 hash、请求 hash、
   schema 版本、离线估算 tokens）、Response Metadata（返回模型、finish reason、usage、
   provider request id）、Execution（status / duration / retry_count / error_class）；
   哈希使用 canonical JSON + SHA-256（`None≠""`、排除 `analysis_run_id` 与
   `generated_at`、保留数组顺序）。
3. **持久化**：telemetry 写入现有 `AnalysisRun.scope` JSON，**无迁移**；仓储层把
   scope 的 request 部分保持 write-once，只允许追加/替换 telemetry（其他差异抛
   `DomainError`，不再静默忽略）。
4. **重试计数**：`LLMGateway.chat_with_attempts()` 返回实际尝试次数，服务据此写
   `retry_count`（不推测）。
5. **UI**：结果对话框新增 结束原因 / Token 用量（Provider 实际值）/ 上下文估算
   （离线，非实际）/ Prompt 版本 / 上下文 Hash / 请求 Hash / 重试次数；`LENGTH` 与
   `CONTENT_FILTER` 显示显式警告（`analysisFinishWarning`），页面消息同步提示；
   HTTP 成功仍为 `SUCCEEDED`（提前结束 ≠ 正常完成）；旧记录显示"未记录（旧记录）"。
6. **文档**：`docs/analysis.md` 新增遥测章节；`docs/phase13-evaluation.md` 更新变更
   记录与可复现性说明。

### 修改文件

- 新增：`app/services/llm/response_metadata.py`、`app/services/analysis_telemetry.py`、
  `tests/services/llm/test_response_metadata.py`、`tests/services/test_analysis_telemetry.py`
- 修改：`app/services/llm/{gateway,__init__}.py`、`app/services/analysis.py`、
  `app/storage/analysis_repositories.py`（scope 追加式 telemetry 校验）、
  `app/ui/{analysis_dialog,analysis_page}.py`、
  `tests/services/{test_analysis_service,test_export_service}.py`、
  `tests/ui/test_analysis_ui.py`、`docs/{analysis,phase13-evaluation}.md`
- 删除：无

### 边界与数据库变更

- **无数据库迁移、无 schema 变化**（Alembic revision 数量不变；事实表/来源表未动）。
- 仓储层扩展为"scope 请求部分不可变 + telemetry 追加"，需显式校验并通过测试；
  历史运行不被回填/重写；分析不写事实。
- 隐私：不存 API Key/Authorization/完整头/原始响应体；Provider 返回值仅取 `id`。

### 新增测试（19 + 集成）

- `test_response_metadata.py`（8）：finish 类别与未知/缺失/非字符串、usage 全量/缺失/
  部分/input-output 拼写/非法值不推算。
- `test_analysis_telemetry.py`（11）：canonical JSON、Prompt/上下文/请求哈希的稳定性
  与内容敏感性、易变值排除、None≠""、partial/complete/fail 合并、scope 回环与坏数据。
- `test_analysis_service.py`（+10）：成功运行遥测字段、同输入哈希一致、usage 缺失
  unavailable、返回模型/finish 原样、重试计数=实际尝试、失败与上下文失败遥测、
  旧记录读取为未记录、无密钥/隐私内容、取消遥测、scope 请求部分不可变 + telemetry
  仅追加。
- `test_analysis_ui.py`（+2）：对话框显示结束原因/用量/Prompt 版本/哈希、LENGTH 截断
  警告与页面提示、旧记录"未记录（旧记录）"且无警告。
- `test_export_service.py`（扩展）：导出元数据包含 telemetry 且不含密钥/结果正文。

子集实测：`tests/services` 441 passed（1:15）；`tests/evaluation` 38 passed；
受影响 UI 三文件 68 passed（1:13）。

### 测试迁移

- `chat_response` 测试助手新增 `model` / `finish_reason` / `usage` / `raw_id` 参数
  （默认行为不变）；原有断言未弱化；5 项 Windows symlink 环境 Skip 保持原义。

### 测试运行命令与结果

子集（服务层 + LLM + 评估 + 受影响 UI）：

```powershell
& .\.venv\Scripts\python.exe -s -m pytest tests/services tests/evaluation tests/ui/test_analysis_ui.py tests/ui/test_memory_ui.py tests/ui/test_phase11_analysis_memory_settings.py -q -ra
```

- **（见下方"最终回归"表 13.3 行前的子集结果）**

全量回归（13.3 完成后）：

- **1630 collected / 1625 passed / 5 environment-skipped / 0 failed（退出码 0，8:33）**
- 增量核对：1598（13.2 完成后）+ 32（13.3 新增：响应元数据 8 + 遥测单元 11 +
  服务集成 11 + UI 2）= 1630 ✓；5 项 Windows symlink 环境 Skip 保持原义。

### Git 提交

| Commit | 说明 |
|---|---|
| `d6d319b` | feat(analysis)：运行遥测 + 响应元数据归一化 + scope 追加式 telemetry（含测试） |
| （本文件提交） | docs(phase13)：记录 Phase 13.3 与字段缺口分析 |

### 与原始计划不同的实现及理由

1. **无迁移、telemetry 存入 scope JSON**：项目硬规则禁止未经批准改 schema；9.7 也要求
   先报告缺口再决定。scope 已是运行元数据 JSON，读侧忽略未知键，版本化 telemetry 块
   满足全部字段需求；若未来需要独立列，按 9.7 流程另行批准。
2. **scope 追加式语义**：既有"scope write-once"契约不推翻，只增加经校验的 telemetry
   追加通道（其他差异报错），并补测试固定。
3. **返回模型与请求模型分开记录**：Provider 可能返回不同版本模型，两者都保留。
4. **截断不改变运行状态**：HTTP 成功仍 `SUCCEEDED`，截断以 finish_reason 与显式警告
   呈现，避免把元数据混入业务状态。

### 已知局限 / 结转 13.4

- 未启用 Provider 级 `response_format` 能力声明（当前仅提示词约定）；属 13.4 模型评估
  的比较维度之一。
- `provider_request_id` 仅保存响应的 `id` 字符串（截断 200 字符），不保存其他响应头。
- 遥测不包含 retry 的时间分布；`error_class` 只记异常类名。
- 旧运行无法补全 finish_reason/usage（不重写历史，符合要求）。

### 下一阶段（13.4）的前置条件

✅ 运行遥测（Prompt/上下文/请求哈希、模型元数据、usage、finish、重试、耗时）可读取；
✅ 离线评估框架与数据集 1.1、引用验证器可复用；✅ 历史兼容与无迁移约束有测试。
13.4 需要：统一 Dataset/Scenario/ContextSnapshot/Prompt 版本下的多模型离线对比、
显式触发的在线评估、报告定位失败样本、人工复核与 LLM Judge 分离、`evaluation-runs/`
产物边界。

---

## Phase 13.4 — Model Evaluation & Review

**目标**：把不同 Provider/模型的同批任务对比做成统一评估流程——不判断"最聪明"，而是评估
事实边界、引用、不确定性、稀疏上下文下的克制与长上下文稳定性；输出机器可读 + 人类可读
报告，人工复核与自动评估分离。

### 完成内容（对应 10.x）

1. **10.1 离线评估为基础模式**：`model_answers` 固定合成答案经真实物化上下文、Prompt、
   Parser、ReferenceVerifier 与规则评估器比较；无 Provider、无网络。
2. **10.2 在线评估只能显式启动**：`python -m app.evaluation --online --provider <name>
   --confirm-online` 双重门禁；缺 provider/确认直接拒绝；每次只调用一个显式模型；不使用
   生产 Workspace 真人数据（只用合成夹具）。
3. **10.3 公平比较固定项**：同 run 内同 Scenario 共享 Dataset 版本/digest、Scenario
   版本、ContextSnapshot、Prompt 版本/哈希、输出契约、请求参数；结果行与 manifest 记录
   版本；失败请求保留状态（error_class / timed_out / attempts / 提前结束）。
4. **10.4 维度**：自动（PARSE_VALIDITY / REQUIRED_SECTIONS / CITATION_VALIDITY /
   CROSS_WORKSPACE_LEAKAGE / FACT_COVERAGE / GROUNDING 代理）、观测（LATENCY /
   TOKEN_USAGE / FAILURE_RATE）、人工（UNSUPPORTED_CLAIMS / UNCERTAINTY_HANDLING /
   EPISTEMIC_SEPARATION / COMPLETENESS / ROBUSTNESS，N/A + 复核清单）。
5. **10.5 不以 LLM Judge 裁决**：本阶段不实现 Judge；人工结论独立保存，不写事实、不自动
   决定 Memory；模型一致 ≠ 事实为真。
6. **10.6 报告**：`evaluation-runs/<run-id>/{manifest.json, raw-results.jsonl,
   metrics.json, report.md}`；含版本、模型、参数、执行时间、成功/失败计数、分指标结果、
   失败样本、引用错误、人工复核清单、跨模型差异、局限；明确"不是排行榜"。
7. **10.7 UI 决策**：不嵌入主界面；现有 AnalysisPage（13.1/13.2/13.3）覆盖单次运行，
   跨模型比较走本地报告文件；独立 Evaluation Viewer 延后并记录设计要点。

### 修改文件

- 新增：`app/evaluation/model_evaluation.py`、`tests/evaluation/test_model_evaluation.py`
- 修改：`app/evaluation/{models,dataset,report,__main__,__init__}.py`、
  `app/domain/entities/analysis_result.py`（`sections_present` 只读字段）、
  `app/services/analysis_result_parser.py`（填充存在性）、
  `tests/evaluation/test_evaluation_contract.py`（注册表 22 项）、
  `tests/services/test_analysis_result_parser.py`（+2）、
  数据集 1.2（A/D/G 增加 alpha/beta 固定答案；18 个 JSON 版本递增）
- 删除：无

### 数据库变更

**无迁移、无 schema 变化、无新增依赖**（评估只在临时夹具库中运行，不写生产
AnalysisRun/事实/Memory）。`AnalysisResult.sections_present` 是纯内存只读字段，默认空
元组，历史 JSON 解析结果不受影响。

### 关键设计决策

1. **数据集版本 1.2**：新增 `model_answers` 属内容变更（非结构），故递增
   `dataset_version` 而保持 `schema_version=1`；加载器对 label 唯一性、占位符可达性、
   占位符仅在 model_answers/sample_output 出现做 fail-closed 校验。
2. **`sections_present` 而非语义判断**：REQUIRED_SECTIONS 需要"四节是否存在"的确定性
   依据；解析器记录 JSON 中出现的 list 键（legacy Markdown 记录非空小节），评估器据此
   判定，不猜测内容语义。
3. **固定答案对比不调用 Provider**：offline 模式下每个 label 只是不同回答内容，共享
   同一 snapshot/prompt；这样"公平比较固定项"由构造保证，且完全离线可复现。
4. **在线评估的失败如实保留**：网关成功时记录真实 attempts；异常路径无法得知重试次数
   时记为未知而不是猜；超时单独标记；usage 缺失 = unavailable，不补 0。
5. **GROUNDING 明确标注为结构性代理**：只检查 FACT 条目是否带合法引用；语义是否被支持
   写入人工复核项，不伪装成自动通过。
6. **报告不做排行榜**：分指标展示 + 差异列表；无综合分；局限章节显式声明不可作排行榜。

### 新增测试（19 项 + 契约/解析器更新）

- `tests/evaluation/test_model_evaluation.py`（19）：
  - 数据集：两标签固定答案、重复 label 拒绝、未知占位符拒绝；
  - 离线：覆盖 3 Scenario × 2 标签、合规/噪声指标差异、公平性元数据、
    答案占位符解析与保留、确定性与零网络（socket 陷阱）、人工指标 N/A、聚合观测；
  - 在线：必须匹配已配置模型、成功元数据与指标、失败保留（401）、超时标记（重试 2 次）、
    提前结束 finish=LENGTH、只调用一个显式 Provider；
  - CLI：`--models` 离线零网络、`--online` 缺确认/缺 provider 拒绝（exit 2）、
    注入 Gateway 在线运行；
  - 报告：manifest/metrics/raw-results/report 内容与"非排行榜"声明。
- `tests/evaluation/test_evaluation_contract.py`：注册表 18 → 22（新增 4 维度）。
- `tests/services/test_analysis_result_parser.py`：`sections_present` JSON/不可解析文本。

### 测试运行命令与结果

子集：

```powershell
& .\.venv\Scripts\python.exe -s -m pytest tests/evaluation tests/services -q -ra
```

- `tests/evaluation`：57 passed（10.3s）
- `tests/services`：441 passed（1:12）

全量回归：

- **1651 collected / 1646 passed / 5 environment-skipped / 0 failed（退出码 0，8:10）**
- 增量核对：1630（13.3 完成后）+ 21（13.4 新增：模型评估 19 + 解析器 sections_present
  2）= 1651 ✓；5 项 Windows symlink 环境 Skip 保持原义。

### Git 提交

| Commit | 说明 |
|---|---|
| `0d4580b` | feat(evaluation)：模型评估与对比（固定答案离线 + 显式在线门禁 + 报告，含测试） |
| （本文件提交） | docs(phase13)：记录 Phase 13.4 |

### 与原始计划不同 / 未完成项

1. **未实现独立 Evaluation Viewer**：10.7 为"如确实需要"；本阶段评估以 CLI + 报告文件
   为界面，主界面保持简洁。设计要点已记录于 `docs/phase13-evaluation.md §13.6`，可作为
   后续独立阶段（不在 13.4 强制范围）。
2. **未实现 LLM Judge 流程**：10.5 禁止以 Judge 为裁判；Judge 属实验性，本阶段只记录
   边界，不实现。
3. **在线评估不写 AnalysisRun**：评估是审计行为，不产生生产分析历史；原始结果只存入
   `evaluation-runs/`（本地、gitignore）。
4. **未新增依赖**：报告与聚合均为标准库实现。

### 已知局限 / 结转

- 自动指标只覆盖结构与引用合法性；语义支持、矛盾处理、不确定性质量仍为人工复核。
- 离线固定答案没有真实 latency/usage；在线每个 run 只测一个模型，跨模型对比需要多个
  在线 run，比较前必须核对 manifest 版本一致性（报告已声明）。
- `attempts` 在异常路径无法准确取得（网关内部重试），报告如实标记未知而非估算。
- `evaluation-runs/` 产物含原始模型回答（合成数据），仍不应提交仓库；已由 `.gitignore`
  排除。

### Phase 13 收尾说明

Phase 13 的五个子阶段（13.0–13.4）至此完成。后续若继续扩展，建议（需用户明确指派）：
把人工复核结果采集与比对接入流程、独立 Evaluation Viewer、Judge 实验记录，以及对
"评估结论不进入事实层"的持续审计。

---

## Phase 13.5 — 遥测重试次数修正（小修复）

**背景**：Phase 13.3 的 `LLMGateway.chat_with_attempts()` 只在**成功**时返回实际
尝试次数；最终失败时直接抛出异常，不携带次数。`AnalysisService` 的失败路径调用
`fail_telemetry()`，而后者固定写 `retry_count = 0`。于是「Attempt 1 → HTTP 500，
Attempt 2 → HTTP 500，最终 FAILED」会被遥测记成 `retry_count = 0`，低估重试并污染
Phase 13 的评估数据。

**修复原则**：成功与最终失败都记录**真实尝试次数**；只有确实无法确认的路径（取消等）
记为 unavailable（`null`），绝不伪造 0。

### 完成内容

1. **Gateway 携带尝试次数**（`app/services/llm/gateway.py`）：最终失败时把实际尝试
   次数附加到抛出的 `LLMError`（`ATTEMPTS_ATTRIBUTE`）；新增
   `attempts_from_error(exc) -> int | None`，只接受 `int >= 1`，其他值（缺失、0、
   bool、字符串）一律视为未知。
2. **遥测不再写死 0**（`app/services/analysis_telemetry.py`）：`_execution()` 改为
   由 `attempts` 推导 `retry_count = attempts - 1`，`attempts=None` → `retry_count=null`；
   `fail_telemetry()` 新增必填 `attempts` 参数，调用方必须显式说明能否确认次数。
3. **Service 三条路径区分**（`app/services/analysis.py`）：
   - Provider 最终失败：`attempts = attempts_from_error(exc)`（真实值）；
   - 上下文构建失败：`attempts = 0`（可证明未发生 Provider 调用）；
   - `cancel_run()` / 取消路径：`attempts = None`（无法确认，记 unavailable）。
4. **在线模型评估**（`app/evaluation/model_evaluation.py`）：失败 outcome 也通过
   `attempts_from_error()` 记录真实尝试次数（此前恒为 `None`）。
5. **UI**（`app/ui/analysis_dialog.py`）：新运行 `retry_count = null` 显示
   `unavailable（无法确认）`；旧运行（无 telemetry）仍显示 `未记录（旧记录）`。

### 语义对照

| 路径 | attempts 来源 | 记录 retry_count |
|---|---|---|
| 首次即成功 | 网关返回 1 | 0 |
| 一次重试后成功 | 网关返回 2 | 1 |
| 不可重试错误（如 401） | 异常携带 1 | 0（真实） |
| 可重试错误重试耗尽（如 500×2） | 异常携带 2 | **1（不再误记 0）** |
| 上下文构建失败 | 显式 0 | 0（可证明无调用） |
| 取消（provider 调用前后均可能） | 未知 | `null`（unavailable） |
| Phase 13.3 之前的历史运行 | 无 telemetry | 读为「未记录」，不回填 |

### 数据库变更

**无**：仍无 Alembic migration、无 schema 变化；历史运行不回填/不重写（旧遥测里的
`retry_count` 是历史事实，保留原值）。

### 修改文件

- `app/services/llm/gateway.py`（尝试次数附加 + `attempts_from_error`）
- `app/services/llm/__init__.py`（导出）
- `app/services/analysis_telemetry.py`（`_execution` / `fail_telemetry`）
- `app/services/analysis.py`（失败、上下文失败、取消三路径）
- `app/evaluation/model_evaluation.py`（在线失败计数）
- `app/ui/analysis_dialog.py`（unavailable 展示）
- 测试：新增 `tests/services/llm/test_gateway_attempts.py`；更新
  `tests/services/test_analysis_telemetry.py`、`tests/services/test_analysis_service.py`、
  `tests/evaluation/test_model_evaluation.py`
- 文档：`docs/analysis.md`、`docs/phase13-evaluation.md`、本文件

### 新增/更新测试

- `test_gateway_attempts.py`（7）：首次成功=1；重试后成功=2；重试耗尽抛出的异常携带
  真实次数（2 与自定义 3）；不可重试=1；取消路径无次数；`attempts_from_error` 拒绝
  缺失/0/bool/字符串。
- `test_analysis_service.py`：新增「重试耗尽记录 retry_count=1」；401 失败断言
  `retry_count=0`（恰 1 次调用）；上下文失败断言 0；取消断言 `null`。
- `test_analysis_telemetry.py`：`fail_telemetry(attempts=2)` → retry_count=1；新增
  `attempts=None` → `null`（unavailable）用例。
- `test_model_evaluation.py`：在线 401 断言 attempts=1；超时断言 attempts=2。

### 测试运行命令与结果

全量回归：

- **1660 collected / 1655 passed / 5 environment-skipped / 0 failed（退出码 0，8:34）**
- 增量核对：1651（13.4 完成后）+ 9（13.5 新增：网关尝试次数 7 + 遥测单测 1 +
  服务集成 1）= 1660 ✓；5 项 Windows symlink 环境 Skip 保持原义。

### Git 提交

| Commit | 说明 |
|---|---|
| `99696b4` | fix(analysis)：失败路径记录真实重试次数（unavailable 不伪造 0，含测试） |
| （本文件提交） | docs(phase13)：记录 Phase 13.5 |

### 与原始计划不同的地方 / 局限

- 取消路径仍按「无法确认」记录 `null`：网关在 provider 调用前后都可能观察到取消，
  不为了凑数而声称确定次数。
- 旧运行不回填：历史遥测中的 `retry_count=0` 保持原样；只有新运行使用新语义。
- 该修复不改变任何业务结果/事实层，仅修正遥测准确性。

---

## 最终回归（每子阶段后更新）

| 阶段 | collected | passed | skipped | failed | 退出码 |
|---|---|---|---|---|---|
| Phase 12.5 末 | 1516 | 1511 | 5 | 0 | 0 |
| Phase 13.0 开工基线 | 1516 | 1511 | 5 | 0 | 0 |
| Phase 13.0 完成后 | 1557 | 1552 | 5 | 0 | 0 |
| Phase 13.0 补充（契约形式化） | — | — | — | — | 仅文档+注册表；相关子集 58 项通过 |
| Phase 13.1 完成后 | 1583 | 1578 | 5 | 0 | 0 |
| Phase 13.2 完成后 | 1598 | 1593 | 5 | 0 | 0 |
| Phase 13.3 完成后 | 1630 | 1625 | 5 | 0 | 0 |
| Phase 13.4 完成后 | 1651 | 1646 | 5 | 0 | 0 |
| Phase 13.5 完成后 | 1660 | 1655 | 5 | 0 | 0 |
