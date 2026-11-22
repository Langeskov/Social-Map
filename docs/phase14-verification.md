# Phase 14 验收记录 — Revision & ChangeLog

> 每个子阶段完成后追加对应小节。测试数字必须来自**本次实际执行**，不沿用历史数字。

---

## Phase 14.0 — Change Semantics & Data Contract（设计阶段）

### 1. 本阶段结论

**完成**。产出完整设计契约 [Phase 14 设计文档](<phase14-design.md>)，覆盖：
架构盘点、业务变更语义、实体审计矩阵、ChangeLog Schema、多态引用决策、操作分组、
事务边界、快照序列化、隐私/保留、导出与备份、恢复规则、Migration 设计、API 草案、
UI 草案、测试策略、风险与实施顺序。

本阶段**未修改任何业务代码、未新增 Migration、未修改数据库 Schema、未修改现有测试**。

### 2. 开发前检查（实际结果）

| 项 | 实际值 |
|---|---|
| 分支 | `phase-13-analysis-quality-evaluation` → 新建 `phase-14-revision-changelog` |
| 开发前 HEAD | `582e9961d524751ecd8e90e6ba2f4f4e7b4e1344`（Phase 13.5 完成） |
| 工作区 | 干净（无未提交修改），未执行 reset/clean/stash |
| 本地分支 | `main`、`chore/lint-cleanup`、`phase-12-investigation-console`、`phase-13-analysis-quality-evaluation` |
| 远端 | `origin/main` 为 Phase 13 发布快照 `9fea86a`（发布快照历史经剔除私有文件重建，与本地开发历史为两条线；本地未推送） |
| 远端差异 | `origin/main...HEAD` = 24 / 45（预期：远端是发布快照，本地是完整开发历史） |
| Alembic head | `0008_memory`（0001–0008，Phase 13 无新迁移，与设计一致） |
| Phase 13 一致性 | 本地代码 = Phase 13.5 收尾；全量测试通过（见 §5），无缺失提交 |
| 测试入口 | `& .\.venv\Scripts\python.exe -s -m pytest -q -ra` |

### 3. 盘点产出（详见设计文档 §2）

- **持久化**：11 张事实表 + 搜索投影 + 分析层 5 表 + memory；ORM 守卫清单；
  当前 head `0008_memory`。
- **写操作清单**：Workspace/Person/Alias/Media/Relationship/RelationshipHistory/
  Event/Participant/Evidence/EvidenceMedia/Memory 的 Service 方法与事务内写入，
  明确到方法名（设计文档 §2.4）。
- **事务机制**：`write_operation` / `transaction`，一操作一 Session 一事务，
  BEGIN IMMEDIATE（ris_write）；无嵌套事务；Media 导入两阶段（文件 + DB）。
- **UI 入口**：PeoplePage/EventDetail/EvidencePanel+Dialog/RelationshipPanel/
  MemoryPanel/SettingsPage（设计文档 §2.5）。
- **迁移测试影响面**：head/表集合断言分布在
  `tests/test_application_startup.py`、`tests/database/test_initialization.py`、
  `tests/migrations/schema_facts.py`、`test_analysis_schema.py`、
  `test_empty_database_migrations.py`（14.1 必须同步更新）。

### 4. 关键设计决定（均写入设计文档）

1. **单表 ChangeLog + 逻辑 Revision**：不建独立 Revision 表。
2. **多态实体引用（方案 A）**：真实 `workspace_id` 外键 + `entity_type` CHECK +
   Writer 校验 + 14.4 完整性审计；拒绝按类型建空表和 SQLite 触发器方案。
3. **聚合快照**：Alias/PersonMedia/EventParticipant/EvidenceMedia 不单独建实体，
   作为 PERSON/EVENT/EVIDENCE 快照中的有序集合；RELATIONSHIP_HISTORY 单独成实体并以
   `parent=(RELATIONSHIP, id)` 关联。
4. **全量白名单快照 + changed_fields 数组**（非只存 diff），便于直接回答历史状态；
   Evidence 不含 `content_text`，Media 不存二进制与 `storage_path`。
