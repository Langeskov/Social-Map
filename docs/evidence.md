# Evidence / EvidenceMedia（Phase 6）

## 语义与边界

Event 回答"发生了什么"，Evidence 回答"凭什么记录这个 Event"。Evidence 是支持某个 Event 的原始或近原始材料，属于事实层：不是 AI 分析、人格判断、关系判断、摘要或自动推断。

本阶段只有 Evidence、EvidenceMedia 两张新表；不实现 Analysis、AnalysisSource、Memory、LLM、Graph、Sync、ChangeLog、Revision、OCR，也没有聊天导入、网页抓取、自动事件提取或自动关系判断。不建立 Source 字典表。

## ER

```text
Workspace 1 ── N Event
Workspace 1 ── N Evidence
Workspace 1 ── N Media

Event    1 ── N Evidence                       复合 FK 含 workspace_id
Evidence 1 ── N EvidenceMedia N ── 1 Media     UNIQUE(evidence_id, media_id, role)

Evidence → Event → RelationshipHistory         通往关系的唯一路径
```

第一版**不建立** Person→Evidence、Relationship→Evidence。未来的 AnalysisSource 可以直接引用 Evidence。

## Evidence 字段

`id`、`workspace_id`、`event_id`（UUID）；`kind`、`source_type`（字符串 + CHECK）；`title`（trim 后 1–200）；`content_text`、`annotation`（可空文本）；`source_label`（可空，≤300，空白归一为 NULL）；`source_reference`（可空文本）；`captured_at` 可空 UTC-aware；`created_at`／`updated_at`／可空 `deleted_at`。

`kind ∈ {TEXT, IMAGE, FILE, LINK, MANUAL, OTHER}`；`source_type ∈ {CHAT, EMAIL, DOCUMENT, IMAGE, FILE, WEB, MANUAL, OTHER}`。Domain 与数据库双重校验；新增类型需要迁移。

`source_type`／`source_label`／`source_reference` 只是来源描述，不是外部系统实体。不实现微信／Discord／Email 导入或爬虫。

## 原始内容与注释分离

`content_text` 是原始证据内容，**按原样保存**（不 trim、不改写）；`annotation` 是用户对证据的说明。二者不合并为 `description`，UI 也分开显示。例如 `content_text="原始聊天文字……"`、`annotation="这是活动前一天的聊天记录。"`。

## 创建后不可变

不可修改：`content_text`、`kind`、`source_type`、`source_reference`、`captured_at`、`event_id`、`workspace_id`、`id`、`created_at`，以及 Media 文件内容本身。可修改：`title`、`annotation`。

保护是分层的：Repository **只提供** `update_metadata`（不提供 `update_content`／`update_source`／`update_captured_at`，也没有通用 `save` 覆盖路径），ORM `before_update` 再次拒绝上述原始字段变化，`before_delete` 拒绝物理删除。管理员 raw SQL／Core bulk 不受 ORM guard 约束，这不是数据库权限系统。

录错原始证据时不要覆盖：**原 Evidence 软删除 + 新建 Evidence**，原记录保持可追溯。

## EvidenceMedia 与 Media 策略

`id`、`workspace_id`、`evidence_id`、`media_id`（UUID）；`role` 可空字符串（≤100，空白归一为 NULL）；`created_at` UTC-aware。同一 Media 可被多个 Evidence 复用，一个 Evidence 可有多个 Media。

- 完全复用 Phase 3 Media／MediaStore，**不复制 sha256**，Evidence 文本也不额外建 hash 字段。
- EvidenceMedia 只是绑定行：解绑或删除 Evidence 都不删除文件。
- **Phase 7 修正的引用规则**：只要 EvidenceMedia 行仍存在，Media 就算仍有引用——**Evidence 软删除不释放 Media**（软删除刻意保留绑定行，所以必须显式解绑才会释放）。有绑定的 Media 不能软删除。**不实现 Media GC、物理删除、备份、恢复。**
- 新文件导入仍走 `MediaService.import_file` 的既有保守补偿，不自行实现补偿。

### role 为 NULL 的唯一性

SQL 中 NULL 互不相等，因此 `UNIQUE(evidence_id, media_id, role)` 无法阻止 `role IS NULL` 的重复行。除该约束外，另加一个 `WHERE role IS NULL` 的 partial unique index，让数据库在两种情况下都真正拒绝重复绑定。

