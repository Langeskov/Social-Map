# Phase 4 验收记录

## 最终全量测试

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -rs
```

**662项：657 passed、5 environment-skipped、0 failed，退出码0。** 最终完整运行62.46秒。

| 范围 | 数量 |
|---|---:|
| 原Phase1–3测试（保留并阶段化schema/head断言） | 431（426通过、5环境跳过） |
| 新Relationship/History纯Domain测试 | 118 |
| 新Schema/0003/数据库约束/ORM guard | 35 |
| 新仓储与Service测试 | 67 |
| 新关系UI测试 | 11 |

5项仍是原 [MediaStore符号链接测试](<../tests/storage/test_media_store.py>)，原因 `OS does not grant temporary-test symlink creation`。没有删除、弱化、改成passed或提升Windows权限。本阶段未修改MediaStore/file_lock/媒体补偿实现。

运行环境沿用Windows、conda Python3.14.6、PySide6 6.11.0、SQLAlchemy2.0.51、Alembic1.20.0、platformdirs4.9.4、Pillow12.3.0。使用 `-s` 避免用户级Qt DLL冲突；Phase4未新增或升级依赖，没有绕过公共conftest。

## 业务与完整性验证

- UNDIRECTED按UUID.int规范化，A-B/B-A同自然键；DIRECTED保持方向，反向可并存。
- type trim1–200、大小写敏感，多类型并存；身份自然键包含directionality和deleted记录。
- Service无公共身份update，UI表只读；Repository比对身份，ORM guard拒绝id/端点/Workspace/type/direction/created_at变化。
- 两端点和History的Workspace复合FK直接数据库拒绝跨scope；自关联、未规范无向、非法type/direction/state CHECK真实拒绝。
- History append-only、同id不能upsert；ORM修改/删除被拒绝。
- 当前状态仅派生，无current_state列；effective_at/created_at/UUID三重排序，future/as_of/backdated边界稳定。
- ACTIVE→PAUSED→ACTIVE→ENDED→ACTIVE始终同Relationship id。
- soft delete不添加ENDED；restore不自动ACTIVE、不增加History；重复操作保持幂等。
- 同身份已ENDED或已删除均不创建第二条；明确冲突指向追加状态/恢复。
- 已删除Person/Workspace不允许建立关系或追加状态/恢复；历史审计仍保留。
- 两线程各自独立Session同自然键并发创建：仅1关系＋1初始History，另一方Conflict。
- History真实flush后注入故障、before_commit create/add/delete/restore失败均整体回滚；Relationship更新时间也回滚。
- Person归档仍解绑PersonMedia、不误删共享Media文件；关系/History保留，不自动ENDED。
- 重启后UUID、历史顺序、UTC时间和状态一致；关闭Session后的返回值仍为可读Domain/DTO。
- 错误和冲突日志不输出私人type、History note或完整人物数据。

## 迁移回归

旧0001/0002文件未修改；独立 [0003](<../alembic/versions/0003_relationship.py>) 只增两表，原五表模型字段/约束和DDL保留。

测试包含：

```text
0001（仅版本表）→0002（五业务表）→0003（七业务表）
0002→0003→0002→0003
base/0001循环仍保留
```

SQLite真实运行验证原五表sentinel数据、PersonMedia双端FK/头像unique、Media dedup约束及schema未被破坏；新复合FK/unique/check命名稳定。最新Alembic command.check无待生成变化。

模型和冻结迁移的PostgreSQL DDL均compile通过，**没有运行真实PostgreSQL服务器**，不能将compile描述为部署验证。

## UI与原生窗口

UI自动测试使用真实迁移临时库＋Service，覆盖添加关系、入向/出向/无向显示、多type、状态变化、History只读、ENDED/删除区别、恢复保持状态、归档端点禁写、Workspace切换清空旧上下文、带时区输入与naive拒绝、future状态及日志隐私。

另在自动清理的临时库做正常Windows Qt验收：

1. 0001→0002→0003→0002→0003，并执行metadata check。
2. 临时fixture创建两Person、朋友/同事/有向关系；不是应用种子数据。
3. 朋友关系PAUSED→ACTIVE→ENDED，soft delete并恢复后仍ENDED，再显式ACTIVE，id不变，History五条。
4. 原生窗口显示真实关系控件，三行关系，History对话框可见。
5. 两次独立应用进程smoke test，日志确认 `Qt platform=windows`，均退出0。
6. 重开库验证版本0003、七业务表、三关系、同id与原History tuple完全一致；error.log为空。

所有业务fixture均在临时目录，退出清理；没有升级/改写用户真实数据库。原生控件可见性与自动操作测试不是人工视觉/DPI评审。

## 修改范围与边界

新增两Domain、两Protocol、SQL关系仓储、独立Service装配、RelationshipService、Person Detail控件及0003；现有Person/Workspace/Media业务逻辑、Session/BEGIN IMMEDIATE和媒体锁/补偿未改。ApplicationServices仅尾部增加可选relationships，原四字段顺序保留。

管理员raw SQL/Core bulk可绕过ORM guards；当前保障受控应用入口和数据库FK/unique/check，不宣称触发器级历史防篡改。没有Revision/ChangeLog、History更正或Event外键。

实际PostgreSQL、其他OS、DPI人工验收、backup/恢复/GC/断电磁盘满、wheel/exe分发等边界仍未完成。Phase3历史验收保留在 [历史记录](<phase3-verification.md>)。

**当前结束于Phase4，不进入Event/Evidence/Analysis/Memory/LLM/Graph/Sync/ChangeLog，也不建立这些空表。**
