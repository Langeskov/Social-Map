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
   J 超长上下文（近限 21527/24000 ≈ 90% 与超限显式拒绝）。
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

## 最终回归（每子阶段后更新）

| 阶段 | collected | passed | skipped | failed | 退出码 |
|---|---|---|---|---|---|
| Phase 12.5 末 | 1516 | 1511 | 5 | 0 | 0 |
| Phase 13.0 开工基线 | 1516 | 1511 | 5 | 0 | 0 |
| Phase 13.0 完成后 | 1557 | 1552 | 5 | 0 | 0 |
