# Phase 10 验收记录（稳定化 / 精简 / 数据完整性 / 备份 / 导出）

## 最终结果

| 收集项 | passed | environment-skipped | failed |
| ---: | ---: | ---: | ---: |
| **1378** | **1373** | **5** | **0** |

退出码 **0**。5 项仍是 Windows symlink 权限限制，**未删除、未弱化、未计为 passed**。

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

```text
SKIPPED [5] tests\storage\test_media_store.py:275:
OS does not grant temporary-test symlink creation
1373 passed, 5 skipped
```

## 一、精简验收报告（规格第 19 节）

### 1. 删除了多少重复代码

| 项目 | 之前 | 之后 | 说明 |
| --- | --- | --- | --- |
| UI 操作边界 `run_operation` | **21 份**实现（10 处模块级函数体 + 方法体），约 148 行重复逻辑 | **1 份**实现（[operations.py](<../app/ui/operations.py>)，41 行含文档）+ 12 处一行委托 | 每个界面保留自己的日志前缀与标题，调用点零改动 |
| 存储层行↔Domain 映射 | 5 个模块各一份 `_values`/`values`/`_domain`/`snapshot`（约 60 行） | [mapping.py](<../app/storage/mapping.py>)（31 行）+ 5 处别名导入 | 同时修掉"从 relationship 模块导入通用辅助"的错误归属 |

净减少约 **120 行重复逻辑**，并把"同一规则两处实现"清零到各自唯一的所有者。

### 2. 合并了哪些真正重复的职责

- **UI 错误契约**：21 份副本里有 1 份（关系面板）漏掉了 `OSError`，且未知错误文案与其它 20 份不同。合并后所有界面共用同一异常集合与同一文案，**这是本次合并修掉的一个真实漂移**。
- **存储映射**：`event_repositories` 与 `search_projection` 原本从**关系**模块导入 `snapshot`/`values` —— 通用职责挂在了错误的模块上，现在统一由 `app/storage/mapping.py` 拥有。

### 3. 刻意保留的结构（并说明理由）

- **各聚合自己的 `_query()` workspace 作用域**：8 处写法相似但**语义不同**——关系查询还会 join 两个端点并过滤"端点已归档"的关系。参数化合并会造出一个比 5 行原码更难读的假抽象，因此按"不要用 Generic 抹掉领域语义"保留。
- **Repository Protocol 按聚合拆分**：承载 workspace 隔离与软删除语义，**拒绝** `GenericRepository[T]`。
- **各服务的 repository bundle**（`AnalysisRepositories`/`MemoryRepositories`）：定义"这个服务允许触碰哪些适配器"，是边界而不是重复。
- **Domain 实体与不变量、`transaction()`/`write_operation()` 工作单元、LLM 传输层与凭据解析、复合外键 schema**：核心语义，一行未动。
- **测试 fixture 未强行合并**：各阶段 fixture 的实体组合、时钟与断言前提都不同，抽成共享工厂会退化成"给每个 fixture 传一堆参数"，风险大于收益。这是**审查后决定不做**，不是遗漏。

### 4. 是否改变公共接口

- 新增：`app/ui/operations.py`、`app/storage/mapping.py`、`ApplicationServices` 新增 4 个字段（`integrity`/`backups`/`backup_verification`/`exports`，全部可选、附加式）。
- 保持：`app/ui/event_detail.py` 仍可 `import run_operation`（改为重新导出共享实现）；`app/storage/relationship_repositories.values/snapshot` 名称保留（现为共享实现的别名）；所有服务方法签名未变。
- **唯一行为变化**（已在上文说明）：关系面板的操作边界现在也把 `OSError` 当作已知错误，并使用统一的未知错误文案。

### 5. 是否改变数据库 schema / Domain semantics

**没有。** 事实层 11 张表、搜索投影、分析层 5 张表、`memory` 全部原样；**本阶段未新增任何 migration**，`0001–0008` 一行未改。Domain 实体、不变量、软删除与 workspace 隔离语义完全未变；Search／Timeline／Graph／Analysis／Memory 的回归全部通过。

## 二、规格测试项对照

