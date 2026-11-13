# Phase 5 — Events 与参与人

## 语义与边界

Event 是用户记录的现实事件或明确观察。`record_kind` 仅有 `FACT` 和 `OBSERVATION`，拒绝 `INFERENCE`。共同参与不代表关系活跃，不自动创建、结束或修改 Relationship，也不自动追加 History。

本阶段只有 Event、EventParticipant 与 History 的可空关联列，不实现 Evidence、Analysis、Memory、LLM、Graph、Sync、ChangeLog、Revision；不创建这些表，不生成示例人物或事件。

## ER

```text
Workspace 1 ── N Event
Workspace 1 ── N Person
Event 1 ── N EventParticipant N ── 1 Person
             UNIQUE(event_id, person_id)
Event 1 ── N RelationshipHistory N ── 1 Relationship
             event_id nullable
```

参与人的两个复合 FK，以及 History → Event 的复合 FK，均包含 `workspace_id`；Service 同时验证归属。数据库约束不是仅靠 UI 过滤。

## 时间与字段

Event：UUID `id / workspace_id`；trim 后 1–200 字符的 `title / event_type`（类型大小写敏感）；`record_kind`；可空 `location / description`；UTC-aware `created_at / updated_at / deleted_at`（后者可空）。

时间严格二选一：

- 日期事件：`occurred_on` 为 DATE，`started_at / ended_at` 都为空。
- 时间事件：`occurred_on` 为空，`started_at` 必填，`ended_at` 可空且不得早于开始。

拒绝 naive datetime；所有 datetime 转为 UTC。DATE 保持日期原义。列表 SQL 明确按发生时间倒序，日期事件仅在排序时使用该日期的 UTC 零点，再按 UUID 倒序稳定决胜；不将 DATE 事件改写成时间事件。允许过去、未来和补录时间，均不影响关系状态。

EventParticipant：UUID `id / workspace_id / event_id / person_id`，可空字符串 `role`（第一版最多 200 字符），可空文本 `note`，UTC-aware `created_at`。一个人物一个事件仅一行；没有 Role 字典表。

## 事务与生命周期

Domain 使用 frozen dataclass、UUID、date、aware datetime，不依赖 Qt／SQLAlchemy。Repository 注入当前 Session，负责映射、flush、SQL 排序，不创建 Engine／Session、不 commit／rollback；UI 不接触 ORM。

EventService 将 Event 与所有参与人的校验、写入置于同一个 `write_operation`。任意参与人失败或提交失败均整体回滚。更新参与人列表也与事件字段修改原子提交，保留未移除参与人的 UUID／created_at；允许明确修改 role／note。字段或参与人编辑后 `updated_at` 严格递增，包括固定测试时钟情形。

Person 归档不会改动历史 Event 或 EventParticipant。新建事件及新增参与人时拒绝 archived Person；既有 archived participant 可保留并显示 `Archived`，不因编辑自动删除。明确“删除参与人”是用户操作，不是归档级联。

Event 使用 soft delete，默认列表隐藏；显式 `include_deleted` 可读。删除／恢复不删除参与人、History 或其他事实，不修改关系状态。恢复保留标题、时间、描述、原参与记录与历史引用。已删除 Event 不允许普通内容和参与人编辑，但可明确关联／解绑其历史引用。

## History append-only 的经确认例外

History 的状态、备注、effective_at、created_at、身份仍不可更新或删除；状态变化继续 append，current state 继续按既有 Phase 4 规则派生。

用户明确确认：`event_id` 是唯一可受控原地修改的关联元数据，不是历史状态修改。`EventService.attach_event_to_history(workspace_id, history_id, event_id)` 可关联或传 `None` 解绑；`attach_relationship_history` 为同义显式入口。操作检查 Workspace、History、Relationship、Event 是否同域并存在，不创建 History，不改变关系 updated_at 或 current state。soft-deleted Event 保留且可作为历史引用。

专用 Repository 先 flush，再短暂授予精确 `(history_id, old_event_id, new_event_id)` 的单行授权；ORM guard 必须同时满足仅 event_id 一个字段改变、授权行和旧／新值匹配，finally 撤销授权。普通 ORM event_id 修改、混改状态或 delete 仍拒绝。不提供 History 通用 update／delete API。UI 关联与解绑都携带打开对话框时的实际引用快照，在同一写事务中核对当前引用；若另一详情已把 History 关联到其他 Event，本次冲突拒绝并刷新旧详情，既不误解除也不覆盖新关联。继承 Phase 4 的保护范围：管理员 raw SQL／Core bulk 不是 ORM guard 的数据库触发器级保证。

## UI 与基础搜索

Events：Workspace 选择、列表、标题／描述／类型的基础搜索、新建、编辑、软删除、显示已删除、恢复、详情、参与人添加／删除、明确关联／解除 History。

Person Detail：Relationships 之外增加 Events Timeline，显示发生日期／时间、标题、类型、FACT／OBSERVATION、参与人及简述；点击详情使用普通 Qt Widget。页面激活与事件 UI 操作后刷新。Events 与 People 各自显式选择 Workspace，从 Timeline 打开的详情锁定该人物事件所在 Workspace。

搜索转义 `% / _ / \\` 并限定 Workspace，不实现 FTS5。查询按发生时间明确 SQL ORDER BY；新索引覆盖 Workspace + started_at／occurred_on／deleted_at／event_type，以及参与人和历史关联查询。

## Migration

新增 [0004 migration](<../alembic/versions/0004_event.py>)，旧 0001／0002／0003 不改：创建 Event 与 Participant，再 batch 修改 History 的 event_id／复合 FK／索引。SQLite 保持 foreign_keys=ON，重建 History 复制原有事实列与命名约束／旧排序索引，不重写历史数据。

业务表数：0002 为 5，0003 为 7，0004 为 9（不含 alembic_version）。验证空库逐级升级、seeded `0003 → 0004 → 0003 → 0004`，以及 migration 故障回滚。降级先移除 History 的新增关联 FK／索引／列，再删除 Participant／Event。

**降级会丢弃 Phase 5 的 Event／Participant 数据和 event_id 引用，不是备份恢复；仅保证原 Phase 1–4 表与 History 原有事实数据不受损。** 已存在旧数据库不会静默升级，须关闭程序、备份完整数据目录，再用应用相同配置执行 Alembic。

SQLite 执行真实测试；PostgreSQL 使用 ORM／冻结迁移 DDL compile 验证，不声称实际 PostgreSQL 部署通过。
