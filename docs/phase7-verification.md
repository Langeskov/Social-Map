# Phase 7 验收记录

## 最终结果

最终执行 Phase 1–7 全量回归，退出码 **0**：

| 收集项 | passed | environment-skipped | failed |
| ---: | ---: | ---: | ---: |
| **1148** | **1143** | **5** | **0** |

耗时 189.78 秒。Phase 5 的 863 项、Phase 6 的 1003 项结果见各自验收记录；本次保留全部旧业务测试，只把 head／表集合断言更新到 `0006_search_projection`，并把"业务事实表（11 张）"与"完整 schema（含投影对象）"在断言里明确区分。**没有删除、弱化或改写任何断言，5 项 symlink 跳过原样保留。**

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

真实跳过原文：

```text
SKIPPED [5] tests\storage\test_media_store.py:275:
OS does not grant temporary-test symlink creation
1143 passed, 5 skipped in 189.78s
```

## 交付范围

- **Unified Search**：[FTS5 投影存储](<../app/storage/search_projection.py>) + [SearchService](<../app/services/search.py>)，覆盖 Person／Alias／Event／Evidence，中文按单字分词、查询安全转义、bm25 排序、Workspace 隔离、软删除过滤与 `rebuild_index()`。
- **Timeline**：[TimelineService](<../app/services/timeline.py>) 与 [TimelinePanel](<../app/ui/timeline_panel.py>)，Person／Workspace 两种模式，Event 与关系状态变更合并排序（含未来与补录时间）。
- **Relationship Graph**：[GraphPage](<../app/ui/graph_page.py>)，QGraphicsView 静态渲染，节点／边／方向箭头／状态、类型过滤、已归档与已删除开关、点击进入详情。
- **Dashboard**：[DashboardService](<../app/services/dashboard.py>) 与 [DashboardPage](<../app/ui/dashboard_page.py>)，真实统计与四类最近记录，点击跳转。
- **Migration 0006**：[0006_search_projection](<../alembic/versions/0006_search_projection.py>)，只新增可重建的搜索投影，不新增业务事实表。
- [投影层设计](<projection.md>)、[架构](<architecture.md>)、[数据库](<database.md>)、[证据](<evidence.md>)、[README](<../README.md>) 已更新。

## 规格测试项对照

| 规格条目 | 覆盖 |
| --- | --- |
| Search 1–12（Person／Alias／Event／Evidence、workspace 隔离、软删除、include_deleted、特殊字符、操作符转义、rebuild、事实修改后更新、删除恢复一致） | [tests/services/test_search_service.py](<../tests/services/test_search_service.py>) |
| Timeline 13–20（Person event、Person relationship history、workspace timeline、date／datetime event、future、backdated、稳定排序） | [tests/services/test_timeline_service.py](<../tests/services/test_timeline_service.py>)、[tests/ui/test_phase7_panels.py](<../tests/ui/test_phase7_panels.py>) |
| Graph 21–30（nodes、edges、directed／undirected、type filter、archived／deleted 过滤、click node／edge、no persistence） | [tests/ui/test_graph_ui.py](<../tests/ui/test_graph_ui.py>) |
| Dashboard 31–35（recent event／relationship／evidence、counts、workspace 隔离） | [tests/services/test_dashboard_service.py](<../tests/services/test_dashboard_service.py>)、[tests/ui/test_dashboard_ui.py](<../tests/ui/test_dashboard_ui.py>) |
| Migration 36–39（0005→0006、0006→0005、rebuild search index、no fact data loss） | [tests/migrations/test_search_projection_migration.py](<../tests/migrations/test_search_projection_migration.py>) |
| 完整回归 Phase 1–6 | 1143 passed 全量回归 |

## 关键实现决定与实测发现

