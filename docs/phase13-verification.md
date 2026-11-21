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

## 最终回归（每子阶段后更新）

| 阶段 | collected | passed | skipped | failed | 退出码 |
|---|---|---|---|---|---|
| Phase 12.5 末 | 1516 | 1511 | 5 | 0 | 0 |
| Phase 13.0 开工基线 | 1516 | 1511 | 5 | 0 | 0 |
| Phase 13.0 完成后 | 1557 | 1552 | 5 | 0 | 0 |
| Phase 13.0 补充（契约形式化） | — | — | — | — | 仅文档+注册表；相关子集 58 项通过 |
| Phase 13.1 完成后 | 1583 | 1578 | 5 | 0 | 0 |
