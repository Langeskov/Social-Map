# 架构与阶段边界

## 当前实现

Phase 1 Qt 骨架、Phase 2 数据库、Phase 3 Workspace／Person／Media、Phase 4 Relationship／RelationshipHistory、Phase 5 Event／EventParticipant、Phase 6 Evidence／EvidenceMedia、Phase 7 投影层（Unified Search／Timeline／Relationship Graph／Dashboard）、Phase 8 分析层（LLM Provider／ContextBuilder／AnalysisRun／AnalysisSource）、Phase 9 的 Analysis Review 与用户确认 Memory、Phase 10 的稳定化（只读完整性审计／一致性备份与恢复校验／导出／数据管理界面）、Phase 11 的 UI/UX 整合、Phase 12 的人物调查工作台、Phase 13 的分析质量与评估（结构化结果契约、来源曝光与引用验证、运行遥测、模型评估），以及 Phase 14.0 的 ChangeLog 设计与 Phase 14.1 的 ChangeLog 持久化基础（append-only 操作审计表）。事实层仍是十一张正式业务表；ChangeLog 不属于事实层，也不是状态 store。旧工作区 Phase 1 指引由用户明确授权的后续阶段请求覆盖。

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
变更审计层：ChangeLog                             ← 追加式操作审计，不是状态 store
```

投影只能从事实层重新计算：Timeline／Graph／Dashboard 不落表，Search 只落一个可随时丢弃重建的索引；任何投影被清空都不会丢事实，`rebuild_index()` 或下一次查询即可恢复。事实写入与搜索投影更新在同一个 Service 事务内完成。

分析层同样不写事实：ContextBuilder 只读事实层，把"真正发给模型的文本"存进 `AnalysisRun.context_snapshot`，来源对象记入四张 `analysis_*_source` 关联表。**所有模型调用都必须由用户点击触发**；Provider 失败只产生 FAILED 运行，绝不改动事实。

记忆层是"用户确认过的长期语义"：Analysis Review → 用户编辑文本 → 明确确认才创建（也可手工创建）。它既不修改事实，也不会被自动喂给分析——`include_memories` 默认关闭，开启后上下文快照会记录实际使用了哪些记忆。

## 变更审计层（Phase 14）

Phase 14 采用**单表 append-only ChangeLog + 逻辑 Revision**：不建独立 Revision 表，不做 Event Sourcing，不复制整套数据库。设计契约见 [Phase 14 设计](<phase14-design.md>)。

- **一张表**：`change_log` 记录一次真实业务写操作（`entity_type/entity_id/action/operation_id/revision_no` + 白名单 canonical JSON 前后快照 + `recorded_at`）。Revision 是按实体读取的逻辑视图。
- **归属**：只有 `workspace_id` 是真实外键；多态实体引用由 Writer 校验与完整性审计兜底（SQLite 无法用一个外键指向多张表），如实保留该限制。
- **不可变性**：Repository 只有 append/read；ORM 守卫拒绝 update/delete；管理员 raw SQL 仍可绕过，不宣称触发器级保护。
- **快照**：白名单字段（`app/services/change_serialization.py`），排除系统时间、Evidence 原始内容、媒体二进制与内部路径；`payload_version` 管理格式演进。
- **14.2 状态**：业务写操作已接入（`create_application_services()` 注入统一 Writer，与业务写入同事务；无变化不记；失败整体回滚）；**没有历史 UI（14.3）、没有恢复（14.4）**。当前应用会为受审计写操作产生 ChangeLog 行，但只能通过后续历史 UI 或 SQL 查看。
- **捕获方式**：Service 层显式 `ChangeLogWriter`（不是 ORM Hook），由最外层业务方法创建 `ChangeOperationContext` 并透传嵌套调用；Alias、PersonMedia、EventParticipant、EvidenceMedia 的子集合变化记录在父实体快照中，RelationshipHistory 独立成实体并以 parent 关联 Relationship。
- **历史边界**：Phase 14 之前创建的数据没有历史记录，不根据 `created_at/updated_at` 伪造；备份天然包含该表，默认导出不含。

## 数据管理（Phase 10）

只读审计（`DataIntegrityService`）、一致性备份与恢复校验（`BackupService` / `BackupVerificationService`）、导出（`WorkspaceExportService`）全部**只读源库与源媒体**，并且只由用户在 Settings 里显式触发：启动不 hash 全媒体、Dashboard 不审计、不自动备份、不自动上传。审计**只报告**（孤儿文件不删除，留给未来的 GC），备份用 SQLite 在线备份 API 取得一致快照而不是复制 WAL 文件，导出的 JSON 明确标注"不是备份"且不含任何凭据。

Phase 10 的结构精简：UI 操作/错误边界合并为 [operations.py](<../app/ui/operations.py>) 唯一一份（原 21 份副本），行↔Domain 映射合并为 [mapping.py](<../app/storage/mapping.py>)（原 5 份，并修正了"通用辅助挂在关系模块"的错误归属）。各聚合自己的 workspace 作用域查询**刻意保留**——它们的可见性语义不同，强行参数化会造出假抽象。

## UI 层（Phase 11）

Phase 11 只改 UI 层与 UI 共享基础设施，**没有**新增 migration、没有改业务 schema、没有改 Domain 语义。

```text
MainWindow
├── TopBar      品牌 / 全局 Workspace 选择器 / 全局搜索（Ctrl+F）/ 状态
├── Sidebar     稳定导航（Dashboard People Events Graph Analysis Settings）+ FACT/USER/DERIVED 图例
├── ContextBar  只读面包屑 Workspace › Page › Object（没有 Back / Forward 历史）
└── QStackedWidget × 6 页面
```

- **Design System 唯一来源**：[design/](<../app/ui/design/>)（tokens / fonts / qss / icons / widgets / theme）。
  应用启动时 `apply_design()` 设置 Fusion 风格、QPalette、字体链与唯一 QSS，并关闭 Qt UI Effects；
  页面不得自带颜色或样式表（有测试扫描强制）。
- **单一 Workspace 上下文**：[context.py](<../app/ui/context.py>) 的 `WorkspaceContext` 是唯一真相；
  页面内旧的 `workspace_combo` 保留为**代理**（只回读上下文），不再持有独立状态。
  切换走固定协议：`about_to_change` → 各页 `reset_context()`（关闭非模态窗口、清选中/详情/搜索）
  → `changed` → 各页重载。Analysis 运行期间全局选择器锁定。
- **层标识与认知标识**：Entity / Link / Occurrence / Proof / Inference 五层各有固定图标与低饱和色；
  FACT / USER / DERIVED 与之正交（实线只读 / 用户可编辑 / 虚线 + Sources）。
- **状态矩阵**：Normal / Empty / Loading / Error / Conflict / Archived / Deleted / Unavailable
  由 `StateStack`、`StateNotice`、`ConflictBanner`、`StatusPill` 统一表达。
- **冲突**：`run_operation` 仍把 `ConflictError` 的 Service 原文交给用户，并额外交给页面 banner 持久显示。
  当前 stale snapshot 保护**只覆盖已有 expected-state guard 的操作**（Evidence 附件、Event↔RelationshipHistory）；
  Person／Event／Memory 的普通 metadata 更新仍是 last-write-wins。Phase 11 不引入 `expected_updated_at`，
  不重构 `ConflictError` taxonomy。
- **无动画**：本阶段不做任何过渡、粒子、节点呼吸或动态连线；只有 Analysis 的耗时文字计数在更新。
- **唯一非 UI 扩展**：`DashboardService.recent_analysis()`（只读，保持 `snapshot()` 的既有查询数上限），
  用于 Dashboard 的 Recent Analysis；Dashboard 页不直接调用 `AnalysisService`。

## UI 层（Phase 12）

Phase 12 把既有能力组织成人物调查工作台（Person Investigation Console），仍然**无 schema 变化**：

- **People 三区工作台**：人物索引 ｜ 人物档案 + Ego Network ｜ Inspector（关系管理／Activity／
  Evidence／Memory／Analysis 五 Tab）。Inspector 为嵌套 `QSplitter` 布局：宽屏右侧栏、窄屏底部抽屉
  （显式切换 + 宽度自动切换）。
- **绘图层唯一实现**：[graph_canvas.py](<../app/ui/graph_canvas.py>) 承载 `PersonNode`／
  `RelationshipEdge`／`GraphView`／几何与状态画笔；全局 Graph 页与 Ego Network 共用（显式点击
  handler 接缝）。Ego Network 只读 `RelationshipService.list_by_person`（一层直接关系），不推断、
  不落库、不持久化坐标。
- **Inspector 联动**：Activity 行选中事件 → 内联事件详情 → 该事件证据 → 所属 Event／Analysis Sources
  跳转，全部经 `navigate_requested → MainWindow.open_target`；无历史导航栈（ContextBar 仍只读）。
- **Evidence 阅读器**：[evidence_dialog.py](<../app/ui/evidence_dialog.py>) 的 `EvidenceReader`
  同时服务详情对话框与 Inspector：FACT 原文只读、USER 注释可编辑，所属 Event 常显可跳。
- **Workspace Timeline**：Events 页第二 Tab（共享 `TimelinePanel` 工作区模式），是完整 Workspace
  时间线的唯一可见入口；Dashboard 只保留统计与最近列表。
- **全局搜索唯一入口**：TopBar（Ctrl+F）；页面内不再有隐藏 SearchPanel 副本，People 页可见搜索框
  只是本地列表过滤器。
- **状态优先级**：TopBar 状态是分层来源（`lock` 级的「分析运行中」不被 `page` 级导航文案覆盖，
  解锁后恢复）；Analysis 锁定锚定 `QThread.finished`，取消与关闭对话框都不提前解锁。
- **归档人物只读契约**：`PersonService.detail(include_deleted=True)` 只读身份与存留别称；
  `soft_delete` 在归档时解绑的图片引用不恢复、不伪造（UI 如实标注），所有写入口拒绝归档对象。

## 未实现与后续决策

- Sync／MemoryRevision；这些表与功能不存在。ChangeLog 已持久化并接入业务写操作（Phase 14.2）；Revision 历史 UI（14.3）与受控恢复（14.4）尚未实现。
- Vision／OCR、RAG／向量库、embedding、记忆相似度搜索、自动记忆整合。
- 聊天导入、自动事件提取、自动摘要、自动关系判断、自动 Memory。
- 由分析结果修改事实的流程（Analysis → 用户复核 → 显式 Service 操作）；本阶段不实现。
- 冲突解决、多用户、云、Docker、Web API；仍是本地优先的 Qt 桌面应用。
- PostgreSQL 搜索（未来用 tsvector／tsquery 替换 Search Adapter）；当前只做 DDL compile。
- 孤儿媒体文件与数据库的垃圾回收；本阶段只报告不删除。
- 后台大列表／大图操作、大媒体量的进度回调、wheel／exe 分发。

完成 Phase 10 后停止，不自动进入后续阶段。
