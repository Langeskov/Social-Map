# Phase 14 设计文档 — Revision & ChangeLog：实体修订历史与操作审计

> 状态：**Phase 14 设计契约（已批准；14.1–14.4 已全部实现并通过验收）**。
> 本文档是最终设计契约与边界；各子阶段的实际实施记录见
> [Phase 14 验收记录](<phase14-verification.md>)。
> 历史注记：14.0 设计编写时的 HEAD 为 `582e996`、Alembic head 为 `0008_memory`；
> 14.1 引入迁移 `0009_change_log` 后 head 未再变化（截至 Phase 14.4 完成）。

---

## 1. 背景与目标

用户需要回答：某条人物/关系/事件/证据何时被修改、修改前是什么、哪些字段变了、多个关联
修改是否属于同一次操作、某些字段能否恢复到历史值。

Phase 14 建立**追加式 ChangeLog**：一次真实发生的业务变更写一行（或同一操作多行），
Revision 是**按实体查询出来的逻辑视图**，不建独立 Revision 表。

### 1.1 目标

1. 记录 Service 层的业务写操作（创建/更新/归档/恢复/关联变化/领域历史追加）。
2. 业务写入与 ChangeLog 写入**同一事务**，一起提交或一起回滚。
3. 以稳定、版本化、白名单化的 JSON 快照记录前后状态。
4. 支持按实体、按操作组、按时间读取历史；支持查看字段差异。
5. 对白名单内普通字段提供**受控恢复**：恢复=新的业务操作+新 ChangeLog，永不改写旧记录。
6. 保持 Phase 1–13 行为、Workspace 隔离、不可变性约束与既有测试。

### 1.2 非目标（明确不做）

Event Sourcing 重构、全库时间旅行、Workspace 任意时点恢复、多用户/同步/云、权限系统、
自动重建历史、LLM 审查变更、多版本自动合并、数据库级事务回放、修改 RelationshipHistory
与 Evidence 的既有不可变规则、改写 AnalysisRun/Evaluation 架构、复制媒体二进制。

---

## 2. 当前架构盘点（以实际代码为准）

### 2.1 持久化模型（`app/storage/models.py`）

- 11 张业务事实表：`workspace, person, person_alias, media, person_media, relationship,
  relationship_history, event, event_participant, evidence, evidence_media`。
- 投影：`search_document` + FTS5（可重建，非事实）。
- 分析层：`analysis_run` + 4 张 `analysis_*_source`。
- 用户记忆：`memory`。
- 类型：`Uuid` 主键、`UTCDateTime`（带时区）、`String/Text/Integer/Date`；约束大量使用
  复合外键 `(workspace_id, id)`、`UNIQUE(workspace_id, id)`、CHECK。
- ORM 守卫（不是数据库触发器，不防管理员 raw SQL）：
  - `RelationshipModel.before_update` 禁止改身份字段；
  - `RelationshipHistoryModel.before_update/before_delete` 只允许受控的 `event_id` 关联；
  - `EvidenceModel.before_update/before_delete` 禁止改原始内容。
- 所有事实行带 `workspace_id`；删除均为软删除（`deleted_at`），`person_alias` 与
  关联行是硬删除（`remove`）。

### 2.2 事务与 Session

- `app/services/unit_of_work.py::transaction(factory, write=False)`：一次操作一个
  `Session`、一个事务、确定性关闭；`write=True` 时设置 `ris_write=true`（SQLite
  `BEGIN IMMEDIATE` 由存储层按该 execution option 实现）；异常统一回滚并转 `DatabaseError`
  （SQLAlchemy 异常）或原样抛出（其他）。
- `app/services/operations.py::write_operation(factory)`：写操作唯一入口，内部就是
  `transaction(write=True)`。
- Repository 不发 Session、不 commit/rollback；Service 拥有事务边界。
- **不支持嵌套事务**：Service 若在 `write_operation` 内再调用另一个 Service 的写方法，
  会产生第二个独立事务（见 §8.4 Media 导入两阶段），ChangeLog 捕获必须以“实际写库的
  那个事务”为准。

### 2.3 Workspace 隔离

- `app/services/rules.py::workspace_required`：写/读前校验 Workspace 存在且未归档。
- 事实表用复合外键把跨 Workspace 引用变成结构上不可达。
- 查询一律 `workspace_id` 优先；UI 只有单一 `WorkspaceContext`。
- ChangeLog 必须遵循同样隔离：每行 `workspace_id` 外键到 `workspace.id`，所有查询强制
  过滤当前 Workspace；禁止跨 Workspace 读取。

### 2.4 Service 写操作清单（实际方法）

| Service | 写方法 | 事务内实际写入 | 备注 |
|---|---|---|---|
| WorkspaceService | `create` | workspace 行 | 名称唯一（含归档） |
| WorkspaceService | `update` | workspace.name/description/updated_at | 名称冲突检查 |
| WorkspaceService | `soft_delete` | workspace.deleted_at | 仅当无有效人物/媒体 |
| PersonService | `create`（无头像） | person 行 | `EDITABLE` 字段白名单 |
| PersonService | `create`（带头像） | **MediaService 事务内**写 media + person + person_media | 人物写入发生在 MediaService；两阶段文件+DB |
| PersonService | `update` | person 字段、updated_at | 无变化也保存（需 no-op 判定） |
| PersonService | `soft_delete` | 删除该人物全部 person_media + person.deleted_at | 归档解绑媒体 |
| PersonService | `add_alias` / `remove_alias` | person_alias 行 / 硬删除 | alias 无 UPDATE |
| PersonService | `set_avatar` / `add_gallery` | person_media（AVATAR 旧行删除） | 经 `person_links.attach_media` |
| PersonService | `remove_media_reference` | 删除 person_media 行 | 只解绑，不删文件 |
| MediaService | `import_file` | 媒体文件 publish + `_persist` 事务：media 行、person（`_new_person`）、person_media | 去重、文件补偿独立于 DB 回滚 |
| MediaService | `soft_delete` | media.deleted_at | 仍被引用时拒绝 |
| RelationshipService | `create` | relationship + 初始 RelationshipHistory | 同身份唯一 |
| RelationshipService | `add_history` | history 行 + relationship.updated_at | 追加式领域历史 |
| RelationshipService | `soft_delete` / `restore` | relationship.deleted_at | 不改 History |
| EventService | `create_event` | event + participants | 同一事务 |
| EventService | `update_event` | event 字段 + participants 增删改 | 同一事务，需同一 operation |
| EventService | `add_participant` / `remove_participant` | participant 行 + event.updated_at | 同一事务 |
| EventService | `soft_delete_event` / `restore_event` | event.deleted_at | 不动 participant/history |
| EventService | `attach_event_to_history` | relationship_history.event_id | ORM 受控关联 |
| EvidenceService | `create_evidence` | evidence + evidence_media 行 | 同一事务 |
| EvidenceService | `update_metadata` | evidence.title/annotation/updated_at | content_text 等不可改 |
| EvidenceService | `add_media` / `remove_media` | evidence_media 行 + evidence.updated_at | 附件快照冲突保护 |
| EvidenceService | `soft_delete_evidence` / `restore_evidence` | evidence.deleted_at | 不动附件 |
| MemoryService | `create` / `create_from_analysis` | memory 行 | 确认闸门；ANALYSIS 需成功 run |
| MemoryService | `update` | content/kind/person/validity/updated_at | 来源字段不可改 |
| MemoryService | `soft_delete` / `restore` | memory.deleted_at | 软删除 |
| AnalysisService | `create_run/cancel_run/delete_run` | analysis 表 | **不纳入普通 Revision**（§4.9） |

### 2.5 UI 导航与详情入口（14.3 将挂载历史入口）