5. **四时间分离**：Event Time / Effective Time / Recorded Time / Analysis Time 不混用。
6. **无迁移历史不伪造**：旧实体首次记录 revision_no=1，UI 明示 Phase 14 前历史不可恢复。
7. **业务事务与审计同事务**：`operation_id` 由最外层 Service 写方法创建并透传嵌套 helper；
   ChangeLog 失败 → 整个事务回滚。
8. **恢复=新业务操作**：走既有 Service，产生 REVERT 新版本；不改写旧行；集合/生命周期/
   不可变字段不参与字段恢复。
9. **隐私与保留**：历史快照可能含旧个人信息；本地库；备份包含、默认导出不含；
   14.x 不提供清理 API。
10. **AnalysisRun 不纳入普通 Revision**；其删除生命周期审计明确排除在 14.x 外。

### 5. 测试结果（14.0 基线，无代码改动）

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

- Collected：**1660**
- Passed：**1655**
- Failed：**0**
- Skipped：**5**（Windows 临时目录 symlink 权限，`tests/storage/test_media_store.py:275`）
- Exit Code：**0**（8:46）

说明：本阶段无代码改动，测试用于确认 Phase 13.5 基线在当前环境可复现；
5 项 Windows symlink 环境 Skip 保持原义，不计为通过。

### 6. 数据库变化

**无**：未新增 Alembic revision、未修改任何表；`alembic heads` 仍为 `0008_memory`。
Migration `0009_change_log` 只计划在 14.1 实施（用户审核设计后）。

### 7. 已知问题和风险（阻塞性）

见设计文档 §17。对下一阶段有影响的：
1. revision_no 并发冲突处理策略（单用户下接受唯一约束回滚）。
2. Media 导入两阶段与 Person 带头像创建路径的 operation 透传。
3. Alias/关联行硬删除导致的多态引用孤儿风险（完整性审计兜底）。
4. 快照体积随修订线性增长（单机可接受，暂不压缩）。
5. UI 历史入口挂在现有详情页，避免主窗口重构。

### 8. 下一阶段（14.1）前置条件

- ✅ 设计文档完成，覆盖 ChangeLog Schema、迁移、序列化、引用决策。
- ⏳ **用户审核设计文档**（本阶段验收条件）。
- 14.1 准备条件：head 仍为 `0008_memory`；迁移测试更新清单已列出；
  Domain/Serializer/Repository 接口草案已定义。
- 14.1 禁止：接入业务 Service、实现 UI、实现恢复、补录旧历史。

### 9. Git 状态

- 当前分支：`phase-14-revision-changelog`
- 开发前 HEAD：`582e996`
- 本阶段修改文件：新增 `docs/phase14-design.md`、`docs/phase14-verification.md`；
  更新 `README.md`（状态、Phase 14 链接、边界说明）
- 是否提交：**是**（`9847eb8` 设计提交；本记录补齐提交紧随其后）
- 是否推送：**否**（未推送远端）

---

## Phase 14.1 — ChangeLog Persistence Foundation

### 1. 本阶段结论

**完成**（按 14.0 已确认设计）：新增 Migration `0009_change_log`、Domain 契约、
append-only ORM 模型与守卫、Repository 接口与 SQL adapter、白名单快照 Serializer、
只读 `ChangeLogService` 查询基础。**未接入任何业务 Service、无历史 UI、无恢复、无旧历史
补录**；既有事实表与 Phase 1–13 行为不变。

### 2. 实际修改内容

- **Domain**：新增 `app/domain/entities/change_log.py`（ENTITY_TYPES / ACTIONS /
  ALLOWED_ACTIONS / PAYLOAD_VERSION / `ChangeLogEntry` 契约与 canonical JSON 编解码），
  并加入 `app/domain/entities/__init__.py` 导出。
- **Storage**：`app/storage/models.py` 新增 `ChangeLogModel`（表 `change_log`）与
  `before_update`/`before_delete` 追加式守卫；新增 `app/storage/change_log_repositories.py`
  （只 add/get/list/get_revision/latest_revision，无 update/delete）。
