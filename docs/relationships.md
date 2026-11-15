# Relationship / RelationshipHistory（Phase 4）

## 身份与隔离

```text
Workspace 1 ── N Relationship
Person (workspace,id) ── Relationship端点A
Person (workspace,id) ── Relationship端点B
Relationship (workspace,id) 1 ── N RelationshipHistory
```

[纯实体](<../app/domain/entities/relationship.py>) 与 [History实体](<../app/domain/entities/relationship_history.py>) 采用frozen dataclass、真实UUID和UTC-aware datetime，不依赖Qt/SQLAlchemy/SQLite。未引入Entity基类、复杂类型表、原生ENUM或后续空表。

Relationship字段：id、workspace_id、person_a_id、person_b_id、relationship_type、directionality、created_at、updated_at、deleted_at。类型trim后1–200，大小写敏感；Service去空白，Domain拒绝未规范文本，DB有trim/长度CHECK。

UNDIRECTED在Service按UUID.int排序，DB要求a<b；DIRECTED原样保存方向。自关联被Domain/Service/DB拒绝。两端点分别用复合FK到Person，History用复合FK到Relationship，不能跨Workspace。

完整自然键：

```text
UNIQUE(workspace_id, person_a_id, person_b_id, relationship_type, directionality)
```

该键覆盖已删除记录。不同type可并存；同type不同directionality可并存；DIRECTED反向是不同身份；重复相同身份被拒绝。没有自动大小写折叠或方向猜测。

## 不可变身份与删除

类型、方向、Workspace和两个人物端点是身份属性，创建后**没有公共Service update或UI编辑入口**。录入错误需soft delete旧记录，再创建不同的正确身份。同身份旧记录已删除时仍须恢复原id，不能另建第二条。

[仓储](<../app/storage/relationship_repositories.py>) 的save/update仅允许既有记录生命周期变化，比较原身份并拒绝修改；[ORM模型](<../app/storage/models.py>) before_update保护id/身份/created_at。History ORM 仍拒绝状态事实更新及删除；Phase 5 唯一新增例外是 EventService 授权的单行 event_id 关联元数据修改，普通 ORM event_id 赋值也拒绝。详见 [事件关联规则](<events.md>)。

这些是受控应用/ORM入口的保障，不是数据库触发器；管理员raw SQL/Core bulk可绕过ORM guards。不要声称所有外部写入也被阻止。数据库直接保障FK、唯一性、自关联、规范化和文本CHECK。

ENDED是状态History，绝不设置deleted_at。删除错误记录只改deleted_at，不添加ENDED。恢复删除记录清除deleted_at、保留所有History和原id，不自动写ACTIVE。要恢复ACTIVE显式追加新状态。

Person归档保留关系和历史，不自动写ENDED；默认关系查询隐藏已归档端点，审计读取可看原记录。恢复或新增状态时Service重新检查两个Person和Workspace均有效。旧PersonMedia解绑、Media保留与补偿语义不变。

## 单一状态真相

History 原 Phase 4 字段：id、workspace_id、relationship_id、state、effective_at、note、created_at。Phase 5 增加可空 event_id 复合 Event FK；没有 current_state/deleted_at。Domain和DB允许可扩展非空状态字符串；关系 Service/UI 继续只写 ACTIVE、PAUSED、ENDED。

- effective_at：用户声明实际生效时间。
- created_at：本次历史写入记录的UTC时间。
- 列表：effective_at ASC、created_at ASC、UUID ASC。
- 最新：三列DESC。

Repository.get_latest无as_of时返回整个时间线最后记录；**Service当前状态总是传入明确UTC as_of**，只考虑effective_at<=as_of。未来记录不提前生效；没有已生效记录时状态为None。允许后补过去记录，其写入时间与生效时间保持分开；后补较早记录不覆盖较晚当前状态。as_of表示状态生效截点，不实现双时态“当时系统已知信息”查询。

当前状态只出现在DTO派生property，不在Relationship表双存。相同effective_at/created_at由UUID确定顺序，重启后稳定，不承诺同微秒UUID顺序代表提交次序。

## Service与事务

[RelationshipService](<../app/services/relationship.py>) 返回Domain/DTO，支持create/get/current/list_by_person/list_by_workspace/history/add_history/soft_delete/restore。关系创建＋初始History、追加History＋更新时间均在同一write_operation中提交或整体回滚。

所有写操作复用BEGIN IMMEDIATE；每operation独立Session，Repository只flush不commit/rollback。History只add，不能upsert覆盖既有id。Service先验证scope和两个有效端点，规范化无向键后检查重复，DB唯一约束仍是最终保障。

[独立装配](<../app/services/relationship_repositories.py>) 不更改Phase3媒体仓储bundle。ApplicationServices增加尾部可选relationships字段，原四字段顺序不变；UI不访问Session/ORM。

## UI

[关系区域](<../app/ui/relationship_panel.py>) 在People人物详情下方，使用普通Qt表格和对话框，不是Graph。

- 添加时选择另一位当前Workspace人物、类型、方向和初始状态。
- 只提供修改状态，不提供类型/方向/端点编辑。
- 时间输入ISO且必须带时区；留空为现在，naive时间拒绝。
- 当前列表分别展示记录建立时间与当前状态生效时间。
- History显示生效/写入时间、状态、备注、UUID，保持排序只读。
- 显示已删除/归档端点checkbox支持历史查看；无有效端点时禁止修改状态/恢复。
- 状态按刷新时查询，不用后台任务篡改字段；未来生效后可点击刷新。

错误日志只记录技术类，不记录历史note、类型或完整人物档案。

## Migration / 验证边界

[0003](<../alembic/versions/0003_relationship.py>) 冻结标准SQLAlchemy类型和约束，只增两表，不引用应用Model，不修改0001/0002。降级到0002会删关系与History数据，但保留原五表；只能在临时库测试往返。

SQLite真实测试、PostgreSQL模型与迁移DDL compile、Alembic metadata check、ORM guard、故障回滚、并发重复拒绝、重启和UI均已覆盖，见 [验收记录](<phase4-verification.md>)。没有实际PostgreSQL服务/其他OS/DPI人工验证。完整 Revision／ChangeLog、历史事实更正流程仍未实现。Phase 5 的 Event FK 与显式关联不改变本页 Phase 4 状态派生规则，见 [事件](<events.md>)；不继续进入其他阶段。