- `PeoplePage`（`app/ui/people_page.py`）：人物索引 + 档案 + Ego Network + Inspector
  （关系/Activity/Evidence/Memory/Analysis Tab）；人物操作按钮集中在详情区。
- `EventDetailView` / `EventDetailDialog`（`app/ui/event_detail.py`）；`EventsPage` 列表。
- `EvidencePanel`（Event 详情内）+ `EvidenceReader` / `EvidenceDetailDialog`
  （`app/ui/evidence_dialog.py`）。
- `RelationshipPanel`（`app/ui/relationship_panel.py`）：关系状态历史、History 查看。
- `MemoryPanel`（`app/ui/memory_panel.py`）：Person / Workspace 记忆。
- `SettingsPage`（`app/ui/settings_page.py`）：Data Management（审计/备份/校验/导出）。
- 所有 UI 经 Service/DTO，不持有 ORM 对象；错误边界统一 `run_operation`
  （`app/ui/operations.py`）。

### 2.6 Analysis / Memory 边界

- `analysis_run` 与来源表是 DERIVED/ANALYSIS 溯源，不是事实修订；14.x 不为每次分析写
  Revision，不复制模型输入/输出/Telemetry。
- `Memory` 是 USER 层，纳入审计（创建/更新/删除/恢复），但不改变其确认闸门和来源语义。
- AnalysisRun 删除（`AnalysisService.delete_run`）的生命周期审计**不纳入 14.2 范围**；
  如需记录属后续独立需求（避免默认扩大审计面）。
- 评估（`app/evaluation/`）与 Phase 13 遥测不受影响。

### 2.7 迁移与测试机制

- `alembic/versions/0001…0008_memory`，当前 head `0008_memory`；`alembic/env.py` 使用
  `app.config.settings.AppConfig` 注入。
- 测试库经 `initialize_database(AppConfig(data_dir=tmp))` 升级到 head。
- 断言 head/表集合的测试（14.1 必须同步更新）：
  `tests/test_application_startup.py:63`、`tests/database/test_initialization.py:34-35,52-70`、
  `tests/migrations/schema_facts.py`、`tests/migrations/test_analysis_schema.py`
  （多处 `0008_memory` 断言与 offline downgrade SQL）、
  `tests/migrations/test_empty_database_migrations.py`（`FORMAL_HEAD`/`HEAD_TABLES`）。
- 现有基线：1655 passed / 5 skipped（Phase 13.5 末，本次 14.0 实跑结果见验收记录）。

---

## 3. 业务变更语义

### 3.1 四种时间严格分离

| 时间 | 归属 | 例子 |
|---|---|---|
| Event Time | Event 业务数据（`occurred_on/started_at/ended_at`） | 聚会发生于 2026-05-12 |
| Effective Time | RelationshipHistory（`effective_at`） | 两人自 2026-03-01 建立关系 |
| Recorded Time | ChangeLog `recorded_at` | 用户 2026-11-22 修改职业 |
| Analysis Time | AnalysisRun / Telemetry | 某次分析何时执行、模型与输入输出 |

ChangeLog 只记录 **Recorded Time**；不得把 `occurred_on`/`effective_at`/`started_at` 当作
记录时间，也不得因 ChangeLog 改变这些领域字段的含义。

### 3.2 ChangeLog 与 RelationshipHistory 的分工

- `RelationshipHistory` 是**领域历史**：关系状态在现实中的生效序列，追加式，已有 ORM
  守卫保护；Phase 14 不修改、不覆盖、不删除它。
- `ChangeLog` 是**操作审计**：用户在本软件里做了什么（创建关系、追加一条状态、把某条
  History 关联到 Event、归档关系……）。
- 恢复功能不允许通过改写/删除 History 实现；关系状态的"回到过去"只能通过**追加新的
  History**（既有业务操作）完成。
- Relationship 的端点、类型、方向在 ChangeLog 快照中可见，但**永不作为可恢复字段**。

### 3.3 什么算一次业务操作

- 一次 Service 公开写方法（用户的一次明确动作）产生一个 `operation_id`。
- 同一事务内为完成该动作而写入的多个实体/行共享该 operation_id（Event+Participants；
  Evidence+EvidenceMedia；Person 归档+PersonMedia 解绑；Person+Avatar）。
- 嵌套 helper（`person_links.attach_media`、`MediaService._persist`）接收**外层**的
  operation 上下文，不再新建 operation。
- 读取、UI 状态变化、投影刷新（`search_document`）、`updated_at` 等系统维护字段**不产生**
  Revision。
- 无实际变化的更新（值相同/空变更/验证失败/回滚）不写 ChangeLog（§5.6）。

---

## 4. 实体审计矩阵

实体类型枚举（`entity_type`）：
`WORKSPACE, PERSON, RELATIONSHIP, RELATIONSHIP_HISTORY, EVENT, EVIDENCE, MEDIA, MEMORY`。

> Alias、PersonMedia、EventParticipant、EvidenceMedia **不单独建实体类型**：它们是所属聚合的
> 子集合，作为父实体快照中的列表字段记录（见下），从而保证"一次 Event 更新包含参与人变化"
> 出现在同一实体时间线里，且不需要为稀疏关联建独立审计表。这是 14.0 的明确决策。

### 4.1 Workspace

| 项 | 内容 |
|---|---|
| 纳入 | 是 |
| 操作 | CREATE / UPDATE（name, description）/ ARCHIVE（soft delete） |
| 快照字段 | `name`, `description` |
| 不可变/排除 | id、created_at/updated_at/deleted_at（生命周期用 action 表达） |
| 可恢复 | name/description（名称唯一冲突时拒绝）；无归档恢复 Service，故不提供 ARCHIVE 撤销 |
| 关联 | 无子集合 |

### 4.2 Person

| 项 | 内容 |
|---|---|
| 纳入 | 是 |
| 操作 | CREATE / UPDATE / ARCHIVE；头像/Gallery 关联变化、Alias 增删 → 同一行 UPDATE |
| 快照字段 | `display_name, real_name, gender, birthday, occupation, organization, location, notes`；子集合 `aliases[]`（alias, alias_type, note, id 排序）与 `images[]`（media_id, role, sort_order） |
| 不可变/排除 | id、workspace_id、created_at、updated_at、deleted_at（生命周期 action 表达） |
| 可恢复 | 上述普通字段（不含 aliases/images 集合）；归档人物**当前无恢复 Service**，14.x 不提供恢复入口 |
| 关联 | 归档时 PersonMedia 解绑由 Service 执行；快照展示解绑结果，但恢复人物不会自动恢复媒体（§12.3） |

### 4.3 Alias（语义映射到 PERSON）

| 项 | 内容 |
|---|---|
| 纳入口径 | 通过 PERSON 的行内 `aliases[]` 集合变化记录（不建 ALIAS 实体） |
| 操作 | 添加（集合变化）/ 删除（硬删除，集合变化） |
| 快照 | 列表元素：`alias`, `alias_type`, `note`；按 `alias, alias_type, note` 稳定排序 |
| 不可变/排除 | alias 行 id 不入快照（行可被硬删除）；无独立 UPDATE 操作（代码事实） |
| 可恢复 | 集合字段不自动恢复（删除别称的恢复需用户手工重新添加） |

### 4.4 Relationship

| 项 | 内容 |
|---|---|
| 纳入 | 是 |
| 操作 | CREATE / ARCHIVE / RESTORE；身份不可改；`add_history` 记在 RELATIONSHIP_HISTORY |
| 快照字段 | `person_a_id, person_b_id, relationship_type, directionality`（**只读展示**） |
| 不可变/排除 | 上述身份字段永不恢复；created_at/updated_at/deleted_at |
| 可恢复 | 无（身份不可改；生命周期恢复走现有 `restore()`，不走快照恢复） |
| 关联 | History 行是独立实体 `RELATIONSHIP_HISTORY`，`parent=(RELATIONSHIP, relationship_id)` |