- **Repository 接口**：新增 `app/repositories/change_log_interfaces.py`（Protocol，
  append + 查询，无变更方法）。
- **Migration**：新增 `alembic/versions/0009_change_log.py`（只建一表 + 三索引 + 约束，
  downgrade 逆序删除；不动既有表）。
- **Service（只读）**：新增 `app/services/change_log.py`（`ChangeLogService`：按实体/
  父实体/operation/指定 revision/最新版本查询，Workspace 隔离与分页校验）与
  `app/services/change_log_repositories.py` 装配。
- **Serialization**：新增 `app/services/change_serialization.py`（每实体白名单、
  集合快照排序、canonical 编码、payload_version 解码、changed_fields 顺序）。
- **Tests**：新增 `tests/services/test_change_serialization.py`、
  `tests/services/test_change_log_service.py`、
  `tests/repositories/test_change_log_repository.py`、
  `tests/migrations/test_change_log_migration.py`；同步更新 head/表集合断言的既有迁移
  与初始化测试（0009、change_log）。
- **Docs**：更新 `docs/architecture.md`（变更审计层）、`docs/database.md`（0009 与
  downgrade）、`docs/phase14-design.md`（14.1 实施说明）。

### 3. 关键设计决定（本阶段落地）

1. **单表 + 逻辑 Revision**：未建 Revision 表；`revision_no` 唯一约束由数据库兜底。
2. **只追加**：Repository 无 update/delete；ORM 守卫拒绝修改/删除；不宣称触发器保护。
3. **多态实体引用**：仅 `workspace_id` 真实外键；实体/父引用由 Writer（14.2）与完整性
   审计（14.4）校验；唯一约束 `(workspace_id, entity_type, entity_id, revision_no)`。
4. **`metadata` 命名**：SQLAlchemy Declarative 保留属性名冲突 → ORM 属性 `metadata_json`，
   数据库列仍为 `metadata`，Domain 字段仍为 `metadata`。
5. **无旧历史补录**：迁移不写行；旧实体首次记录 revision_no=1（14.2 起）。
6. **写入口仍未启用**：14.1 不调用 Writer，应用运行时不会产生 ChangeLog 行。

### 4. 数据库变化

- 新增唯一 Migration：`0009_change_log`（down_revision `0008_memory`）。
- 新表 `change_log`：真实 `workspace_id` 外键、多态实体引用、唯一 revision、
  JSON 快照列、`recorded_at`；CHECK：实体类型/操作类型/revision ≥1/parent 成对/
  payload_version 非空/changed_fields 非空。
- 既有表未修改；迁移不写数据；downgrade 只删除 `change_log` 及索引；
  现有数据升级后逐行保留（有测试）。
- Alembic head：`0008_memory` → **`0009_change_log`**。

### 5. 测试运行命令与结果

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

- **1686 collected / 1681 passed / 5 environment-skipped / 0 failed（退出码 0，8:37）**
- 增量核对：1660（Phase 14.0 基线）+ 26（Phase 14.1 新增：迁移 4 + 仓储 8 +
  序列化 10 + 只读 Service 4）= 1686；既有迁移/初始化 head 断言已更新但未削弱；
  5 项 Windows symlink 环境 Skip 保持原义。

新增/更新的针对性测试（14.1 范围）：

- 迁移：建表/列/可空/约束/索引、upgrade/downgrade 往返、数据保留、非法行拒绝、
  offline SQL 渲染与在全新库执行；
- 仓储：roundtrip/canonical JSON/Workspace 隔离/事务/唯一 revision/CHECK/FK/
  ORM 追加式守卫/无 update-delete API/分页与父查询；
- 序列化：8 类白名单、日期/时间/UUID/None 归一、集合排序、changed_fields 顺序、
  未知实体/缺字段/非法版本拒绝；
- 只读 Service：排序/分页/隔离/revision/operation/父查询/参数校验/无写 API。

### 6. 已知问题和风险

