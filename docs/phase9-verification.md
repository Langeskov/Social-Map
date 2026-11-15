# Phase 9 验收记录

## 最终结果

最终执行 Phase 1–9 全量回归，退出码 **0**：

| 收集项 | passed | environment-skipped | failed |
| ---: | ---: | ---: | ---: |
| **1304** | **1299** | **5** | **0** |

Phase 5/6/7/8 的结果见各自验收记录；本次保留全部旧业务测试，只把 head／表集合断言更新到 `0008_memory`，并把 `memory` 计入"完整 schema"、不计入 11 张业务事实表。**没有删除、弱化或改写任何断言，5 项 symlink 跳过原样保留。**

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

真实跳过原文：

```text
SKIPPED [5] tests\storage\test_media_store.py:275:
OS does not grant temporary-test symlink creation
1299 passed, 5 skipped
```

## 交付范围

- **Memory Domain**：[memory.py](<../app/domain/entities/memory.py>)（`Memory` 与 `CandidateReference`，含 kind／来源类型／有效期全部不变量）。
- **Memory 存储**：[memory_repositories.py](<../app/storage/memory_repositories.py>)（软删除、有效期过滤、"当前有效"查询、provenance 只写一次）。
- **MemoryService**：[memory.py](<../app/services/memory.py>)（手工创建、从分析创建需显式确认、编辑、软删除／恢复、候选引用校验）。
- **ContextBuilder memory 开关**：[context.py](<../app/services/context.py>)（`include_memories` 默认 False、`selected_memory_ids`、`scope["included_memory_ids"]`）。
- **Migration 0008**：[0008_memory.py](<../alembic/versions/0008_memory.py>)（只新增 `memory` 一张表）。
- **UI**：[analysis_page.py](<../app/ui/analysis_page.py>)（Sources 四组展开与跳转、候选引用、Save as Memory）、[analysis_dialog.py](<../app/ui/analysis_dialog.py>)（确认对话框、分析选项的 include_memories）、[memory_panel.py](<../app/ui/memory_panel.py>)（记忆列表／编辑／删除／恢复、Workspace 记忆对话框），以及 Person Detail 的记忆面板。
- 设计文档：[Memory 与 Review](<memory.md>)；[架构](<architecture.md>)、[数据库](<database.md>)、[README](<../README.md>) 已更新。

## 规格测试项对照

| 规格条目 | 覆盖 |
| --- | --- |
| Analysis Review 1–5（Source 可跳转、Workspace 隔离、目标存在、目标属于 context、无效模型引用不可信化） | [tests/services/test_memory_service.py](<../tests/services/test_memory_service.py>)、[tests/ui/test_memory_ui.py](<../tests/ui/test_memory_ui.py>) |
| Memory 6–23（MANUAL／ANALYSIS 创建、confirmed_at、取消不创建、内容可编辑、来源分析不变、软删除、恢复、valid_from、valid_until、过期过滤、人物级、Workspace 级、跨 Workspace 拒绝、分析 FK、人物 FK、分析不受影响、不修改任何事实） | 同上两个文件 |
| Context 24–28（默认不含、显式包含、快照记录 included_memory_ids、已删除不入、已过期不入） | [tests/services/test_memory_service.py](<../tests/services/test_memory_service.py>) |
| Migration 29–33（0007→0008、0008→0007、完整 schema 与事实层仍 11 张、分析数据保持、事实数据保持） | [tests/migrations/test_memory_schema.py](<../tests/migrations/test_memory_schema.py>) |
| 完整回归 Phase 1–8 | 1299 passed 全量回归 |

## 关键实现决定与实测结论

1. **确认是硬性闸门。** `create_from_analysis(..., confirmed=True)` 的 `confirmed` 必须是字面 `True`，否则直接 `DomainError`；UI 测试断言"打开对话框后 `list_all()` 仍为空"、"点取消仍为空"、"点确认才出现一条"。`content` 由用户提供并可在对话框中编辑，Service **绝不**自动复制 `raw_result`。
2. **来源可追溯但不复制事实。** Review 的 Sources 列表只显示名称／短 id／说明，点击跳到真实详情（Evidence 跳到其所属 Event 并选中该证据）。测试断言列表里不出现证据原文。
3. **模型引用只作候选。** `candidate_references()` 把 `[TYPE:uuid]` 解析出来，但必须同时满足"是合法 UUID""属于本次运行记录的来源""对象仍存在且同 Workspace"三条才标记 `accepted`；测试用伪造 UUID 与**不属于本次来源**的真实 UUID 验证它们被拒绝且不可跳转，并断言解析过程不写任何数据。
4. **provenance 只写一次。** `UPDATE` 路径显式拒绝修改 `source_type`／`source_analysis_id`／`confirmed_at`／`created_at`，编辑只改内容、kind、人物绑定与有效期并更新 `updated_at`。
5. **有效期语义**：`valid_until` 为**排他**上界（等于 `now` 即视为过期），`valid_from` 为包含下界；两者可空。过期与尚未生效的记忆都保留在库中，`include_expired=True` 可查看。
6. **Memory 绝不进入默认分析。** `include_memories` 默认 False；即使显式指定 `selected_memory_ids`，已删除／已过期／尚未生效的记忆也不会被加入上下文；`scope["included_memory_ids"]` 记录实际使用的记忆 id。
7. **不建 content 唯一约束**：同一人物可以同时拥有"张三喜欢摄影""张三喜欢摄影活动"以及重复措辞的记忆，测试显式覆盖。
8. **隐私**：`caplog` 断言记忆内容不出现在日志（创建、编辑、删除、恢复、kind 非法报错全路径），记忆内容也不出现在对话框标题里。

## 验证方式

- 真实 SQLite + 真实迁移 + 真实服务；`0007 → 0008 → 0007 → 0008` 往返、离线 `--sql` 在真实 SQLite 执行、`PRAGMA foreign_key_check` 干净、非法 kind／source_type／空内容／超长内容／反向有效期／跨 Workspace 引用全部被真实拒绝（CHECK 与复合 FK）。
- 「Memory 不修改任何事实」用 Core select 逐行列值快照断言（创建、编辑、删除、恢复、由分析创建全部路径）。
- UI 测试使用 `FakeTransport`，**没有任何真实网络请求**；断言打开页面、展开 Sources、查看候选都不触发模型调用。

## 桌面启动

全新空临时库、原生 Windows Qt，退出码 0（6 页导航，日志显示 Phase 9）。自动 UI 测试使用 offscreen；此 smoke 不等同于人工 DPI／布局评审。所有验收 fixture 都在 pytest 临时目录，没有生成示例业务数据。

## 边界

没有 Sync、ChangeLog、Revision、MemoryRevision、Vision、OCR、RAG、向量库、embedding、相似度搜索、自动 Memory、自动整合、自动事实修改或自动关系修改，也没有这些表。Memory 的 content 编辑会覆盖旧文本（无历史版本），这是本阶段明确接受的取舍。

**完成 Phase 9 后停止。**
