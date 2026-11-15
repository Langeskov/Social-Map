# 架构与阶段边界

## 当前实现

Phase 1 Qt 骨架、Phase 2 数据库、Phase 3 Workspace／Person／Media、Phase 4 Relationship／RelationshipHistory、Phase 5 Event／EventParticipant、Phase 6 Evidence／EvidenceMedia、Phase 7 投影层（Unified Search／Timeline／Relationship Graph／Dashboard）、Phase 8 分析层（LLM Provider／ContextBuilder／AnalysisRun／AnalysisSource）、Phase 9 的 Analysis Review 与用户确认 Memory，以及 Phase 10 的稳定化（只读完整性审计／一致性备份与恢复校验／导出／数据管理界面）。事实层仍是十一张正式业务表。旧工作区 Phase 1 指引由用户明确授权的后续阶段请求覆盖。

```text
UI（Domain Entity / DTO / Service）
  ↓
Application / Service（业务规则、Workspace、原子事务与媒体补偿）
  ↓
Domain / Repository Interface（stdlib）
  ↓
SQLAlchemy Repository Adapter（Domain ↔ ORM）
  ↓
SQLite / Local MediaStore
```

Domain 不依赖 Qt／SQLAlchemy／SQLite／Alembic／Storage；UUID、UTC-aware datetime、date 使用普通 Python 类型。ORM 不暴露 UI；仓储只持久化／flush，Service 每操作独立 Session，读 BEGIN、写 BEGIN IMMEDIATE。关系与事件各有独立 Repository 装配，不改变 Phase 3 媒体 bundle。

## ER 与不变式

```text
Workspace 1 ── N Person
Workspace 1 ── N Media
Workspace 1 ── N Event
Person 1 ── N PersonAlias
Person 1 ── N PersonMedia N ── 1 Media
Person A ── Relationship ── Person B
Relationship 1 ── N RelationshipHistory
Event 1 ── N EventParticipant N ── 1 Person
Event 1 ── N RelationshipHistory （event_id 可空）
Event 1 ── N Evidence N ── N Media （经 EvidenceMedia 绑定，UNIQUE(evidence_id, media_id, role)）
```

Workspace 是数据边界，不是账号／权限系统。Alias、PersonMedia、Relationship 端点、History 父关系、Participant 的两端、History 的 Event 引用均由 Workspace 复合 FK 与 Service 双重约束，直接 SQL 跨 Workspace 关联也被数据库拒绝。

Person 姓名／Alias 不唯一。Media 独立、workspace/hash 唯一、原文件不可变；Avatar 仅由 PersonMedia.role 表达，partial unique 保证单头像。软归档、引用解绑和文件补偿沿用 Phase 3。

Relationship 身份创建后不可改；无向端点按 UUID 规范化，自然键覆盖 deleted 记录。ENDED 不等于 soft delete；关系 current state 从 History 的 `(effective_at, created_at, id)` 派生，未来状态不提前生效。

History 状态事实 append-only；用户明确确认唯一例外是经 EventService 原地修改 event_id 关联元数据。单行旧／新值授权与 ORM changed-field 检查限制该路径，不提供通用 History update／delete，关联不改变状态／备注／时间／关系 updated_at。管理员 raw SQL／Core bulk 不属于 ORM guard 的数据库触发器级保护范围。

Event 仅 FACT／OBSERVATION，不接受 INFERENCE。日期事件和时间事件严格二选一，aware datetime 转 UTC，DATE 不转换时区；SQL 显式按发生时间排序。Event 与参与人创建／编辑原子提交。创建、编辑、删除、恢复 Event 不自动操作关系或 History。

Evidence 是支持 Event 的原始或近原始材料，只挂在 Event 上；`content_text`（原始内容）与 `annotation`（用户注释）分离，原始字段创建后不可改，改错走"软删除 + 新建"。Evidence 只经 `EvidenceMedia` 复用 Phase 3 Media，不复制 sha256，解绑不删文件；Media 软删除前同时检查人物引用与证据引用。Evidence 创建与附件绑定同一事务，附件操作携带快照，不一致时冲突拒绝。不自动把 Evidence 变成 Event、关系或分析。

Person 归档保留 Event／Participant；禁止新增 archived participant，已有行保留并标记 Archived。Event soft delete 默认隐藏但保留参与人与历史引用，恢复不重写历史。