1. **未接入业务 Service**：应用当前不会产生审计行；14.2 之前"历史为空"是预期行为。
2. **revision_no 并发**：写事务内 MAX+1，唯一约束兜底；单用户下可接受，14.2 需验证。
3. **多态孤儿风险**：数据库无法外键到实体，14.4 完整性审计补齐检测。
4. **快照体积**：全量白名单快照（非 diff），Evidence 大文本与媒体二进制已排除。
5. **head 断言更新**：多处历史迁移测试同步到 0009（未弱化旧 revision 断言）。

### 7. 下一阶段（14.2）前置条件

- ✅ Migration 0009 可升级/降级；表与索引经测试；旧数据升级无损。
- ✅ Repository 只追加、Serializer 白名单与版本化就绪；只读查询可用。
- ⏳ 14.2 需要：`ChangeOperationContext` + `ChangeLogWriter`（同事务写入）、
  按审计矩阵接入各 Service、事务/回滚/operation 分组/无变化不记测试。
- 14.2 禁止：UI、恢复、旧历史补录、改 RelationshipHistory/Evidence 规则。

### 8. Git 状态

- 分支：`phase-14-revision-changelog`
- 本阶段提交：`18dba28`（代码 + 测试，含迁移 0009）；文档提交紧随本记录
- 是否推送远端：**否**

---

## Phase 14.2 — Transactional Change Capture

（待实施）

---

## Phase 14.2 — Transactional Change Capture

### 1. 本阶段结论

**完成**：在 14.0 审计矩阵与 14.1 持久化基础上，实现统一捕获入口
（`ChangeOperationContext` + `ChangeLogWriter`），并将 ChangeLog 接入 Person／Alias（聚合快照）／
Media／Relationship／RelationshipHistory／Event／Participant（聚合快照）／Evidence／
EvidenceMedia（聚合快照）／Workspace／Memory 的真实业务 Service。业务写入与审计写入同事务，
成功一起提交、失败一起回滚；无实际变化的更新不产生虚假 Revision。**未实现历史 UI 与恢复。**

### 2. 实际修改内容

- **Application/Service**：新增 `app/services/change_capture.py`（operation context、writer、
  实体/父引用校验、no-op 规则）、`app/services/change_snapshots.py`（聚合快照助手）；
  `application.py` 组合根创建唯一 Writer 并注入 7 个业务 Service。
- **接入点**：
  - Workspace：create/update/archive（`ensure_default` 系统引导不审计）。
  - Person：create（含带头像的单 operation 两阶段）、update、archive、alias 增删、
    avatar/gallery 关联、媒体引用解除。
  - Media：import（仅新建或恢复已删除媒体）、archive；人物/证据的附件变化进入父实体快照。
  - Relationship：create、archive、restore；add_history → RELATIONSHIP_HISTORY
    APPEND_HISTORY（parent=RELATIONSHIP）；Event↔History 关联 → ATTACH/DETACH_EVENT。
  - Event：create/update/archive/restore；participant 增删与角色/备注变化进入 EVENT 快照。
  - Evidence：create、metadata 更新、archive/restore、附件增删。
  - Memory：手工/来自分析的 create、update、archive、restore。
- **Domain 契约微调**：`APPEND_HISTORY` 归入 create-like（`before=null`），与"新领域历史行
  此前不存在"一致；更新 14.1 相关测试数据。
- **未改**：无 schema/migration（仍 `0009_change_log`）；无 UI；无恢复；未回填旧历史；
  AnalysisRun 不进入普通 Revision。

### 3. 关键设计决定（实际落地）

1. **Writer 不拥有事务**：只使用调用方 `write_operation` 的 Session；不 commit、不新建 Session。
2. **显式 operation context**：最外层 Service 方法创建，嵌套 Media 导入透传；同一 operation
   的所有行共享 `operation_id` 与 `recorded_at`。
3. **聚合快照**：Alias/PersonMedia/EventParticipant/EvidenceMedia 不单独建实体，作为父实体
   快照集合；RelationshipHistory 独立成实体并以 parent 关联 RELATIONSHIP。
4. **no-op 规则**：UPDATE/ATTACH/DETACH 前后快照一致 → 不写行；生命周期动作与创建始终记录。
5. **组合根注入**：`create_application_services()` 是唯一生产装配点；Service 的
   `changes=None` 默认关闭捕获，保证既有直接构造 Service 的测试/脚本行为不变。