1. **中文必须自己分词。** FTS5 默认 `unicode61` 把整段中文当成一个 token，`摄影` 匹配不到 `共同参加摄影活动`；本机 SQLite 3.53.2 **没有** trigram 分词器。因此索引时把 CJK 连续段拆成单字、拉丁串保持完整，查询构造成带引号的短语（`"摄 影"`）与前缀（`"photo"*`），用户输入永不直接进入 `MATCH`。
2. **投影同步在事实事务内。** 事实适配器在写入后立即更新投影，因此不存在"事实已提交、索引未更新"的窗口；投影被清空或损坏时 `rebuild_index()` 或下一次搜索会自愈重建。
3. **FTS5 对象不进 autogenerate。** FTS5 虚表与其五个 shadow 表是 SQLite 自己物化的基础设施，未映射为模型；`alembic/env.py` 增加 `include_object` 过滤，否则 `alembic check`／`--autogenerate` 会持续报告 6 个 `remove_table` 差异，甚至可能生成"删除搜索索引"的迁移。
4. **`--sql` 离线渲染。** 迁移里的 FTS5 DDL 使用 `op.execute(...)`；`op.get_bind().exec_driver_sql(...)` 在离线模式下没有对应实现（`MockConnection`），会直接崩溃。
5. **SQLite 临时目录（真实缺陷根因）。** `DROP TABLE` FTS5 虚表需要可写的临时目录，本机禁止写系统临时目录，SQLite 因此返回 `SQLITE_CANTOPEN`（"unable to open database file"）。该目录在 C 层只解析一次，所以必须在建立首个连接前把 `SQLITE_TMPDIR`／`TMPDIR`／`TEMP`／`TMP` 指向可写目录（[conftest.py](<../conftest.py>) 的 `enable_sqlite_temporary_files()`）。已实测：同样的迁移，临时目录不可写 ⇒ 降级失败；可写 ⇒ 往返、原子回滚、离线往返全部通过。
6. **Media 引用规则按 Phase 7 修正**：只要 `EvidenceMedia` 行仍存在就算引用，**Evidence 软删除不再释放 Media**（软删除刻意保留绑定行）。Phase 6 的两处旧断言已按新规则更新，`has_references` 的注释与文档同步说明。
7. **归档人物的显示缺口已补齐。** 实现过程中发现 `PersonService.list` 没有列出已归档人物的途径，导致关系图的"显示已归档人物"只能画出被关系引用的归档人物、时间线的相关人物列对归档参与者只能显示短 id。现补充 `PersonService.list(..., include_deleted=False)` 作为管理／审计视图（默认行为不变），关系图与时间线改用它，因此**没有任何关系的已归档人物也能画出来**、归档参与者也显示其记录姓名；另有专门测试覆盖这两种情况。

## 验证方式

- 搜索：真实 SQLite + 真实迁移，覆盖四类实体、跨 Workspace、软删除／恢复、`* " - AND OR NOT ( ) :` 等输入、投影被 drop／清空后的自愈重建、`limit` 与 `entity_types` 过滤、以及"投影不是事实"（清空投影后事实仍在且可重建）。
- Timeline／Dashboard：断言调用前后**所有表名与行数不变**（纯读）、统计不缓存、并用 `before_cursor_execute` 计数器证明"稀疏世界 == 稠密世界"（无 N+1：person 7／workspace 6／counts 8／snapshot 12 条 SELECT）。
- Graph：只读服务构建，断言构建前后事实表快照不变；方向箭头按 DIRECTIONALITY 区分；未来生效的状态不会被当作当前状态显示。
- Migration：`0001→…→0006` 计数 0/5/7/9/11/11（业务事实表），`0005↔0006` 往返保留 11 张事实表全部数据与约束、降级后无 `%search_document%` 对象、FTS5 可重建、注入失败后 revision 与数据不变、离线 `--sql` 在真实 SQLite 执行成功、`PRAGMA foreign_key_check` 干净。
- PostgreSQL：仅 SearchDocumentModel 与既有事实模型的 DDL compile（UUID／TIMESTAMP WITH TIME ZONE／UNIQUE／CHECK），**未实现也未声称 PostgreSQL 搜索**。

## 桌面启动

全新空临时库、原生 Windows Qt，退出码 0，同时确认空库迁移到新 head：

```text
Empty database initialized through Alembic
Database schema version verified
Application started (Phase 7)
GUI smoke test passed: window visible and 6 pages navigable; Qt platform=windows
Application stopped: 0
```

自动 UI 测试使用 offscreen；此 smoke 不等同于人工 DPI／布局评审。所有验收 fixture 都在 pytest 临时目录，没有生成示例业务数据。

## 边界

事实层仍是十一张业务表；Phase 7 只新增可重建的搜索投影基础设施。没有 Analysis／AnalysisSource／Memory／LLM／Sync／ChangeLog／Revision／OCR，也没有自动摘要、自动关系判断或向量数据库；Graph 不推断关系，Timeline 不修改事实，搜索不上传数据。未宣称完成真实 PostgreSQL 搜索、其他 OS、完整 backup／恢复或安装包分发。

**完成 Phase 7 后停止。**
