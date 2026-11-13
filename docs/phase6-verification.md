# Phase 6 验收记录

## 最终结果

最终执行 Phase 1–6 全量回归，退出码 **0**：

| 收集项 | passed | environment-skipped | failed |
| ---: | ---: | ---: | ---: |
| **1003** | **998** | **5** | **0** |

耗时 122.18 秒（最后一次回归：pytest 默认临时根，仓库内 `.test-runs/`）。Phase 5 的 863 项结果见 [Phase 5 验收记录](<phase5-verification.md>)；本次保留全部旧业务测试，只把 head／表集合断言更新到 `0005_evidence` / 11 张业务表，显式升级到 0004 的历史 schema 测试保持冻结语义。**没有删除、弱化或改写任何断言，5 项 symlink 跳过原样保留。**

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

真实跳过原文：

```text
SKIPPED [5] tests\storage\test_media_store.py:275:
OS does not grant temporary-test symlink creation
998 passed, 5 skipped in 122.18s
```

## 后续清理与测试合并

交付完成后做了一次仓库清理与测试合并，**覆盖率没有下降**：

- 删除缓存与测试残留共 **791.91 MB**：`.test-runs/`（735.09 MB，含 Phase 5／Phase 6 各轮留在旧 revision 的测试库）、`pytest-of-Tesla/`（53.19 MB）、空 `pytest-cache-files-*`、`.pytest_cache/` 与全部 `__pycache__/`。工作区（不含 `.venv`）从 792.85 MB 降到 0.94 MB。真实用户数据库不在工作区，未被触碰。
- 定位并修掉残留根因：本机受限环境**无法写入用户临时目录**（实测 `PermissionError`），`tempfile.gettempdir()` 因此回退到当前目录，pytest 会把上千个测试库写进仓库。新增仓库级 [conftest.py](<../conftest.py>)：只有当系统临时目录真的不可写时才把临时根固定到被忽略的 `.test-runs/`，并用真实探针文件判断；正常机器行为不变。同时在 `pyproject.toml` 设置 `tmp_path_retention_policy = "failed"`，只保留最近数轮且仅保留失败用例的临时目录。
- 合并重复测试 **25 项**（可收集数 1028 → 1003）：把 `tests/repositories/` 中通过原始 ORM 插入、与 `tests/migrations/` 完全重复的数据库约束用例（CHECK／复合 FK／UNIQUE／NULL-role partial unique）与 ORM guard 用例（9 个原始字段、metadata 允许项、delete 拒绝）统一并入迁移测试这一权威位置，后者断言更强（含回滚后原值恢复）。仓储测试只保留适配器自身职责，并新增 1 项「metadata 更新不触碰原始材料」的适配器断言。Phase 5 的同名重复项同样并入迁移测试。既有断言无一删除或放松。


## 交付范围

- [Evidence Domain](<../app/domain/entities/evidence.py>)、[EvidenceMedia Domain](<../app/domain/entities/evidence_media.py>)：普通 Python UUID／str／UTC-aware datetime；kind 与 source_type 白名单；原始内容按原样保存。
- [ORM](<../app/storage/models.py>)：`evidence`、`evidence_media`，命名复合 FK／UNIQUE／CHECK／索引，Null-role partial unique index，末尾两个 guard（9 个原始字段禁改、禁止物理删除）。
- [Repository 协议](<../app/repositories/evidence_interfaces.py>) 与 [SQL 适配器](<../app/storage/evidence_repositories.py>)：只有 `update_metadata`，没有 content／source／captured_at 更新入口，也没有通用覆盖 save。
- [EvidenceService](<../app/services/evidence.py>)：kind 业务规则、原始内容保护、附件快照冲突、原子事务、软删除／恢复。
- [0005 migration](<../alembic/versions/0005_evidence.py>)：0001–0004 不改，只新增两表。
- [Evidence 面板](<../app/ui/evidence_panel.py>)、[证据对话框](<../app/ui/evidence_dialog.py>)、[Events 页面](<../app/ui/events_page.py>) 接入。
- Phase 3 媒体引用检查扩展：`MediaRepository.has_references` 现在同时覆盖人物引用与证据引用（经用户确认）。
- [证据设计](<evidence.md>)、[架构](<architecture.md>)、[数据库](<database.md>)、[README](<../README.md>) 已更新。

## 核心语义验收