6. **两阶段 Media**：文件阶段不写审计；只有实际提交业务行的 `_persist` 事务写审计；
   人物创建带头像时由 PersonService 透传 operation，捕获 PERSON CREATE + MEDIA IMPORT 一次。
7. **审计失败即回滚**：Writer/Serializer 异常向外传播，业务事务一并回滚（有测试）。

### 4. 数据库变化

**无**：未新增 migration、未改 schema；仍为 `0009_change_log`。14.2 仅开始产生审计行。

### 5. 测试运行命令与结果

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

- **1712 collected / 1707 passed / 5 environment-skipped / 0 failed（退出码 0，8:55）**
- 增量核对：1686（Phase 14.1 完成后）+ 26（Phase 14.2 新增：change capture 14 +
  transactions 12）= 1712；既有测试无删除/弱化，5 项 Windows symlink skip 保持原义。

新增测试：

- `tests/services/test_change_capture.py`（14）：writer 契约（operation context、跨 Workspace/
  不存在实体/父引用拒绝、no-op、生命周期、APPEND_HISTORY 快照规则、session/context 必填）；
  Person/Alias/Media 关联、Event/Participant、Relationship/History、Evidence/附件、Memory、
  Workspace、Media 的实体捕获；带头像创建为单一 operation 且无重复 PERSON 行。
- `tests/services/test_change_capture_transactions.py`（12）：业务+审计同提交；快照生成失败、
  ChangeLog 写入失败、Event/Participant 中途失败均整体回滚；校验失败无审计；no-op 不增版本；
  revision 按实体递增；带头像 operation 分组；只读操作不写；第二 Workspace 隔离；组合根共享
  Writer；无 Writer 的 Service 不捕获。
- 14.1 测试按 `APPEND_HISTORY` 新语义更新（before=null），未弱化断言。

### 6. 已知问题和风险

1. 14.2 不提供任何读取 UI；审计行只能通过 SQL/后续 14.3 查看（预期）。
2. `revision_no` 依赖事务内 MAX+1 + 唯一约束；单用户可接受，并发冲突会整体回滚。
3. 多态引用孤儿仅靠 14.4 完整性审计（数据库无跨表外键）。
4. 归档/恢复等生命周期动作在快照无字段差异时仍记录一行（`changed_fields=[]`），
   语义由 action 表达。
5. 既有测试大多直接构造 Service（无 Writer），因此不覆盖捕获；捕获路径由 14.2 测试与
   组合根测试覆盖；生产装配点唯一。

### 7. 下一阶段（14.3）前置条件

- ✅ 业务写操作已产生可查询的 ChangeLog；operation 分组、版本递增、Workspace 隔离可用。
- ✅ 原子提交/回滚、no-op、嵌套两阶段均有测试。
- ⏳ 14.3 需要：历史查询/分页、Diff（字段中文标签、长文本折叠、未知版本降级）、
  操作组展示、旧数据"无历史"提示、Person/Event/Evidence/Relationship/Memory 入口。
- 14.3 禁止：恢复按钮、修改历史、伪造旧历史。

### 8. Git 状态

- 分支：`phase-14-revision-changelog`
- 本阶段提交：`7030e9f`（代码 + 测试）；文档提交紧随本记录
- 是否推送远端：**否**

---

## Phase 14.3 — Revision History & Diff Viewer

（待实施）

---

## Phase 14.3 — Revision History & Diff Viewer

### 1. 本阶段结论

**完成**：提供稳定的只读历史查询/分页/操作组/字段差异能力，并实现 Person/Event/
Evidence/Relationship/Memory 的历史入口对话框。可访问已归档实体历史；旧数据明确提示
"不可恢复/不补录"；未知或损坏快照降级显示而不崩溃；**没有任何恢复/撤销行为**（14.4）。

### 2. 实际修改内容