## Workspace 隔离

`(workspace_id, event_id) → Event(workspace_id, id)`、`(workspace_id, evidence_id) → Evidence(workspace_id, id)`、`(workspace_id, media_id) → Media(workspace_id, id)`。Service 再做二次校验。Workspace A 的 Event 不能挂 Workspace B 的 Evidence，Evidence 也不能引用 B 的 Media——数据库直接拒绝。

## 类型规则（Service 校验）

| kind | 规则 |
| --- | --- |
| TEXT | 必须提供非空 `content_text` |
| IMAGE | 创建时至少一个 EvidenceMedia |
| FILE | 创建时至少一个 EvidenceMedia |
| LINK | 必须提供非空 `source_reference` |
| MANUAL | 允许 `content_text`，不强制 |
| OTHER | 必须有 `title`（其余不强制） |

IMAGE／FILE 还必须**始终**保留至少一个附件：移除最后一个附件会被拒绝（`ConflictError`），提示"附件确实有误时软删除该证据后重建"。复杂业务条件留在 Service，数据库只做基础 CHECK。

## 创建原子性

`Evidence` + 全部 `EvidenceMedia` 在**同一个写事务**提交：任意 Media 不存在、跨 Workspace、Event 已删除或提交失败，整个写入回滚，不会出现"Evidence 已存在但附件写了一半"。

## 时间

`captured_at` 是证据被获取／截取／记录的实际时间，可为空；`created_at` 是证据进入本系统的时间。两者不混淆。所有 datetime 必须 timezone-aware 并转为 UTC，naive datetime 拒绝。

## 删除 / 恢复

- **Event 软删除**：不删除 Evidence、不改 Evidence 内容；UI 默认隐藏该 Event 的 Evidence，`include_deleted` 才显示；恢复 Event 后 Evidence 自动重新可见。
- **Evidence 软删除**：不删 Media、不改 Event、不改 RelationshipHistory；绑定行保留。恢复只清 `deleted_at`，原始字段、附件、Event 与关系状态都不变。
- 已删除 Event 下不允许**新建** Evidence；既有 Evidence 仍可读、可改 metadata、可增删附件（与 Phase 5 的历史关联同样是纯链接操作）。

## 陈旧引用保护

附件操作携带调用方看到的附件快照（`EvidenceService.attachment_ids(detail)`），Service 在写事务内比对当前集合；不一致时抛 `ConflictError` 且不修改数据库，UI 刷新后重试。并发添加同一 Media 时恰好一个成功。快照逻辑只在 Service，不在 Repository。

## UI

Event Detail 增加 Evidence 区：列表显示 title、kind、来源、`captured_at`、附件数量、annotation 摘要；支持新建 Evidence、编辑 metadata（只有 title／annotation）、查看原始内容与附件、添加 Media（选择已有或导入新文件后附加）、移除 Media 引用、删除／恢复 Evidence。

Evidence Detail 明确区分**「原始证据」（只读）**与**「用户注释」（可编辑）**。不提供任何修改 `content_text`／`kind`／`source_type`／`source_reference`／`captured_at` 的入口。

## 搜索

本阶段不做 FTS5，只提供 `list_by_event()`、`list_by_workspace()` 与 Event Detail 下的基础过滤（kind）。Person／Event／Evidence 的统一全文搜索留到后续阶段。

## 搜索与投影

Evidence 的 `title`、`content_text`、`annotation`、`source_label`、`source_reference` 会进入 Phase 7 的搜索投影；证据本身**不是**独立时间线条目，而是 Event Detail 的子项。详见 [投影层设计](<projection.md>)。

## Migration 0005

新增 [0005 migration](<../alembic/versions/0005_evidence.py>)，0001–0004 不改：只创建 `evidence`、`evidence_media` 两表及命名约束与索引（无 batch，因为不改动既有表）。业务表数 9 → 11。

验证 `0001 → … → 0005`、seeded `0004 → 0005 → 0004 → 0005`、`PRAGMA foreign_key_check` 无错误，以及降级只删这两张表且原九表数据完整。降级会丢弃 Phase 6 数据，不是备份恢复。SQLite 真实运行；PostgreSQL 只做 DDL compile 验证，不声称部署通过。