### 4.5 RelationshipHistory

| 项 | 内容 |
|---|---|
| 纳入 | 是（父实体=RELATIONSHIP） |
| 操作 | APPEND_HISTORY（新增状态）/ ATTACH_EVENT / DETACH_EVENT（受控 `event_id` 关联） |
| 快照字段 | `relationship_id, state, effective_at, note, event_id` |
| 不可变/排除 | id、created_at；state/effective_at/note 创建后不可改（领域规则） |
| 可恢复 | 无（只能追加新 History；禁止改写/删除） |
| 完整性 | 该行不可作为恢复目标；ORM 守卫保持现状 |

### 4.6 Event

| 项 | 内容 |
|---|---|
| 纳入 | 是 |
| 操作 | CREATE / UPDATE / ARCHIVE / RESTORE；Participant 增删改 → 同一 EVENT 行 UPDATE（集合 `participants[]`） |
| 快照字段 | `title, event_type, record_kind, occurred_on, started_at, ended_at, location, description`；`participants[]`（person_id, role, note，按 person_id 排序） |
| 不可变/排除 | id、workspace_id、created_at/updated_at/deleted_at；participants 行 id/created_at 不入快照 |
| 可恢复 | 上述普通字段（不含 participants 集合；参与人增删需用户手工操作）；生命周期用现有 `restore_event()` |
| 关联 | 与 RelationshipHistory 的关联变化记在 RELATIONSHIP_HISTORY（ATTACH/DETACH），同时属于同一 operation |

### 4.7 Evidence

| 项 | 内容 |
|---|---|
| 纳入 | 是 |
| 操作 | CREATE / UPDATE（title/annotation）/ ARCHIVE / RESTORE；附件关联变化 → 同一 EVIDENCE 行 UPDATE（`media[]`） |
| 快照字段 | `title, annotation`；`media[]`（media_id, role，按 media_id,role 排序） |
| 不可变/排除 | `content_text, source_type, source_reference, source_label, captured_at, event_id, kind`（保持既有只读规则，**永不进入快照**，避免重复大文本）；created_at/updated_at/deleted_at |
| 可恢复 | 仅 `title`, `annotation`；content_text 不可恢复（本来就不可改） |
| 关联 | EvidenceMedia 增删记在 EVIDENCE 集合字段；不复制媒体二进制 |

### 4.8 Media

| 项 | 内容 |
|---|---|
| 纳入 | 是 |
| 操作 | IMPORT（新导入或恢复已删除媒体）/ ARCHIVE（仅在无引用时） |
| 快照字段 | `sha256, mime_type, size_bytes, width, height`（文件内容身份与元数据） |
| 不可变/排除 | 二进制文件、`storage_path`（运行时内部路径）；created_at/updated_at/deleted_at |
| 可恢复 | 无（文件与元数据不可字段恢复；关联由所属实体时间线体现） |
| 关联 | PersonMedia/EvidenceMedia 行不单独记；其变化体现在 PERSON/EVIDENCE 的集合字段 |

### 4.9 Memory

| 项 | 内容 |
|---|---|
| 纳入 | 是 |
| 操作 | CREATE（手工/来自 Analysis 的已确认）/ UPDATE / ARCHIVE（软删除）/ RESTORE |
| 快照字段 | `person_id, kind, content, source_type, source_analysis_id, valid_from, valid_until` |
| 不可变/排除 | id、workspace_id、confirmed_at（用户确认时间，系统时间）；deleted_at 用 action |
| 可恢复 | `content, kind, person_id, valid_from, valid_until`；来源字段不恢复（不可改） |
| 保持 | USER 层语义；不得自动创建 Memory；不写事实层 |

### 4.10 AnalysisRun / Evaluation（不纳入）

- 不写普通实体 Revision；不复制模型输入/输出/Telemetry；不改变 Phase 13 评估。
- `AnalysisService.delete_run` 的生命周期审计：**14.x 不实现**，记录为后续可选需求。

### 4.11 矩阵速查

| entity_type | 记录操作 | 可恢复字段 | 生命周期恢复入口 |
|---|---|---|---|
| WORKSPACE | CREATE/UPDATE/ARCHIVE | name, description | 无（不提供） |
| PERSON | CREATE/UPDATE/ARCHIVE | 8 个普通字段 | 无（当前无 restore） |
| RELATIONSHIP | CREATE/ARCHIVE/RESTORE | 无 | 现有 `restore()` |
| RELATIONSHIP_HISTORY | APPEND_HISTORY/ATTACH_EVENT/DETACH_EVENT | 无 | 无 |
| EVENT | CREATE/UPDATE/ARCHIVE/RESTORE | 8 个普通字段 | 现有 `restore_event()` |
| EVIDENCE | CREATE/UPDATE/ARCHIVE/RESTORE | title, annotation | 现有 `restore_evidence()` |
| MEDIA | IMPORT/ARCHIVE/RESTORE | 无 | 重新导入已归档媒体时 RESTORE（同一 Media ID） |
| MEMORY | CREATE/UPDATE/ARCHIVE/RESTORE | content,kind,person_id,valid_from,valid_until | 现有 `restore()` |

---

## 5. ChangeLog 数据契约

### 5.1 Schema（Migration `0009_change_log`）

| 列 | 类型 | 空 | 说明 |
|---|---|---|---|
| `id` | Uuid | 否 | 主键，应用侧 uuid4 |
| `workspace_id` | Uuid | 否 | FK → `workspace.id`（真实外键） |
| `entity_type` | String(30) | 否 | CHECK 枚举（§4 的 8 类） |
| `entity_id` | Uuid | 否 | 被审计实体标识（**无跨表外键**，见 §6） |
| `parent_entity_type` | String(30) | 是 | 聚合父类型（目前仅 RELATIONSHIP_HISTORY → RELATIONSHIP） |
| `parent_entity_id` | Uuid | 是 | 聚合父标识；`(type,id)` 同时为空或同时非空 |
| `action` | String(30) | 否 | CHECK 枚举（§5.2） |
| `operation_id` | Uuid | 否 | 一次用户操作的分组键 |
| `revision_no` | Integer | 否 | 该实体历史版本号，CHECK `>= 1` |
| `changed_fields` | Text | 否 | 规范化 JSON 数组（`[]` 或字段名列表，按白名单顺序） |
| `before_snapshot` | Text | 是 | JSON 对象；CREATE/IMPORT 时为 NULL |
| `after_snapshot` | Text | 是 | JSON 对象；归档/删除类仍保留 after（含生命周期语义由 action 表达） |
| `payload_version` | String(10) | 否 | 快照格式版本，初始 `"1"` |
| `metadata` | Text | 是 | 非敏感辅助 JSON（如 `{"source_revision_no": 3}`） |
| `recorded_at` | UTCDateTime | 否 | 记录时间（Recorded Time），同一 operation 共用同一值 |

约束与索引：

- `pk_change_log(id)`；
- `fk_change_log_workspace_id_workspace`：`workspace_id → workspace.id`；
- `uq_change_log_workspace_entity_revision`：`(workspace_id, entity_type, entity_id,
  revision_no)` —— 防同一实体版本重复；
- `uq_change_log_workspace_id_id`：`(workspace_id, id)`（与其他表一致的复合引用目标；
  14.x 无表引用它，保留扩展一致性）；