- **Service**：新增 `app/services/change_history.py`（`ChangeHistoryService`：
  `entity_history` 新版在前分页、`parent_history`、`revision`、`operation`、`diff`/
  `adjacent_diff`；中文字段/实体/动作标签与值格式化；`HistoryPage.legacy_hint`）。
- **Domain/Storage**：`ChangeLogEntry.snapshot_error` 可选字段；Repository 容错解码
  （未知 payload_version / 损坏 JSON → 行仍可读、标记不可 diff）；变更
  `list_by_entity(newest_first=True)` 查询参数（默认保持旧行为）。
- **Application**：`ApplicationServices.change_history` 装配只读服务。
- **UI**：新增 `app/ui/revision_history.py`（只读 `RevisionHistoryDialog` +
  `OperationDialog` + `LongTextDialog`）；入口：People 人物操作区"历史"、Event 详情、
  Evidence 面板、Relationship 面板、Memory 面板；关系显示"状态历史（追加记录）"分区。
- **Docs**：设计文档 §20、architecture、README、本记录。
- **未改**：无 schema/migration（仍 0009）；无恢复；历史记录不可修改；未补录旧历史。

### 3. 关键设计决定

1. **只读优先**：查询/差异/UI 全部只读；不提供 update/delete/revert API 或按钮。
2. **新版在前 + 分页**：`limit+1` 探测 `has_more`；不在 UI 线程做全量扫描。
3. **人类可读**：实体/动作/字段中文标签；值统一格式化（None/布尔/日期/时区/UUID/
   集合摘要/长文本）；不向用户暴露原始 JSON 或内部字段名。
4. **未知快照降级**：`snapshot_error` 标记 + 明确提示；禁止 diff/revert 该行；
   绝不猜测或伪造内容。
5. **旧数据边界**：最早记录不是创建动作时提示 Phase 14 之前历史不可恢复；完全没有历史
   时说明不会补录；不用 created_at/updated_at 生成版本。
6. **关系历史**：RELATIONSHIP 行与 parent=RELATIONSHIP 的 RELATIONSHIP_HISTORY 行分区分页
   展示；操作组按钮统一展示同一次操作的全部变更。
7. **已归档实体**：历史查询不依赖实体当前可写/可见性，归档后仍可查看。

### 4. 数据库变化

**无**：未新增 migration、未改 schema（仍 `0009_change_log`）。本阶段只读。

### 5. 测试运行命令与结果

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

- **1733 collected / 1728 passed / 5 environment-skipped / 0 failed（退出码 0，8:59）**
- 增量核对：1712（Phase 14.2 完成后）+ 21（Phase 14.3 新增：history service 11 +
  UI 10）= 1733；既有测试无删除/弱化，5 项 Windows symlink skip 保持原义。

新增测试：

- `tests/services/test_change_history.py`（11）：新版在前分页、字段标签/值格式、任意/相邻
  版本 diff、长文本全文、操作组、关系 parent 历史、归档实体可读、旧数据提示与不伪造、
  未知 payload_version 降级、Workspace 隔离、参数校验与只读 API。
- `tests/ui/test_revision_history_ui.py`（10）：版本列表与字段差异渲染（无原始 JSON/无
  恢复按钮）、操作组对话框、关系状态历史分区、旧数据/无历史提示、未知快照降级、长文本
  只读全文、归档人物历史、面板入口按钮、People 页面按钮打开对话框、Event 详情按钮。

### 6. 已知问题和风险

1. 差异以完整快照对比展示，未做逐字段折叠/统计；长文本用全文对话框处理。
2. 关系状态历史分区独立分页（不把两股数据混排为单一分页流）。
3. 未知快照只能显示审计元数据，不能 diff/revert（设计选择，不猜测）。
4. 历史 UI 只读；恢复字段能力属 14.4，未提前实现。
5. 入口按钮未覆盖 Workspace 历史（设计优先级为 Person/Relationship/Event/Evidence/
   Memory）；Workspace 历史仍经后续或 SQL 查看。

### 7. 下一阶段（14.4）前置条件

