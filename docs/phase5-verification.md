# Phase 5 验收记录

## 最终结果

最终执行 Phase 1–5 全量回归，退出码 **0**：

| 收集项 | passed | environment-skipped | failed |
| ---: | ---: | ---: | ---: |
| **863** | **858** | **5** | **0** |

耗时 109.24 秒（最后一次回归：`final-phase5b`）。原 Phase 4 的 662 项历史基线仍见 [原验收记录](<phase4-verification.md>)；本次保留旧业务测试，并针对当前 head 与冻结旧 revision 的差异更新明确断言，不删除、弱化或改写 symlink 测试结果。

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra --basetemp=G:\TestArea\.test-runs\final-phase5b
```

真实跳过原文：

```text
SKIPPED [5] tests\storage\test_media_store.py:275:
OS does not grant temporary-test symlink creation
858 passed, 5 skipped in 109.24s
```

5 项仍是 Windows symlink 创建权限限制，不计为 passed。测试代码和 skip 原因保留在 [媒体存储测试](<../tests/storage/test_media_store.py#L275>)。

## 交付范围

- [Event Domain](<../app/domain/entities/event.py>)、[EventParticipant Domain](<../app/domain/entities/event_participant.py>)：普通 Python UUID／date／UTC-aware datetime；FACT／OBSERVATION，拒绝 INFERENCE；文本和时间边界。
- [ORM](<../app/storage/models.py>)：Event、Participant、History.event_id，命名 CHECK／UNIQUE／复合 FK／索引。
- [Repository 协议](<../app/repositories/event_interfaces.py>) 与 [SQL 适配器](<../app/storage/event_repositories.py>)：当前 Session 注入、Domain 快照、显式发生时间 SQL 排序；不拥有 commit／rollback／Engine／Session。
- [EventService](<../app/services/event.py>)：原子创建／编辑、归档参与人规则、软删除／恢复、搜索、按人物查询、显式 History 关联／解绑。
- [0004 migration](<../alembic/versions/0004_event.py>)：不修改 0001–0003；在线 SQLite batch 与冻结定义的 offline SQL 路径。
- [Events UI](<../app/ui/events_page.py>)、[事件表单](<../app/ui/event_dialog.py>)、[可操作详情](<../app/ui/event_detail.py>)、[Person Timeline](<../app/ui/event_timeline.py>)，正式应用装配与页面刷新。
- [事件规则](<events.md>)、[架构](<architecture.md>)、[数据库](<database.md>) 与 [README](<../README.md>) 已更新。

## 核心语义验收

| 验收 | 实际覆盖 |
| --- | --- |
| Event CRUD、date／time、FACT／OBSERVATION | Domain、Service、UI 使用真实迁移与 SQLite；naive 时间、混合日期／开始、倒置结束、空／超长字段拒绝 |
| 多人物、角色／备注、唯一参与记录 | Repository UNIQUE 与 Service 校验；按 UUID 而非同名文本管理 |
| Event + Participants 原子提交 | 第二个参与人写入失败、编辑参与人写入失败、before_commit 失败时全操作回滚 |
| Workspace 双重隔离 | Participant 两个复合 FK 与 History Event 复合 FK真实拒绝跨域，Service 二次校验 |
| archived Person | 禁止新建／新增参与；归档不修改 Event／Participant；旧 archived 参与记录保留、显示 Archived；role／note 编辑不改变参与 ID／created_at |
| 删除／恢复 | 默认隐藏，include_deleted 显式查询；参与人、History 引用保留，关系状态不自动变化 |
| History 关联／解绑 | 同域明确操作、多条 History 引用、已删 Event 引用保留、归档关系／端点历史可关联；不新增 History、不改状态／时间／备注 |
| History append-only | 普通 ORM 更新／删除拒绝；唯一经用户确认例外为 event_id 单行旧／新值授权，混改字段与错误授权拒绝，finally 撤权 |
| 陈旧详情保护 | 同域关联与解绑都携带打开对话框时的实际引用快照，并在写事务内核对；两份非模态详情重关联后，旧详情既不能解绑也不能覆盖另一事件的最新引用 |
| Timeline／搜索／排序 | 标题／描述／类型字面搜索，转义通配符；混合 DATE／UTC 时间、微秒、未来／过去／补录，明确 SQL ORDER BY；简述安全纯文本显示 |
| 独立进程重启 | 临时验收库写入后，两个独立桌面进程启动，两个独立读取进程验证 Event／Participants／History 引用与 ACTIVE 状态持久一致 |

相关测试：

- [Domain](<../tests/domain/test_event_entities.py>)
- [Repository／授权保护](<../tests/repositories/test_event_repositories.py>)
- [Service／原子回滚](<../tests/services/test_event_service.py>)
- [UI／可操作详情](<../tests/ui/test_event_ui.py>)
- [独立进程重启](<../tests/test_event_restart.py>)

## Migration 与 PostgreSQL

[迁移验收测试](<../tests/migrations/test_event_schema.py>) 真实运行 SQLite：

- `0001 → 0002 → 0003 → 0004`，业务表数分别为 0／5／7／9（不含版本表）。
- seeded `0003 → 0004 → 0003 → 0004`，保留 Workspace／Person／Alias／Media／PersonMedia／Relationship／History 原有数据、约束及索引。
- 0004 的非空 History.event_id 可正确降级移除；降级后无 Event／Participant／event_id，再升级旧 History.event_id 为 NULL。
- 故障注入晚期 batch 索引创建，DDL／数据／revision 整体回滚。
- FK 始终启用，foreign_key_check 无错误，Alembic command.check 无 metadata 差异。
- offline SQL 生成不创建数据库，生成后的升级／降级 SQL 在真实 SQLite 执行并验证原数据。

**降级会丢弃 Phase 5 Event／Participant 和关联元数据，不是备份恢复；只保证原七表事实数据保留。** 已存在旧库仍拒绝静默升版，需关闭应用、备份整个数据目录、使用相同配置显式 Alembic upgrade。

PostgreSQL 仅验证 ORM 与冻结迁移 DDL compile（UUID、aware timestamp、复合 FK、CHECK、nullable event_id、索引，包括既有 partial index）；**未运行实际 PostgreSQL 服务，不宣称部署验证。**

## 桌面启动

两处原生 Windows Qt 启动（关联快照修复前后各一次）均为退出码 0，最新一次在全新空临时库上运行，同时确认 `0001 → 0004_event` 空库迁移链：

```text
Empty database initialized through Alembic
Database schema version verified
Application started (Phase 5)
GUI smoke test passed: window visible and five pages navigable; Qt platform=windows
Application stopped: 0
```

启动退出码 0。此为真实窗口／导航 smoke，不等同于人工 DPI／布局评审；自动 UI 测试使用 offscreen。所有人物／事件验收 fixture 均在 pytest 临时目录，无生产示例数据。

## 修改后的复核

只读复核指出一个陈旧引用问题：人物 Timeline 允许多个非模态详情，事件 A 的旧详情可能在事件 B 已把同一 History 改绑到 B 之后，仍按旧快照解绑，从而误删 B 的当前关联。

修复方式：**关联与解绑都携带打开对话框时的真实引用快照**，由 `EventService.attach_event_to_history(..., expected_event_id=...)` 在同一写事务内核对当前值；不一致时 `ConflictError` 拒绝并刷新旧详情，不修改关联。默认哨兵 `ANY_EVENT` 表示“调用方未提供已知引用”，`None` 表示“期望当前未关联”。

修复后再次全量回归 **858 passed、5 skipped、0 failed（退出码 0）**，并新增验收：

- Service：陈旧解绑、陈旧重新关联、归档关系／端点仍可显式关联、归档参与人 role／note 元数据编辑保留参与 ID 与 created_at。
- UI：两份详情同时打开、重关联后旧详情解绑被拒绝且不影响新关联。

## 环境处理与边界

本机用户级 PySide6 与 conda Qt 存在 DLL 冲突，使用 Python `-s` 禁用用户包，不修改业务断言。初期 pytest 临时目录遇到 Windows 文件权限拒绝；测试移至工作区临时路径，权限诊断按用户授权运行。脚本验证补充了测试父目录权限，但没有把其结论伪装成深层 pytest 目录已修好；用户随后将会话切换完全权限，最终完整测试在该环境运行。

权限 [诊断报告](<../permission-recovery/acl-report-28d3bd51309848798401263ba1f79927.jsonl>)、[恢复脚本](<../permission-recovery/acl-backup-5b955fbb7a4a4533b757df5776f806c4.json.ps1>) 和 [原权限备份](<../permission-recovery/acl-backup-5b955fbb7a4a4533b757df5776f806c4.json>) 保留；没有更改文件内容／所有者。原来用于撤销该父目录权限变更的命令是：

```powershell
pwsh -NoProfile -File 'G:\TestArea\permission-recovery\acl-backup-5b955fbb7a4a4533b757df5776f806c4.json.ps1' -Path 'G:\TestArea\.test-runs' -AllowRoot 'G:\TestArea' -Restore 'G:\TestArea\permission-recovery\acl-backup-5b955fbb7a4a4533b757df5776f806c4.json'
```

**后续清理说明：**该目录 `G:\TestArea\.test-runs` 已在项目清理中整体删除（其中只有测试临时库），权限授予也随之失效，撤销命令不再需要执行；`permission-recovery/` 证据文件保留。测试临时根现已由仓库级 `conftest.py` 统一接管，见 [Phase 6 验收记录](<phase6-verification.md>)。

仅九张正式业务表。没有 Evidence／Analysis／AnalysisSource／Memory／LLM／Graph／Sync／ChangeLog／Revision，也没有 OCR、聊天导入、自动提取、自动关系判断或示例业务数据。未宣称完成 FTS5、真实 PostgreSQL、其他 OS、完整 backup／恢复或安装包分发。

**完成 Phase 5 后停止。**