- `ix_change_log_workspace_entity`：`(workspace_id, entity_type, entity_id, revision_no)`；
- `ix_change_log_workspace_operation`：`(workspace_id, operation_id)`；
- `ix_change_log_workspace_recorded`：`(workspace_id, recorded_at, id)`；
- CHECK：`entity_type IN (...)`、`action IN (...)`、`revision_no >= 1`、
  `parent_entity_type IN (...) OR parent_entity_type IS NULL`、
  `(parent_entity_type IS NULL) = (parent_entity_id IS NULL)`、
  `length(payload_version) > 0`、`length(trim(changed_fields)) > 0`。

JSON 内容校验保持在 Serializer / Repository（与现有 scope/context_snapshot 一致，不对 JSON
加 SQLite 函数约束，避免依赖 JSON1 编译选项）。

**14.1 实施说明**：迁移 `0009_change_log` 与 ORM 按上表落地；`metadata` 是 SQLAlchemy
Declarative 的保留属性名，因此 ORM 属性为 `metadata_json`、数据库列名仍为 `metadata`
（Repository 负责编解码，Domain 字段仍叫 `metadata`）。Repository 只提供 `add`/查询，
无 update/delete；Writer、业务接入、UI 与恢复仍未实现。

### 5.2 action 枚举

`CREATE, UPDATE, ARCHIVE, RESTORE, IMPORT, APPEND_HISTORY, ATTACH_EVENT, DETACH_EVENT,
REVERT`（14.4 引入 REVERT）。

各实体允许的 action 由 Writer 校验（例：MEDIA 允许 IMPORT/ARCHIVE/RESTORE；EVIDENCE 不允许
APPEND_HISTORY）。

### 5.3 revision_no 规则

- 每个 `(workspace_id, entity_type, entity_id)` 独立递增，首次记录为 1；
- 在写事务内 `SELECT MAX(revision_no)+1` 生成；唯一约束最终兜底；
- 不存在"版本 0"或伪造历史；Phase 14 之前创建的实体**没有历史**，其首次记录通常不是
  CREATE，UI 必须明示"Phase 14 生效前的历史不可恢复"；
- 删除/归档同样占用新版本号（历史是追加的）；
- 并发：单用户、SQLite `BEGIN IMMEDIATE` 下写操作串行；极端并发冲突让唯一约束失败并
  整体回滚（不引入乐观锁/重试机制，§5.7）。

### 5.4 operation_id 规则

- 由**最外层 Service 公开写方法**创建 `ChangeOperationContext`（uuid4 + 单一
  `recorded_at`）；
- 同一事务内所有 ChangeLog 行共享；
- 嵌套 helper 接收传入的 context，不再生成新 id；
- 一次操作跨两个事务的情况（Media 导入的文件阶段与 DB 阶段）**只记录实际提交的 DB
  事务**；文件补偿失败不写审计（未产生业务数据）；
- operation 不单独建表：`operation_id` 本身即分组键。

### 5.5 快照格式

- `before_snapshot` / `after_snapshot`：**白名单字段的完整状态对象**（不是全实体复制，
  也不是只存 diff），便于直接回答"该版本时这些字段是什么"；
- CREATE/IMPORT：`before=null`，`after=完整快照`；
- APPEND_HISTORY：新领域历史行也是"此前不存在"，`before=null`、`after=完整历史快照`；
- UPDATE：`before` 与 `after` 都是完整白名单状态，`changed_fields` 精确列出不同字段；
- ARCHIVE/RESTORE：`before/after` 为生命周期动作前后的完整业务快照（不含系统时间），
  action 表达生命周期；
- 子集合（aliases/images/participants/media）以有序数组入快照；数组元素排序规则固定
  （见 §9），避免无意义 diff；
- `changed_fields` 是**业务字段名数组**（白名单名，按白名单声明顺序），UI 映射为中文标签；
- `metadata` 只放非敏感辅助信息（如 revert 来源版本、history 的 relationship_id）。

### 5.6 空操作与虚假 Revision

不记录 ChangeLog 的情况（14.2 统一实现）：

1. 更新请求没有带来实际持久化变化（业务字段值相同）；
2. 仅 `updated_at` 变化（系统维护字段，不算业务变化）；
3. 验证失败、事务回滚、异常路径；
4. 只读操作、投影刷新、UI 状态变化；
5. Service 的幂等返回（如对已删除实体再次 soft_delete、对未删除实体 restore 直接返回
   原值）——若未发生写入，则无审计行；
6. 无实际变化的恢复（14.4：目标字段与当前值相同 → 不写 REVERT，提示无变化）。

### 5.7 不可变性与并发

- **业务不可变**：Repository/Service 只提供 `add` 与查询，不提供 update/delete；
  ORM `before_update`/`before_delete` 抛 `DomainError`（与 RelationshipHistory 相同的
  守卫风格）。
- 不宣称触发器级保护：管理员 raw SQL/Core bulk 仍可绕过；文档与测试都明确这一点。
- ChangeLog 不代表解决并发写入问题：不引入 `expected_updated_at`、不改现有 last-write-wins
  语义、不宣称 Revision 等价乐观锁。
- 历史清理/脱敏在 14.x **不提供 API**（§10）。

---

## 6. 实体引用与完整性决策

`entity_type + entity_id` 是多态引用，SQLite 无法用一个普通外键指向多张表。比较：

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 多态引用 `entity_type/entity_id` | 一张表覆盖所有实体；新增实体只加枚举；查询简单 | DB 无法外键到实体；需 Service/完整性测试兜底；孤儿风险 | **采用** |
| B. 每类型独立关联表/引用表 | 可有外键完整性 | 8 张近乎空的表；每加实体要迁移；查询需 UNION；维护成本高 | 拒绝 |
| C. 多态 + SQLite 触发器校验存在性 | DB 级更强 | 触发器按类型分支复杂；迁移与维护成本高；项目一贯不用触发器表达业务规则；禁用外键即可绕过 | 拒绝（记录为备选） |

**采用 A 的补偿措施**（14.1/14.4 落地）：

1. `workspace_id` 是**真实外键** → 历史不可能属于不存在的 Workspace；
2. `entity_type` 有 CHECK 枚举 → 类型不可乱写；
3. Writer 在写事务内校验：实体存在（按类型查对应表，允许已归档）、
   `entity.workspace_id == change_log.workspace_id`、`entity_id` 有效 UUID；
4. `parent` 组合校验：类型枚举 + 同时为空/非空 + 父实体存在且同 Workspace；
5. 完整性审计新增检查（14.4）：
   - `CHANGE_LOG_ORPHAN_ENTITY`：实体行不存在（允许软删除，但行仍应存在）；
   - `CHANGE_LOG_WORKSPACE_MISMATCH`：实体 workspace 与日志 workspace 不同；
   - `CHANGE_LOG_INVALID_TYPE/ACTION`；
   - `CHANGE_LOG_REVISION_GAP/DUPLICATE`（按实体检查连续性）；
   - `CHANGE_LOG_SNAPSHOT_INVALID`：JSON 不可解析、payload_version 未知；
   - `CHANGE_LOG_OPERATION_INCONSISTENT`：同 operation 跨 Workspace；
6. 测试覆盖孤儿、类型错误、跨 Workspace、父引用错误。

**已知限制**：数据库层不阻止"合法 UUID 但实体后来被硬删除"（alias/关联行硬删除不产生
实体；日志审计的是聚合实体，聚合实体均为软删除，故孤儿只可能来自管理员 raw SQL）。
该风险在文档与完整性审计中显式保留，不宣称解决。

---

## 7. 操作分组规则