- ✅ 历史查询/差异/操作组/旧数据提示/未知快照降级均可用并有测试。
- ✅ UI 只读入口与操作组展示已就绪。
- ⏳ 14.4 需要：恢复白名单、预览+确认、经原 Service 更新、REVERT 审计、完整性检查、
  禁止恢复项测试与 UI 恢复入口。
- 14.4 禁止：改写旧 ChangeLog、恢复不可变字段/关联、绕过既有 Service。

### 8. Git 状态

- 分支：`phase-14-revision-changelog`
- 本阶段提交：`36b7cbe`（Service + UI + 测试）；文档提交紧随本记录
- 是否推送远端：**否**

---

## Phase 14.4 — Controlled Revert & Integrity

### 1. 本阶段结论

**完成**：实现白名单字段的受控恢复（预览 → 明确确认 → 经原 Service 写入 → 新 REVERT
Revision；旧历史不改不删），并为 ChangeLog 增加只读完整性审计。禁止恢复的字段/实体/状态
均被明确拒绝；失败不产生新版本；UI 无静默恢复入口。

### 2. 实际修改内容

- **Service**：新增 `app/services/change_revert.py`（`ChangeRevertService`：
  `preview`/`revert`、白名单、目标快照校验、当前实体校验、名称冲突、`REVERT` 审计元数据、
  经 people/events/evidence/memories/workspaces 的既有 update 方法写入）。
- **审计通道**：`ChangeOperationContext` 增加 `action_override`/`metadata`；
  `ChangeLogWriter.record` 将原 UPDATE 标记为 REVERT 并写入
  `metadata={"source_revision_no": n}`；五个 update 方法新增内部 `operation=` 透传参数
  （默认行为不变，既有测试无需修改）。
- **Integrity**：`DataIntegrityService` 新增只读 ChangeLog 检查与独立
  `table_change_log`/`change_log_rows_checked` 计数；错误码：
  `CHANGE_LOG_ORPHAN_ENTITY`、`CHANGE_LOG_WORKSPACE_MISMATCH`、
  `CHANGE_LOG_INVALID_TYPE`、`CHANGE_LOG_INVALID_ACTION`、
  `CHANGE_LOG_REVISION_GAP`、`CHANGE_LOG_REVISION_DUPLICATE`、
  `CHANGE_LOG_SNAPSHOT_INVALID`、`CHANGE_LOG_PARENT_INVALID`、
  `CHANGE_LOG_OPERATION_INCONSISTENT`。
- **UI**：`RevisionHistoryDialog` 新增"恢复此版本字段…"（仅可恢复且有差异时启用）；
  新增 `RevertPreviewDialog`（字段"当前 → 目标"+ 确认/取消），确认后执行并刷新时间线。
- **Application**：`ApplicationServices.change_revert` 装配；恢复服务与业务服务共享同一
  Writer/Service 实例。
- **未改**：无 schema/migration（仍 0009）；历史行不可修改/删除；无自动修复、无自动恢复。

### 3. 关键设计决定

1. **白名单唯一**：PERSON/EVENT 各 8 字段、EVIDENCE title/annotation、MEMORY
   content/kind/person_id/valid_from/valid_until、WORKSPACE name/description；其余实体
   明确不支持。Association/子集合不随字段恢复。
2. **经原 Service**：恢复不写 SQL、不绕过领域校验/事务/审计；REVERT 是新的业务操作，
   产生新 Revision，旧版本只读。
3. **明确确认**：`confirmed=True` 为必需参数；UI 必须经过预览对话框；无变化不写。
4. **失败即拒绝且无副作用**：归档/删除、快照不可用、名称冲突、非法目标值、确认缺失均在
   写入前拒绝；审计写入失败整体回滚。
5. **完整性审计只报告**：多态引用无法用外键表达，ChangeLog 检查作为补偿控制，只报告不修复。
6. **无静默恢复**：UI 不为无差异/不支持/归档版本启用按钮，并显示原因。

### 4. 数据库变化

**无**：未新增 migration、未改 schema（仍 `0009_change_log`）。新审计行与既有表结构一致。

### 5. 测试运行命令与结果

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

