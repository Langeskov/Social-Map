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

## Phase 14.3 — Revision History & Diff Viewer

（待实施）

---

## Phase 14.4 — Controlled Revert & Integrity

（待实施）

---

## 最终回归汇总（每子阶段更新）

| 阶段完成后 | Collected | Passed | Failed | Skipped | Exit |
|---|---|---|---|---|---|
| 14.0（设计，无代码改动） | 1660 | 1655 | 0 | 5 | 0 |
| 14.1（ChangeLog 持久化基础） | 1686 | 1681 | 0 | 5 | 0 |