| 业务操作 | operation 行 | 分组示例 |
|---|---|---|
| Event 更新（字段+参与人） | 1 个 EVENT UPDATE 行（participants 在快照内） | 一次 operation 一行 |
| Event 更新 + 关联 History | EVENT UPDATE + RELATIONSHIP_HISTORY ATTACH_EVENT | 两行同 operation |
| Evidence 创建 + 附件 | 1 个 EVIDENCE CREATE（media 在快照内） | 一次 operation |
| Person 创建 + 头像 | PERSON CREATE + MEDIA IMPORT（同一 Media 事务） | 两行同 operation |
| Person 归档 | 1 个 PERSON ARCHIVE（images 快照清空） | 一次 operation |
| 关系创建 | RELATIONSHIP CREATE + RELATIONSHIP_HISTORY APPEND_HISTORY | 两行同 operation |
| 关系状态追加 | RELATIONSHIP_HISTORY APPEND_HISTORY | 一次 operation |
| 媒体导入并挂到人物 | MEDIA IMPORT + PERSON UPDATE（images） | 两行同 operation |
| Memory 从分析确认创建 | MEMORY CREATE（来源字段入快照；不动 AnalysisRun） | 一次 operation |

禁止：同一实体在一次操作中产生两条重复 Revision；同一 SQL UPDATE 单独成为一个 operation；
跨 Workspace 的 operation。

---

## 8. 事务边界

### 8.1 总原则

- 业务写入 + ChangeLog 写入在**同一个 `write_operation`** 内完成，共同 commit/rollback；
- 不允许业务数据提交后用独立事务异步补写；
- ChangeLog 写入失败（序列化异常、DB 约束失败）必须向外传播 → 整个事务回滚；
- UI 层只感知一次 Service 调用的成功/失败。

### 8.2 流程一：Event 更新 + Participant 同步（`EventService.update_event`）

```text
update_event(workspace, id, **changes, participants=...)
└─ write_operation (Session S, BEGIN IMMEDIATE)
   ├─ _get(old event) / 读取 current participants
   ├─ replace + repositories.events.update(entity)
   ├─ participants.remove(...) / update_metadata(...) / add(...)
   ├─ ChangeLogWriter.record(EVENT, id, action=UPDATE)
   │    ├─ before = snapshot(old_event, old_participants)
   │    ├─ after  = snapshot(saved_event, new_participants)
   │    ├─ changed_fields = diff(before, after)
   │    └─ repository.add(ChangeLogEntry(... same Session))
   └─ session.commit()  ← 业务 + 审计一起提交；任一步异常整体回滚
```

operation_id 在方法入口创建，一次调用只有一个。

### 8.3 流程二：Evidence 创建 + Media 关联（`EvidenceService.create_evidence`）

```text
create_evidence(...)
└─ write_operation
   ├─ workspace/event/media 校验
   ├─ repositories.evidence.save(evidence)
   ├─ repositories.evidence_media.add(...) ×N
   ├─ ChangeLogWriter.record(EVIDENCE, id, action=CREATE)
   │    before = null；after = {title, annotation, media:[...]}
   └─ commit（证据、附件、审计同一事务）
```

content_text 不进入快照；媒体文件不复制。

### 8.4 流程三：Person 归档 + PersonMedia 解绑（`PersonService.soft_delete`）

```text
soft_delete(...)
└─ write_operation
   ├─ person_required(old)
   ├─ repositories.person_media.remove_for_person(...)（解绑全部）
   ├─ repositories.people.save(replace(old, deleted_at=now, updated_at=now))
   ├─ ChangeLogWriter.record(PERSON, id, action=ARCHIVE)
   │    before.images = 原引用列表；after.images = []；changed_fields=["images"]
   └─ commit（解绑、归档、审计同一事务）
```

恢复人物（14.x 无此 Service）如未来实现，必须走新的业务方法并产生 RESTORE 行，且**不得**
自动重建 PersonMedia。

### 8.5 Media 导入的两阶段边界

`MediaService.import_file`：文件 publish（`MediaStore`，不在 DB 事务内）→
`_persist` 内的 `write_operation` 写 media/person/person_media。审计只在 `_persist`
提交时写入；文件补偿（`_compensate`）不产生审计记录。人物创建带头像时，PersonService
把 operation context 传给 MediaService，由 `_persist` 统一记录 PERSON CREATE + MEDIA
IMPORT。

---

## 9. 快照序列化规则（`payload_version = "1"`）

1. 数据格式：JSON 对象（`before_snapshot`/`after_snapshot`）与 JSON 数组
   （`changed_fields`/子集合）；
2. 序列化：`json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`
   → 稳定字节序，便于对比与哈希；
3. 类型编码：
   - `datetime`：UTC，`.astimezone(timezone.utc).isoformat()`（含 `+00:00`）；
   - `date`：`YYYY-MM-DD`；
   - `UUID`：小写标准字符串；
   - `None` → `null`；布尔→true/false；整数原样；字符串原样（不 trim，保持已存值）；
4. 数组排序：`aliases` 按 `(alias, alias_type, note)`；`images` 按
   `(role, sort_order, media_id)`；`participants` 按 `person_id`；`media` 按
   `(media_id, role)` —— 与 UI 展示顺序解耦，保证 diff 稳定；
5. 字段白名单是唯一来源：Serializer 只接受 §4 声明的字段；多余/缺失字段直接报错；
6. 不存 ORM 对象、Session、二进制、媒体文件、`updated_at`/`created_at`/`deleted_at`
   等系统时间；
7. 前向兼容：读取端遇到未知字段/未知版本时 `ChangeLogEntry.snapshot_error` 标记并降级
   （UI 显示"无法解析此版本快照"），不崩溃、不猜测；可恢复只支持当前 `payload_version`
   的已知字段；
8. 大小：快照只含业务字段；Evidence 不含 `content_text`；Memory 内容上限 4000 字符
   （模型约束）；列表长度受业务体量限制。不在 14.x 引入压缩。

---

## 10. 隐私与数据保留策略

- ChangeLog 快照会保留**旧的个人信息**（改名前姓名、旧备注等），与当前数据一样属本地
  私人数据；仅存本机数据库，不上传。
- 14.x 不提供历史清理/脱敏/删除 API；ChangeLog 追加式，业务接口不可改/删。
- Workspace：只支持归档（且要求无有效人物/媒体），**不支持硬删除**；归档不删除其
  ChangeLog；历史查询对归档 Workspace 仍可读（只读、按 Workspace 隔离）。
- 用户删除数据库文件 = 删除全部历史；本阶段不设计导出后删除流程。
- 备份：`BackupService` 用 SQLite online backup 复制整库 → **自动包含 ChangeLog**；
  恢复备份 = 整库恢复（包含历史）。
- 导出：现有 `WorkspaceExportService` 默认导出事实/记忆/分析元数据，**14.x 不把
  ChangeLog 加入默认导出**（导出用于数据可携带，不是审计副本）。如未来需要，
  以显式选项 `include_changelog=False` 默认关闭，并在文档中声明"导出含历史快照的旧个人
  信息"——本阶段不实现该选项。
- 日志：与现有规则一致，不把快照内容写入技术日志。

---

## 11. 导出与备份策略（落地口径）

| 场景 | 行为 | 14.x 是否改动 |
|---|---|---|
| 数据库备份 / 校验 / 恢复 | 整库复制，天然包含 `change_log` | 否（自动生效） |
| 备份 manifest | schema_revision 变为 `0009_change_log` 后，旧备份仍按原 manifest 校验 | 否 |
| Workspace 导出 | 不包含 ChangeLog（不改变现有导出 JSON 结构） | 否 |
| 导出历史 | 未来显式选项；本阶段只读 UI + 本地文件 | 延后 |
| 完整性审计 | 14.4 增加 ChangeLog 检查；行数计数是否纳入另行决定（建议作为独立 `counts` 键，不改变 fact_tables 计数语义） | 14.4 |

---

## 12. 恢复规则（14.4 使用；14.0 只定义）

### 12.1 可恢复字段白名单