- **1756 collected / 1751 passed / 5 environment-skipped / 0 failed（退出码 0，9:07）**
- 增量核对：1733（Phase 14.3 完成后）+ 23（Phase 14.4 新增：revert 12 +
  integrity 7 + UI 4）= 1756；既有测试无删除/弱化，5 项 Windows symlink skip 保持原义。

新增测试：

- `tests/services/test_change_revert.py`（12）：Person 恢复产生 REVERT 新版本且旧行不变、
  白名单过滤、不支持实体/未知快照/归档拒绝、确认必需、无变化不写、Workspace 名称冲突不写、
  Workspace description 恢复、Event 日期与 Evidence 元数据恢复（content_text 不变）、
  Memory person/validity 的 None 往返、审计写入失败回滚、参数校验。
- `tests/services/test_change_integrity.py`（7）：干净数据无 ChangeLog 错误与计数、孤儿实体、
  Workspace 不一致、版本缺口、快照非法、父引用无效、跨 Workspace 操作、Workspace 过滤。
- `tests/ui/test_revision_history_ui.py`（+4，共 14）：恢复按钮仅对有效目标启用、归档阻止、
  确认对话框字段清单、执行恢复后出现 REVERT 版本与提示。

### 6. 已知问题和风险

1. 恢复只覆盖单实体白名单字段；关联（alias/participants/media）需用户显式操作。
2. 恢复目标版本必须存在且快照可解析；不具备历史的数据无法恢复（不伪造）。
3. 归档实体的字段恢复被拒绝：需先经既有恢复功能；Person/Workspace 当前没有恢复功能，
   因此其归档记录只能查看历史。
4. 名称冲突在预览即阻止，但并发下仍可能由 Service 最终拒绝（last-write-wins 之外的既有
   ConflictError 语义）。
5. 完整性审计的 `CHANGE_LOG_REVISION_DUPLICATE`/`INVALID_*` 主要防御未来/绕过写入；
   正常 CHECK 约束下难以触发，测试覆盖可触发子集。

### 7. 下一阶段前置条件

- ✅ Phase 14 全部子阶段（14.0–14.4）完成：审计写入、历史/差异 UI、受控恢复、完整性检查。
- ✅ 旧历史不可变、恢复产生新版本、失败无副作用、UI 明确确认均有测试。
- ⏭ 任何后续阶段（如 Sync/自动 Memory/OCR/RAG/向量库）必须先由用户明确批准设计，
  不自动推进；不得引入自动模型调用/自动同步/自动上传。

### 8. Git 状态

- 分支：`phase-14-revision-changelog`
- 本阶段提交：`a1f471b`（Service + UI + 完整性检查 + 测试）；文档提交紧随本记录
- 是否推送远端：**否**

---

## Phase 14 总结

- **14.0** 设计（审计矩阵/契约/不可变边界）✅
- **14.1** `change_log` append-only 持久化基础（迁移 0009）✅
- **14.2** 业务写操作同事务捕获（8 类实体、operation 分组、失败回滚）✅
- **14.3** 只读历史/差异/操作组 UI（5 类实体入口、旧数据提示、未知快照降级）✅
- **14.4** 白名单受控恢复（REVERT 新版本、旧历史不可变）+ ChangeLog 完整性审计 ✅

Phase 14 完成后仍是单用户、本地优先、不联网、不同步、无账号的应用；ChangeLog 是审计/
可解释性基础设施，不是 Event Sourcing、不是多端同步机制，也不授权任何自动写入。

---

## 最终回归汇总（每子阶段更新）

| 阶段完成后 | Collected | Passed | Failed | Skipped | Exit |
|---|---|---|---|---|---|
| 14.0（设计，无代码改动） | 1660 | 1655 | 0 | 5 | 0 |
| 14.1（ChangeLog 持久化基础） | 1686 | 1681 | 0 | 5 | 0 |
| 14.2（业务事务接入） | 1712 | 1707 | 0 | 5 | 0 |
| 14.3（只读历史与差异 UI） | 1733 | 1728 | 0 | 5 | 0 |
| 14.4（受控恢复与完整性） | 1756 | 1751 | 0 | 5 | 0 |