| 验收 | 实际覆盖 |
| --- | --- |
| 在 Event 下创建 Evidence | Service／Repository／UI 使用真实迁移与 SQLite；跨 Workspace 的 Event 被拒绝 |
| 原始文本与来源信息 | `content_text` 原样保存（含空白），`source_type`／`source_label`／`source_reference` 独立字段 |
| FACT 与 OBSERVATION 事件 | 两种事件的证据都真实创建与查询 |
| 图片／文件附件 | 经 Phase 3 `MediaService.import_file` 导入后绑定；FILE 与 IMAGE 规则各自验证 |
| 同一 Media 多 Evidence 复用 | 同一 Media 被两个 Evidence 引用；同一 Evidence 多附件；`role` 不同视为不同绑定 |
| Cross Workspace 阻止 | 复合 FK 真实拒绝（含双方都存在但不同域的情形），Service 二次校验 |
| 删除 Evidence 不误删 Media | 原始文件字节不变、Media 行未删除；解绑只删绑定行 |
| 删除 Event 不丢 Evidence | Event 软删除后 Evidence 仍在，默认隐藏、`include_deleted` 可见，内容未被修改 |
| 恢复后数据一致 | 恢复 Event 后自动重新可见；恢复 Evidence 只清 `deleted_at` |
| 原始内容不可直接修改 | Service 拒绝非 title／annotation 的键；Repository 无更新入口；ORM 9 个字段 guard 正反例 |
| UI 只改元数据 | 详情对话框「原始证据」组内无可写控件，只有注释可保存 |
| stale attachment | 添加与移除都携带快照；不一致时 `ConflictError` 且数据库不变；并发添加恰好一个成功 |
| Migration 0004 ↔ 0005 | 0/5/7/9/11 计数、seeded 往返九表完整、降级无 `%evidence%` 对象、PRAGMA 校验 |
| Phase 1–5 继续通过 | 1023 passed 全量回归 |

相关测试：

- [Domain](<../tests/domain/test_evidence_entities.py>)
- [Repository／约束与 guard](<../tests/repositories/test_evidence_repositories.py>)
- [Service／原子性与冲突](<../tests/services/test_evidence_service.py>)
- [UI](<../tests/ui/test_evidence_ui.py>)
- [迁移与 PostgreSQL compile](<../tests/migrations/test_evidence_schema.py>)

## 关键设计决定

1. **`role` 为 NULL 的唯一性**：SQL 中 NULL 互不相等，`UNIQUE(evidence_id, media_id, role)` 挡不住重复行，因此额外增加 `WHERE role IS NULL` 的 partial unique index，数据库在两种情况下都真实拒绝。
2. **最后附件规则（用户确认）**：IMAGE／FILE 必须始终保留至少一个附件；移除最后一个会被拒绝，提示软删除后重建。TEXT／MANUAL 等不受限。
3. **Media 软删除引用检查（用户确认）**：`has_references` 同时统计人物引用与证据引用，保持 Phase 3"无有效引用才能删除"的原意；证据软删除后即可删除媒体。只扩检查，不改 Media 其他语义。
4. **已删除 Event 下的证据**：不允许新建 Evidence；既有证据仍可读、可改 metadata、可增删附件（纯链接操作，与 Phase 5 历史关联一致）。UI 在 Event 删除期间把面板置为只读，等恢复后再操作。
5. **`captured_at` 与 `created_at` 分离**：前者是证据被获取的时间（可空），后者是进入系统的时间（必填），两者都强制 UTC。

## Migration 与 PostgreSQL

[迁移验收测试](<../tests/migrations/test_evidence_schema.py>) 真实运行 SQLite：`0001 → 0002 → 0003 → 0004 → 0005` 业务表数 0／5／7／9／11；seeded `0004 → 0005 → 0004 → 0005` 保留原九表数据与约束；降级后 `sqlite_master` 无 `%evidence%` 对象且 `get_indexes` 抛 `NoSuchTableError`，再升级为空表；CHECK／FK／UNIQUE／partial unique 真实拒绝非法写入；`PRAGMA foreign_keys=1`、`PRAGMA foreign_key_check` 无错误。

**降级 0005 会丢弃 Evidence／EvidenceMedia，不是备份恢复。** 已存在旧库仍拒绝静默升级，需备份整个数据目录后用相同配置显式 `alembic upgrade head`（最新 head 为 `0005_evidence`）。

PostgreSQL 只做 ORM 与冻结迁移 DDL compile（UUID、TIMESTAMP WITH TIME ZONE、复合 FK、UNIQUE、四个 CHECK、partial unique index）并用 mock engine 执行 0005（2 次 CREATE TABLE、2 次 DROP TABLE、无 INSERT）；**未运行真实 PostgreSQL 服务，不宣称部署验证。**

## 桌面启动

在全新空临时库上用原生 Windows Qt 启动，退出码 0，同时确认空库迁移到新 head：

```text
Empty database initialized through Alembic
Database schema version verified
Application started (Phase 6)
GUI smoke test passed: window visible and five pages navigable; Qt platform=windows
Application stopped: 0
```

自动 UI 测试使用 offscreen；此 smoke 不等同于人工 DPI／布局评审。所有人物／事件／证据验收 fixture 都在 pytest 临时目录，没有生成示例业务数据。

## 边界

仅十一张正式业务表。没有 Analysis／AnalysisSource／Memory／LLM／Graph／Sync／ChangeLog／Revision，也没有 OCR、聊天导入、网页抓取、自动事件提取、自动摘要、自动关系判断或向量库；不建立 Source 表。未宣称完成 FTS5 统一搜索、真实 PostgreSQL、其他 OS、完整 backup／恢复或安装包分发。

**完成 Phase 6 后停止。**