| entity_type | 可恢复字段 |
|---|---|
| PERSON | display_name, real_name, gender, birthday, occupation, organization, location, notes |
| EVENT | title, event_type, record_kind, occurred_on, started_at, ended_at, location, description |
| EVIDENCE | title, annotation |
| MEMORY | content, kind, person_id, valid_from, valid_until |
| WORKSPACE | name, description（名称唯一冲突 → 拒绝） |
| RELATIONSHIP / RELATIONSHIP_HISTORY / MEDIA | **无**（只读历史） |

### 12.2 禁止恢复

- 主键、workspace_id、created_at/updated_at/deleted_at、confirmed_at 等系统字段；
- Relationship 端点/类型/方向；
- RelationshipHistory 的 state/effective_at/note（只能追加新历史）；
- Evidence 的 content_text/kind/source_*/captured_at/event_id；
- Memory 的 source_type/source_analysis_id；
- Media 二进制与 storage_path；
- 任何子集合（aliases/images/participants/media）：关联恢复需显式业务操作，不由快照自动重建。

### 12.3 执行流程

1. 读目标 Revision 快照（白名单字段）；
2. 校验实体存在（允许归档？——仅对现有 Service 允许的对象；归档实体拒绝恢复并提示先恢复）；
3. 校验白名单与目标版本 payload_version 已知；
4. 计算与当前值的差异；全部相同 → 不写任何记录，提示"无变化"；
5. 展示将修改的字段（旧值/新值），用户明确确认；
6. 通过**原有 Service** 执行更新（如 `PersonService.update`、`EventService.update_event`、
   `EvidenceService.update_metadata`、`MemoryService.update`、`WorkspaceService.update`），
   不直接 UPDATE 表；
7. 写审计：动作 `REVERT`，`metadata={"source_revision_no": n}`，产生新 revision；
8. 旧 ChangeLog 一行不改；旧 RelationshipHistory/Evidence 原始内容不受影响。

### 12.4 归档与恢复的区分

- 字段快照恢复 **不处理生命周期**：`deleted_at` 不属于可恢复字段；
- 实体恢复必须调用已有 `restore()`/`restore_event()`/`restore_evidence()`；
- Person 无 restore Service → 14.x 不为 Person 提供归档恢复入口；
- Workspace 无 restore Service；
- 恢复实体不恢复归档时解绑的媒体关联（PersonMedia/EvidenceMedia）。

### 12.5 恢复失败与边界

- 关联对象不存在/跨 Workspace/已归档/约束冲突 → 拒绝并说明原因；
- 恢复失败时数据库不变（Service 事务回滚）；
- 重复恢复同一值 → 无新 Revision；
- 恢复不能绕过 Evidence 不可变规则、RelationshipHistory 追加规则或任何 CHECK 约束。

---

## 13. Migration 设计（14.1 执行）

- 新 revision：`0009_change_log`，`down_revision = "0008_memory"`；
- 只 `create_table("change_log", ...)` + 索引，不改任何既有表；
- 列/约束按 §5.1；`recorded_at` 用 `DateTime(timezone=True)`（与 ORM `UTCDateTime`
  一致）；
- downgrade：按创建逆序 drop 索引 → drop table；不触碰其他表；
- 测试要求：
  - 空库 `upgrade head → downgrade 0008 → upgrade head` 往返；
  - 既有数据升级后不丢失（用带数据临时库升级到 0008 再升级 0009，行数不变）；
  - `PRAGMA foreign_key_check` 为空；
  - 唯一约束/索引存在性由 `inspect` 断言；
  - 更新 head 相关测试常量（§2.7 列表）；
- 不新增初始数据、不自动补录旧历史。

---

## 14. API / Service 接口草案（14.1–14.4）

### 14.1 Domain / 契约（新增 `app/domain/entities/change_log.py`）

```python
ENTITY_TYPES = ("WORKSPACE", "PERSON", "RELATIONSHIP", "RELATIONSHIP_HISTORY",
                "EVENT", "EVIDENCE", "MEDIA", "MEMORY")
ACTIONS = ("CREATE", "UPDATE", "ARCHIVE", "RESTORE", "IMPORT",
           "APPEND_HISTORY", "ATTACH_EVENT", "DETACH_EVENT", "REVERT")
PAYLOAD_VERSION = "1"

@dataclass(frozen=True)
class ChangeLogEntry:
    workspace_id: UUID; entity_type: str; entity_id: UUID; action: str
    operation_id: UUID; revision_no: int
    changed_fields: tuple[str, ...]
    before_snapshot: dict | None; after_snapshot: dict | None
    payload_version: str; recorded_at: datetime
    parent_entity_type: str | None = None; parent_entity_id: UUID | None = None
    metadata: dict | None = None
    id: UUID = field(default_factory=uuid4)
```

### 14.1 Serializer（新增 `app/services/change_serialization.py`）

```python
class ChangeSnapshotSerializer:
    def snapshot(self, entity_type, entity, *, aliases=(), images=(), participants=(), media=()) -> dict
    def changed_fields(self, entity_type, before, after) -> tuple[str, ...]
    def encode(self, value) -> str     # canonical JSON
    def decode(self, payload_version, text) -> dict   # 带版本校验/降级标记
```

### 14.1 Repository（新增接口 + SQL adapter）

```python
class ChangeLogRepository(Protocol):
    def add(self, entry: ChangeLogEntry) -> ChangeLogEntry
    def get(self, workspace_id, entry_id) -> ChangeLogEntry | None
    def list_by_entity(self, workspace_id, entity_type, entity_id, *,
                       limit=None, offset=0) -> list[ChangeLogEntry]
    def list_by_parent(self, workspace_id, parent_type, parent_id, *,
                       limit=None, offset=0) -> list[ChangeLogEntry]
    def list_by_operation(self, workspace_id, operation_id) -> list[ChangeLogEntry]
    def latest_revision(self, workspace_id, entity_type, entity_id) -> int | None
    def list_operations(self, workspace_id, *, limit=None, offset=0)  # 14.3 可选
```

无 update/delete 方法；ORM 守卫抛 `DomainError`。

### 14.2 Writer（新增 `app/services/change_capture.py`）

```python
@dataclass(frozen=True)
class ChangeOperationContext:
    operation_id: UUID; workspace_id: UUID; recorded_at: datetime

class ChangeLogWriter:
    def begin(self, workspace_id) -> ChangeOperationContext
    def record(self, context, *, entity_type, entity, action, before, after,
               parent=None, metadata=None) -> ChangeLogEntry
```

Writer 不拥有事务：必须在调用方 `write_operation` 的 Session 上工作
（构造时传入 `session` 或 `repositories`）。

### 14.3 查询/展示（新增 `app/services/change_history.py`）

```python
@dataclass(frozen=True)
class RevisionView: entry; field_changes: tuple[FieldChange, ...]; operation_rows: tuple[...]
def entity_history(workspace_id, entity_type, entity_id, *, limit, offset) -> tuple[RevisionView,...]
def operation(workspace_id, operation_id) -> tuple[ChangeLogEntry,...]
def revision(workspace_id, entity_type, entity_id, revision_no) -> RevisionView
def diff(workspace_id, entity_type, entity_id, from_revision, to_revision) -> tuple[FieldChange,...]
```

`FieldChange`：`field`、`label`、`before`、`after`（已格式化），UI 不解析原始 JSON。

### 14.4 Revert（新增 `app/services/change_revert.py`）

```python
class ChangeRevertService:
    def preview(self, workspace_id, entity_type, entity_id, revision_no) -> RevertPreview
    def revert(self, workspace_id, entity_type, entity_id, revision_no, *,
               confirmed: bool = False) -> RevertResult
```