| 规格条目 | 覆盖 |
| --- | --- |
| DataIntegrity 1–10（干净库、坏 FK、文件缺失、孤儿文件、hash 不符、workspace 不一致、来源悬空、Memory 来源非法、过期 Memory、审计不改数据） | [tests/services/test_integrity_service.py](<../tests/services/test_integrity_service.py>)（28 项） |
| Backup 11–17（WAL 运行中快照、快照可打开、媒体已复制、manifest、hash 正确、失败不破坏源、不修改源） | [tests/services/test_backup_service.py](<../tests/services/test_backup_service.py>)（21 项） |
| Restore 18–23（临时副本恢复、schema 有效、FK 干净、媒体 hash、应用可打开、数据与源一致） | 同上 |
| Export 24–28（导出、跨 Workspace 排除、秘密排除、按选择包含 Analysis、JSON 可解析） | [tests/services/test_export_service.py](<../tests/services/test_export_service.py>)（9 项） |
| Settings 界面（四个显式操作、打开页面不自动执行、结果展示、隐私） | [tests/ui/test_settings_ui.py](<../tests/ui/test_settings_ui.py>) |
| 精简回归（UI 边界合并后行为不变） | `tests/ui`、`tests/test_phase1.py`、`tests/test_application_startup.py` |
| 完整回归 Phase 1–9 | 1373 passed 全量回归 |

## 三、关键实现与实测结论

1. **备份用 SQLite 在线备份 API**（`sqlite3.Connection.backup()`），**不是**复制文件：测试在"最近事务只存在于 WAL"的状态下做备份，断言该标记行**不在源主库文件字节里**、却出现在快照中——这是"不是撕裂快照"的硬证据。快照随后收敛为自包含单文件再取 sha256。
2. **审计与备份都只读源**：审计对全部 18 张表（含 FTS5 影子表）与媒体目录清单/哈希做逐行列值快照比对；备份前后源库与源媒体同样逐行/逐字节不变。
3. **只报告不修复**：孤儿媒体文件只记 `ORPHAN_MEDIA_FILE` 警告且文件仍在（GC 留给以后）。审计绝不删除、绝不补回缺失文件、绝不修复 FK。
4. **恢复校验在临时副本上进行且总是清理**，"备份文件存在"从不作为通过依据；10 个命名检查全部通过才 `VERIFIED`。
5. **导出不是备份**：JSON 明确带 `"is_backup": false` 与 README 说明；没有任何凭据字段（连 `credential_ref` 都不出现），Analysis 默认只导出元数据，原始回答与上下文快照必须显式选择。
6. **全部显式触发**：启动不 hash、Dashboard 不审计、不自动备份、不自动上传。

### 诚实的边界与不可达状态

- `PERSON_WORKSPACE_MISMATCH` / `MEDIA_WORKSPACE_MISMATCH` 的"父行属于别的 Workspace"在 schema 下**不可达**（父行就是 workspace 本身），它们只在父行缺失时报，且那必然同时是 FK 违规；其余 6 个 workspace mismatch 由复合外键阻止，测试用 `PRAGMA foreign_keys=OFF` + 真实存在的跨 Workspace 父行构造可达用例，**没有伪造**。
- `MEMORY_STATE_INVALID` 的 `confirmed_at IS NULL` 分支被列的 `NOT NULL` 阻止，检查保留但注明不可达。
- `PRAGMA foreign_key_check` 是**库级**检查，不受 Workspace 过滤影响；报告里用 `counts["foreign_key_check_scope"]=="database"` 如实标注。
- 未覆盖：磁盘满/掉电/跨盘符 rename、大媒体量进度回调；PostgreSQL 仍只做 DDL compile。

## 四、桌面启动

全新空临时库、原生 Windows Qt，退出码 0，6 页导航可达，日志显示 `Application started (Phase 10)`。自动 UI 测试使用 offscreen；此 smoke 不等同于人工 DPI／布局评审。全部验收 fixture 位于 pytest 临时目录，未生成示例业务数据，也未触碰真实用户数据库。

## 五、边界

未实现：Sync、Revision、ChangeLog、MemoryRevision、Vision、OCR、RAG、向量库、embedding、自动 Memory、自动分析、自动事实修改、冲突解决、多用户、云、Docker、Web API、PostgreSQL server。仍然是本地优先的 Qt 桌面应用；**本阶段未新增任何数据库表或 migration**。

**完成 Phase 10 后停止。**
