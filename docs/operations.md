# 数据管理：完整性审计 / 备份 / 恢复校验 / 导出（Phase 10）

Phase 10 不是新功能阶段，而是把已经完成的东西收紧、保护起来。它只增加**只读审计**、**文件级备份与恢复校验**、**导出**和对应的 Settings 界面；事实层、投影层、分析层与记忆层的语义**一行未改**，也**没有新增任何 migration**。

## 完整性审计（DataIntegrityService）

`services.integrity.check(workspace_id=None, *, verify_media_files=True) -> IntegrityReport`

**只读**：不 INSERT/UPDATE/DELETE、不跑迁移、不刷新搜索投影、不创建 Analysis／Memory、**不删除孤儿文件**。所有问题都只是报告。

`IntegrityReport` 永远是结构化对象，不是裸 bool：

```text
ok            errors 为空才为 True（warnings 不影响）
checked_at    UTC aware
errors        tuple[IntegrityIssue, ...]
warnings      tuple[IntegrityIssue, ...]
counts        各表行数与检查统计（数字与短字符串）
summary()     一行人类可读摘要
to_mapping()  JSON 可序列化（UI 与备份 manifest 复用）
```

`IntegrityIssue`：`code`（稳定机器码）／`severity`／`message`／`entity_type`／`entity_id`／`detail`。**message 里绝不出现 Memory 或 Evidence 的正文**，只有 id、相对路径与计数。

检查项：

| 类别 | 代码 |
| --- | --- |
| 外键 | `FOREIGN_KEY_VIOLATION`（`PRAGMA foreign_key_check`） |
| Workspace 一致性 | `PERSON_WORKSPACE_MISMATCH`、`RELATIONSHIP_WORKSPACE_MISMATCH`、`RELATIONSHIP_HISTORY_WORKSPACE_MISMATCH`、`EVENT_PARTICIPANT_WORKSPACE_MISMATCH`、`EVIDENCE_WORKSPACE_MISMATCH`、`EVIDENCE_MEDIA_WORKSPACE_MISMATCH`、`PERSON_MEDIA_WORKSPACE_MISMATCH`、`MEDIA_WORKSPACE_MISMATCH` |
| 引用／悬空 | `MEDIA_UNREFERENCED`（WARNING）、`PERSON_MEDIA_DANGLING`、`EVIDENCE_MEDIA_DANGLING`、`ANALYSIS_SOURCE_DANGLING` |
| 媒体文件 | `MEDIA_FILE_MISSING`、`MEDIA_FILE_SIZE_MISMATCH`、`MEDIA_FILE_HASH_MISMATCH`（ERROR）、`ORPHAN_MEDIA_FILE`（WARNING，**只报告**） |
| Memory | `MEMORY_SOURCE_INVALID`、`MEMORY_STATE_INVALID`（ERROR）、`MEMORY_EXPIRED`（WARNING） |

- `verify_media_files=False` 只做数据库检查，不触碰文件系统。
- 媒体存储不可用时记 `MEDIA_STORE_UNAVAILABLE` 警告并继续，不崩。
- 大媒体库的 hash 校验很慢，因此**必须显式触发**：启动不跑、Dashboard 不跑、定时器不跑。

## 备份（BackupService）

```text
<destination>/database.sqlite3   一致快照（自包含单文件）
<destination>/media/…            被引用的媒体文件副本
<destination>/manifest.json      application_version / schema_revision / created_at /
                                 workspace_count / database_sha256 /
                                 media_file_count / media_total_bytes / media[]
```

- **一致性**：数据库用 **SQLite 在线备份 API**（`sqlite3.Connection.backup()`）从运行中的 WAL 库取快照，**绝不**直接复制 `database.sqlite3`／`-wal`／`-shm`——文件复制会静默丢掉只存在于 WAL 里的已提交事务。快照随后收敛为自包含单文件（`journal_mode=DELETE` + `wal_checkpoint(TRUNCATE)`），再取 `database_sha256`。
- **只读源**：不跑迁移、不改 `deleted_at`、不刷新搜索、不创建 Analysis／Memory、不改动任何媒体文件。
- **原子**：先写 partial 临时目录，全部成功才改名；失败清理临时目录，源库与既有备份不受影响。目标已存在且非空 → 拒绝覆盖。
- **不记录秘密**：manifest 里没有 API Key、凭据引用值、Authorization 头，也没有媒体绝对路径（只有相对路径）；**不含 Memory／Evidence 正文**。
- 媒体缺失或 hash 不一致 → 备份**失败**（静默带进损坏副本比没有备份更危险）。

## 恢复校验（BackupVerificationService）

在**临时目录的副本**上完成，绝不触碰生产库：

```text
manifest → database_sha256 → copy → schema_revision → integrity_check →
foreign_key_check → media_files → media_extra_files → application_open → integrity_audit
```

全部通过才 `ok=True`；`checks` 每项都有 name／ok／message，失败原因可读。**"备份文件存在"从不作为通过依据。** 校验结束清理临时目录（诊断信息已经在 `checks` 里，保留一份含私人数据的副本反而是第二个无保护数据库）。

`integrity_audit` 用**副本**的 engine 与 `MediaStore` 跑一次完整性审计；审计模块不可用时记为"已跳过"并写明原因，审计真的跑起来并报错则校验失败。

## 导出（WorkspaceExportService）

```text
<destination>/workspace.json   人物／别名／关系／关系历史／事件／参与人／证据／
                               证据附件／媒体／Memory／Analysis 元数据
<destination>/media/           被引用媒体文件副本
<destination>/README.txt       说明：这是导出，不是备份
```

- **Export ≠ Backup**：导出用于查看与搬运，**不能**直接覆盖生产数据库；恢复请走 Backup。
- **隐私**：导出中没有任何凭据字段（结构上就不存在），也不导出 LLM Provider 配置；Analysis 默认只导出元数据（provider、model、状态、时间、scope、来源 id），**原始回答与上下文快照必须显式选择**才包含，并在 `privacy` 段里如实标注。
- 单文件复制失败只记录在该文件的条目里（`copied=false` + 原因），不中断整个导出；事实仍然完整导出。

## Settings 界面

`Data Management · 数据管理`：`[Run Integrity Check]`、`[Create Backup]`、`[Verify Backup]`、`[Export Workspace]`，各自显示时间／位置／大小／状态。**全部显式触发**，没有云备份、没有自动上传、没有启动时自动执行；耗时的检查与备份在后台线程执行。