`preview` 只读；`revert` 必须 `confirmed is True`，再调用既有 Service 更新，同一事务内由
Writer 记录 `REVERT`。

---

## 15. UI 设计草案（14.3）

- **入口**：在既有详情页/对话框加"历史"按钮，不重构主窗口：
  - Person：人物详情操作区（PeoplePage / Inspector）。
  - Event：`EventDetailDialog` / 事件详情。
  - Evidence：`EvidenceReader` / `EvidenceDetailDialog`。
  - Relationship：`RelationshipPanel` 的关系标题区。
  - Memory：`MemoryPanel` 选中项操作区。
  - Workspace：Settings 的 Data Management（只读；可选）。
- **RevisionHistoryDialog**：列表（时间倒序）+ 分页（按 revision_no 分段，每页 50）；
  每行：时间、动作标签、版本号、changed_fields 摘要、操作分组标记。
- **DiffView**：字段中文标签 + 旧值/新值；长文本折叠（点击展开）；日期/布尔/UUID/空值
  明确显示；数组显示增删摘要；未知字段/旧版本显示"无法解析此版本快照"而不崩溃。
- **操作组**：点击某个 operation 显示同组所有实体变更（Event + 相关 History 等），并标注
  "同一次操作"。
- **无历史数据**：明确提示"该记录在 Phase 14 之前创建，早期历史不可恢复；之后的修改会
  出现在这里"；禁止用 created_at/updated_at 伪造版本。
- **恢复按钮（14.4）**：只在可恢复实体与可恢复字段版本上出现；点击后先预览字段差异与
  二次确认；关联/生命周期不可恢复项只读。
- 全部读取经 Service；历史查询分页，避免一次性渲染大快照；长文本查看使用独立阅读器，
  不把大段内容挤入时间线。
- 符合 `app/ui/design/` 唯一来源；不新增颜色字面量/`setStyleSheet`；测试扫描保持通过。

---

## 16. 测试策略

| 层 | 14.1 | 14.2 | 14.3 | 14.4 |
|---|---|---|---|---|
| Domain | 枚举/字段契约/不可变 | 语义映射 | — | 白名单 |
| Serialization | 白名单/版本/规范化/非法输入 | 各实体快照 | 旧版本降级 | 可恢复编码 |
| Repository | add/查询/隔离/排序/唯一 | 同事务写入 | 分页/operation | 恢复后新版本 |
| Migration | upgrade/downgrade/存量升级/约束 | — | — | — |
| Service | — | 全量接入+原子提交/回滚 | 历史查询/差异 | 恢复全路径 |
| Integration | — | 多实体 operation_id/嵌套 | 操作组展示 | 恢复不改旧历史 |
| UI | — | — | 历史入口/差异/旧数据提示 | 恢复预览/确认 |
| Revert | — | — | — | 禁止项测试 |

重点测试文件（计划）：

- `tests/services/test_change_log_repository.py`、`tests/services/test_change_serialization.py`、
  `tests/migrations/test_change_log_migration.py`（14.1）
- `tests/services/test_change_capture_*.py`（按 Person/Event/Evidence/Relationship/
  Memory/Workspace/Media 拆分或单文件分组）与 `tests/services/test_change_atomicity.py`（14.2）
- `tests/services/test_change_history.py`、`tests/ui/test_revision_history_ui.py`（14.3）
- `tests/services/test_change_revert.py`、`tests/services/test_change_integrity.py`
  （14.4）

现有测试不得删除/弱化；5 项 Windows symlink skip 保持原义；head 断言测试按 §2.7 更新。

---

## 17. 风险与未决问题

1. **revision_no 并发**：单用户 + `BEGIN IMMEDIATE` 下写串行；极端并发让唯一约束失败并
   回滚。不引入乐观锁（15.x 前不扩大范围）。
2. **捕获覆盖完整性**：Service 方法多，漏接会形成"改过但无历史"；用审计矩阵 + 每方法
   测试 + 14.4 完整性检查（可选"写操作未捕获"无法从 DB 判断）兜底；接受残余风险。
3. **快照体积**：全量白名单快照（非 diff）会随修订次数线性增长；单机单用户可接受；
   Evidence 大文本与媒体二进制已排除。若未来膨胀，再评估压缩/差分（不改 payload_version
   兼容策略）。
4. **Media 两阶段**：文件 publish 与 DB 事务不可合并；审计只覆盖 DB 提交；文件补偿失败
   不写审计。已文档化。
5. **Person 带头像创建**：人物写入发生在 MediaService 事务，PersonService 需传 operation
   context；实现时需同时覆盖"无头像"路径。
6. **Alias/关联行硬删除**：不建独立实体；孤儿风险在完整性检查中显式保留。
7. **恢复关联**：14.x 不支持集合字段自动恢复，避免违反"关联对象存在/归档/同 Workspace/
   重复约束"等检查；用户可见但不可一键恢复。
8. **旧数据无历史**：UI 必须如实说明，不伪造；首次记录 revision_no=1 且 action 可能不是
   CREATE。
9. **导出/清理**：默认导出不含历史；不提供清理 API；如未来需要，先定隐私策略再实现。
10. **ORM 绕过**：append-only 只对业务接口成立；管理员 raw SQL 仍可改；不宣称触发器保护。
11. **UI 范围**：历史查看是新增对话框/按钮，避免大改主窗口；14.3 用现有页面挂载。
12. **Phase 13 不受影响**：不写 AnalysisRun Revision、不复制 Telemetry、不改评估。

---

## 18. 14.1–14.4 实施顺序与门禁

| 阶段 | 交付 | 门禁 |
|---|---|---|
| 14.1 | Migration `0009_change_log`、Domain、Serializer、Repository、不可变守卫、迁移/仓储测试 | 全部测试绿；旧库升级无损；无 UI/Service 接入 |
| 14.2 | `ChangeOperationContext` + `ChangeLogWriter`；按 §4 矩阵接入 Person/Event/Evidence/Relationship/Memory/Workspace/Media/Alias（聚合快照）；事务/回滚/无变化/分组测试 | 每个接入 Service 有捕获测试；原子性测试；不破坏既有测试 |
| 14.3 | `ChangeHistoryService`、分页、Diff、操作组；Person/Event/Evidence/Relationship/Memory 历史 UI；旧数据提示 | Workspace 隔离/归档实体可读；UI 不持 ORM；无恢复按钮 |
| 14.4 | 恢复白名单、预览+确认、经原 Service 更新、REVERT 审计、完整性检查、UI 恢复入口 | 禁止项测试；失败回滚；旧历史不变 |

每阶段结束：跑全量回归 → 更新 `docs/phase14-design.md`（如设计需修订）与
`docs/phase14-verification.md` → 报告实际 collected/passed/failed/skipped/exit code →
停止等待指令。**本设计未获用户审核前不得进入 14.1。**

---

## 19. 实施记录（Phase 14.2 — Transactional Change Capture）

- **统一入口**：`app/services/change_capture.py` 的 `ChangeOperationContext` +
  `ChangeLogWriter`。Writer 只接收调用方的 Session，不 commit、不新建 Session；
  `record()` 在同事务内校验实体/父引用（按类型查询对应表，允许软删除行）、拒绝跨
  Workspace、处理 create-like 快照规则、计算 `changed_fields` 与下一 `revision_no`、
  追加一行；`UPDATE`/`ATTACH_EVENT`/`DETACH_EVENT` 快照无变化时返回 `None`（不造 Revision）。
- **快照助手**：`app/services/change_snapshots.py` 在调用方事务内加载聚合集合
  （aliases/images/participants/media）并交给 Serializer。