## 迁移与 UI 装配

0001 历史空 schema；0002 五表；0003 七表；0004 九表并扩展 History.event_id；0005 十一表并新增证据两表。旧 0001–0004 不修改。SQLite batch 保留旧 History 数据与约束，保持 foreign_keys=ON；已有旧库仍须用户备份并显式升级。Default Workspace 为系统元数据，不是迁移 seed。

ApplicationServices 尾部可选 EventService 保持既有调用兼容。Events 升级为普通 Qt Widget；Person Detail 增加 Timeline，打开可操作事件详情。Events 与 People 明确独立选择 Workspace，页面激活和事件操作刷新；Timeline 详情锁定对应 Workspace。UI 不拿 ORM 或 Session，不制作动态图和 AI panel。

详见 [投影层](<projection.md>)、[证据](<evidence.md>)、[事件](<events.md>)、[关系](<relationships.md>)、[数据库](<database.md>)、[媒体](<media.md>)。

## 事实层 / 投影层 / 分析层

```text
事实层：Person, PersonAlias, Relationship, RelationshipHistory,
        Event, EventParticipant, Evidence, EvidenceMedia, Media
投影层：Search(FTS5), Timeline, Graph, Dashboard   ← 全部可重算，不是 Source of Truth
分析层：AnalysisRun, AnalysisSource               ← 只读事实，输出永不回写事实
记忆层：Memory                                    ← 只有用户明确确认才会存在
```

投影只能从事实层重新计算：Timeline／Graph／Dashboard 不落表，Search 只落一个可随时丢弃重建的索引；任何投影被清空都不会丢事实，`rebuild_index()` 或下一次查询即可恢复。事实写入与搜索投影更新在同一个 Service 事务内完成。

分析层同样不写事实：ContextBuilder 只读事实层，把"真正发给模型的文本"存进 `AnalysisRun.context_snapshot`，来源对象记入四张 `analysis_*_source` 关联表。**所有模型调用都必须由用户点击触发**；Provider 失败只产生 FAILED 运行，绝不改动事实。

记忆层是"用户确认过的长期语义"：Analysis Review → 用户编辑文本 → 明确确认才创建（也可手工创建）。它既不修改事实，也不会被自动喂给分析——`include_memories` 默认关闭，开启后上下文快照会记录实际使用了哪些记忆。

## 数据管理（Phase 10）

只读审计（`DataIntegrityService`）、一致性备份与恢复校验（`BackupService` / `BackupVerificationService`）、导出（`WorkspaceExportService`）全部**只读源库与源媒体**，并且只由用户在 Settings 里显式触发：启动不 hash 全媒体、Dashboard 不审计、不自动备份、不自动上传。审计**只报告**（孤儿文件不删除，留给未来的 GC），备份用 SQLite 在线备份 API 取得一致快照而不是复制 WAL 文件，导出的 JSON 明确标注"不是备份"且不含任何凭据。

Phase 10 的结构精简：UI 操作/错误边界合并为 [operations.py](<../app/ui/operations.py>) 唯一一份（原 21 份副本），行↔Domain 映射合并为 [mapping.py](<../app/storage/mapping.py>)（原 5 份，并修正了"通用辅助挂在关系模块"的错误归属）。各聚合自己的 workspace 作用域查询**刻意保留**——它们的可见性语义不同，强行参数化会造出假抽象。

## 未实现与后续决策

- Sync／ChangeLog／Revision／MemoryRevision；这些表与功能不存在。
- Vision／OCR、RAG／向量库、embedding、记忆相似度搜索、自动记忆整合。
- 聊天导入、自动事件提取、自动摘要、自动关系判断、自动 Memory。
- 由分析结果修改事实的流程（Analysis → 用户复核 → 显式 Service 操作）；本阶段不实现。
- 冲突解决、多用户、云、Docker、Web API；仍是本地优先的 Qt 桌面应用。
- PostgreSQL 搜索（未来用 tsvector／tsquery 替换 Search Adapter）；当前只做 DDL compile。
- 孤儿媒体文件与数据库的垃圾回收；本阶段只报告不删除。
- 后台大列表／大图操作、大媒体量的进度回调、wheel／exe 分发。

完成 Phase 10 后停止，不自动进入后续阶段。