- **组合根唯一装配**：`create_application_services()` 创建一个 Writer 并注入
  Workspace/Person/Media/Relationship/Event/Evidence/Memory 服务；Service 构造函数的
  `changes=None` 仅用于独立单测/脚本，不改变既有测试行为。`ensure_default()` 是系统
  引导元数据，不做审计。
- **聚合边界**：Alias、PersonMedia、EventParticipant、EvidenceMedia 的变化记录为
  父实体（PERSON/EVENT/EVIDENCE）的快照集合差异；RelationshipHistory 为独立实体，
  `parent=(RELATIONSHIP, id)`；APPEND_HISTORY 以 `before=null` 记录。
- **两阶段 Media**：人物带头像创建时，PersonService 生成 operation context，经
  `MediaService.import_file(..., operation=ctx)` 传入；实际写库的 `_persist` 事务内记录
  PERSON CREATE + MEDIA IMPORT（同一 operation），不产生重复 PERSON 行。Media 新导入记录
  IMPORT；重新导入已归档媒体记录 RESTORE（同一 Media ID，不产生第二个 IMPORT）；已存在且未
  归档的重复导入不产生媒体审计行；人物/证据的附件变化进入父实体快照。
- **失败原子性**：快照生成或 ChangeLog 写入失败直接抛出 → 业务事务整体回滚；
  业务校验失败不写审计；只读操作不写。
- **未变**：无 schema/migration 变化（仍 0009）；未实现历史 UI（14.3）与恢复（14.4）；
  未回填旧历史；AnalysisRun 仍不进入普通 Revision。

---

## 20. 实施记录（Phase 14.3 — Revision History & Diff Viewer）

- **读取服务**：`app/services/change_history.py` 的 `ChangeHistoryService` 只读：
  `entity_history`（新版在前、每页 50、`has_more` 分段）、`parent_history`
  （RELATIONSHIP_HISTORY 按父关系分组）、`revision`、`operation`（同操作全部行）、
  `diff`/`adjacent_diff`（任意两版本/相邻版本字段差异）。所有查询先校验 Workspace；
  已归档实体仍可读（历史查询不依赖实体当前可见性）。
- **人类可读格式**：实体/动作/字段均有中文标签（`ENTITY_LABELS`/`ACTION_LABELS`/
  `FIELD_LABELS`）；值统一格式化（None、布尔、date/datetime 本地时区、UUID、集合
  摘要、长文本），UI 不显示原始 JSON 或内部字段名；集合差异（aliases/images/
  participants/media）按稳定顺序摘要；长度超限只截断显示，完整文本按需打开。
- **旧数据与不可解析快照**：`ChangeLogEntry.snapshot_error` + Repository 容错解码让
  "未知 payload_version / 损坏 JSON" 的行仍可作为审计元数据读取；该行标记为
  "快照不可用"，禁止 diff/revert，UI 显示明确提示而不是崩溃或猜测。`legacy_hint`
  对"最早可见版本不是创建"的记录提示 Phase 14 之前历史不可恢复；无历史记录时明确
  说明"不会补录"，绝不根据 created_at/updated_at 伪造。
- **UI**：新增 `app/ui/revision_history.py`（只读 `RevisionHistoryDialog` +
  `OperationDialog` + `LongTextDialog`）。入口：People（人物操作区"历史"）、Event 详情
  对话框、Evidence 面板、Relationship 面板、Memory 面板；关系额外显示"关系状态历史
  （追加记录）"分区。列表新版在前、分页加载；差异面板字段级"旧 → 新"；操作组按钮展示
  同一次操作的全部变更。**没有恢复/撤销按钮**（14.4 才实现）。
- **组合根**：`ApplicationServices.change_history` 新增只读服务；UI 经 Service，不接触
  ORM/JSON。
- **未变**：无 schema/migration；未实现字段恢复（14.4）。

---

## 21. 实施记录（Phase 14.4 — Controlled Revert & Integrity）

- **恢复白名单**（`app/services/change_revert.py`）：PERSON 8 字段、EVENT 8 字段、
  EVIDENCE 仅 title/annotation、MEMORY content/kind/person_id/valid_from/valid_until、
  WORKSPACE name/description。实体身份/Workspace/系统时间戳/Relationship 身份与 History/
  Evidence 原始内容/Memory 来源字段/Media 二进制与路径/所有子集合均不可恢复；不支持的类型
  返回明确"不支持"原因。
- **恢复流程**：`preview()` 读取目标 Revision 快照 → 校验实体存在且未归档/未删除、字段
  白名单、payload_version、目标值合法性、当前值差异、Workspace 名称冲突；`revert()` 必须
  `confirmed=True`，无差异时返回 applied=False 且不产生新版本。执行时创建
  `ChangeOperationContext(action_override="REVERT", metadata={"source_revision_no": n})`，
  调用**原 Service 的 update 方法**（新增内部 `operation=` 参数透传上下文），业务校验、
  领域规范化、事务与审计写入全部复用；ChangeLog 产生一条 REVERT Revision，旧行不改不删。
  REVERT 可通过再次恢复反向操作，不是不可逆改写。
- **关联不自动恢复**：alias、参与者、附件、媒体引用、关系端点等仍由用户显式操作；生命周期
  恢复只经既有 restore Service（Person/Workspace 当前没有恢复，故其归档记录不能字段恢复）。
- **失败语义**：归档/已删除、快照不可用、白名单外实体、名称冲突、确认缺失均在写库前拒绝；
  快照生成/审计写入失败由 Service 事务整体回滚，不产生新 Revision。
- **UI**：历史对话框新增"恢复此版本字段…"按钮，仅当预览 `can_revert and has_changes` 时可用；
  点击打开 `RevertPreviewDialog` 展示"当前 → 目标"字段清单，确认后才执行；结果以新版本出现在
  时间线顶部。无恢复能力的实体/版本按钮禁用并给出原因。
- **完整性审计**（`DataIntegrityService`）：新增只读 ChangeLog 检查（不修复）：孤儿实体、
  Workspace 不一致、未知实体/动作、版本号缺口/重复、快照或 changed_fields 不可解析、
  父引用无效、同一 operation 跨 Workspace。表行数作为独立 `table_change_log` 计数，
  不改变 fact_tables 语义；备份/导出行为不变。
- **未变**：无 schema/migration（仍 0009）；不重写历史、不自动修复任何数据；不改变备份/导出
  内容。


---

## 22. 发布后静态审计修复（2026-11-22）

在 Phase 14 完成后的静态审计中修复了以下问题，均未改变数据库 Schema：

1. **本地文件误跟踪**：`AGENTS.md` 与 `项目.txt` 从 Git 索引移除（保留本地文件与 `.gitignore`
   规则）。对这两个文件的当前内容及全部历史版本做了模式级密钥扫描，未发现 API Key/Token/
   密码/本机路径；此项清理不重写 Git 历史，历史提交中仍保留这两个文件，若需彻底移除必须
   单独评估并确认历史改写。
2. **历史分页**：普通实体与 Relationship 父历史均改为 SQL 层真正的“最新优先”分页；
   UI 初次加载后立即推进 offset，连续加载不再重复第一页（新增 >50 条分页测试）。
3. **Media 重新导入语义**：首次导入 IMPORT、重新导入已归档媒体 RESTORE（同一 Media ID）、
   已存在且未归档的重复导入不产生媒体审计行；集中审计矩阵与测试同步更新。
4. **完整性审计快照语义**：除 JSON 可解析外，增加白名单字段完整性、Domain 动作/前后快照
   约束、`changed_fields` 与快照差异一致性检查（`CHANGE_LOG_SNAPSHOT_INVALID`、
   `CHANGE_LOG_CHANGED_FIELDS_INVALID`）。
5. **文档收尾**：设计文档标注最终状态与历史时点；删除验收文档中残留的“待实施”重复小节。
