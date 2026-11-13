# Relationship Intelligence System

本地优先的人际关系知识库。当前实现 **Phase 10 — Stabilization / Data Integrity / Backup / Export**：在 Phase 1–9 的完整功能之上做收紧与保护（只读完整性审计、一致性备份与恢复校验、Workspace 导出、Settings 数据管理），**不新增业务功能、不新增 migration**。

## 当前可用功能

- Workspace 创建、切换和 Default Workspace 幂等初始化。
- Person 创建、编辑、搜索、软归档；Alias 添加/删除。
- 头像与 Gallery、同 Workspace 媒体去重、多人物复用；原图查看与缩放。
- Person Detail 的 Relationships：添加关系、修改状态、查看历史、删除错误记录及恢复记录。
- Events：基础搜索、新建、日期／时段、FACT／OBSERVATION、多人物参与、编辑、软删除／恢复、详情。
- Person Detail 的 Events Timeline；参与人角色／备注、Archived 历史保留；显式关联／解绑 History，不自动改关系状态。
- Event Detail 的 Evidence：原始文本／链接／文件证据、来源信息、附件（复用已有 Media 或导入新文件）、metadata 编辑、删除／恢复。
- **Unified Search**：SQLite FTS5 统一搜索 Person／Alias／Event／Evidence，含中文分词、来源/摘要显示与索引重建。
- **Timeline**：Person Timeline 与 Workspace Timeline（Event + 关系状态变更，含未来与补录时间）。
- **Relationship Graph**：Person 为节点、Relationship 为边，可拖动／缩放／过滤，点击进入详情。
- **Dashboard**：真实统计（人物数、活跃关系数、7/30 天 Event 与 Evidence 数）与最近人物／Event／关系变更／Evidence。
- **Analysis**：Person／Relationship／Event 的按需分析（**用户点击才调用模型**），可选时间范围与上下文范围、预览将要发送的上下文、查看 Analysis History 与四段式结果。
- **LLM Provider**：OpenAI-compatible 配置（base_url／model／temperature／timeout／extra_headers／credential_ref）与连接测试；**API Key 只以引用形式存在，不入库、不入日志**。
- **Analysis Review**：分析结果四段展示、Sources 按 Person／Relationship／Event／Evidence 分组展开并**点击跳到真实详情**（不复制事实正文）；模型输出里的 `[TYPE:uuid]` 只作**候选引用**，必须与本次运行的来源核对通过才可跳转。
- **Memory（用户确认的长期记忆）**：Analysis → 用户复核 → 编辑文本 → **[确认保存]** 才创建；也可在 Person Detail／Workspace 手工创建。支持编辑、软删除、恢复、有效期（valid_from／valid_until）与过期过滤；**Memory 永不修改任何事实**。
- **ContextBuilder 的 Memory 开关**：`include_memories` **默认关闭**，显式开启才把当前有效的记忆发送给模型，并在上下文快照里记录 `included_memory_ids`。
- **Data Management（Settings）**：`[Run Integrity Check]` 只读审计（外键／workspace 一致性／悬空引用／媒体文件缺失与 sha256 不符／孤儿文件／Analysis 与 Memory 来源）、`[Create Backup]` 生成一致快照 + 媒体 + manifest、`[Verify Backup]` 在临时副本上校验并可被应用打开、`[Export Workspace]` 导出 JSON + 媒体（**默认不含分析原文与快照，导出不是备份**）。全部**手动触发**，没有云备份与自动上传。
- 十一张正式业务表：workspace、person、person_alias、media、person_media、relationship、relationship_history、event、event_participant、evidence、evidence_media；外加可随时重建的搜索投影（`search_document` + FTS5）、5 张分析表（analysis_run 与 4 张来源关联表）与 `memory`。

**没有 Sync / ChangeLog / Revision / MemoryRevision / Vision / OCR / RAG / 向量库或这些空表；分析层与记忆层都永不修改事实，也不会自动生成 Memory。** Settings 只读。程序不插入示例人物、图片、关系、事件或证据；Default Workspace 仅是系统元数据。

## 安装与启动

Python >=3.11，依赖约束见 [pyproject.toml](<pyproject.toml>)。Phase 10 未新增或升级依赖（备份用标准库 sqlite3 在线备份 API）。

```powershell
conda create -n relationship-intelligence python=3.12
conda activate relationship-intelligence
python -m pip install -e ".[dev]"
python -m app.main
```

本机局部虚拟环境复用 conda。用户级 PySide6 与 conda Qt 有 DLL 冲突，因此用 `-s` 禁用用户级包：

```powershell
& .\.venv\Scripts\python.exe -s -m app.main
& .\.venv\Scripts\python.exe -s -m pytest -q -rs
```

当前支持源码/可编辑安装；wheel/exe迁移资源分发尚未完成。

## 使用人物与媒体

进入 People，选择/创建 Workspace；创建或编辑人物，可同时选择头像；详情内管理 Alias、头像和 Gallery。删除图片引用只解绑；归档人物使用 soft delete，图片文件保留。

新建人物＋头像的DB部分一笔事务；Gallery多选按每张图片独立事务处理。原图不可变，显示缩放不修改文件。导入在当前UI线程执行，大图校验期间可能等待；异步进度UI未实现。

## 使用关系

1. 在当前 Workspace 至少创建两个人物。
2. 选择人物，向下滚动详情到 **Relationships**，点击“添加关系”。
3. 选择对方、非空类型和方向；无向会规范化，方向可选本人→对方或对方→本人。
4. “修改状态”只追加 ACTIVE / PAUSED / ENDED History。时间留空为当前UTC；显式时间必须包含时区，例如 `2026-10-03T18:00:00+08:00`。
5. “查看 History”显示实际生效时间、状态、备注、写入时间及UUID，按确定顺序排列。
6. “删除错误记录”仅soft delete；勾选显示已删除记录后可“恢复记录（保持状态）”。

**身份不可改：** Workspace、两个端点、relationship_type、directionality创建后不编辑。类型trim后1–200字符且大小写敏感。录入错误请删除错误关系，创建不同的正确身份；同身份即使已删除也不能创建第二条，必须恢复原记录。

**ENDED ≠ 删除。** ENDED后追加ACTIVE复用同一个Relationship id；恢复删除记录不自动变成ACTIVE，也不自动增加History。结束与恢复状态都通过明确追加历史表达。

当前状态不是数据库第二份字段：从 `effective_at <= 查询时刻` 的最新History派生，按 `(effective_at, created_at, id)` 决胜。未来记录不会提前生效；界面使用“刷新状态”重新查询。尚无已生效History显示“尚未生效”。

有归档端点的关系默认隐藏；可查看其历史，但不能新增状态或恢复关系。Person归档不自动结束关系，不删History，也不改原有媒体补偿逻辑。

## 使用证据（Evidence）

在 **Events** 中选中事件，详情下方是 **Evidence** 区。Event 回答"发生了什么"，Evidence 回答"凭什么记录它"。

1. **新建证据**：选择 kind（TEXT／IMAGE／FILE／LINK／MANUAL／OTHER）与来源类型，填写标题；TEXT 必须填原始内容，LINK 必须填来源引用，IMAGE／FILE 必须至少附加一个 Media。
2. **原始内容与注释分开**：`content_text` 是原始材料（按原样保存），`annotation` 是你的说明，两者不会合并。
3. **附件**：可附加当前 Workspace 已有 Media，或导入新文件后再附加（导入仍走 Phase 3 的媒体补偿规则）。同一 Media 可被多个 Evidence 复用；移除引用只解绑，不删文件。
4. **只能改 title 与 annotation**：`content_text`、`kind`、`source_type`、`source_reference`、`captured_at` 创建后不可修改。原始记录录错时请**软删除后新建**，不要覆盖。
5. **删除／恢复**：Evidence 软删除不删 Media、不改 Event 和关系状态；恢复只清 `deleted_at`。
6. 事件被软删除时，其 Evidence 默认隐藏，勾选"显示已删除"可见；恢复事件后自动重新可见。

详见 [证据设计](<docs/evidence.md>)。

## 使用事件与 Timeline

进入 **Events**，明确选择 Workspace。新建事件时填写标题、类型和 FACT／OBSERVATION，日期（`YYYY-MM-DD`）与包含时区的开始时间严格二选一；结束时间可空，不能早于开始。勾选多个人物，可填写角色／备注。

选中事件后可编辑、添加／移除参与人、软删除、显示已删除并恢复。Person Detail 的 **Events Timeline** 按发生时间最新在前；点击可操作详情，编辑、参与人和 History 关联操作后会刷新。

Archived 人物不能新增参与；已有历史参与记录保留并标记 Archived。Event 删除／恢复不删参与人或历史引用，不改变关系状态。**共同参与不等于关系 ACTIVE。**

“关联既有 History”明确选择同 Workspace 历史；“解除所选 History 关联”清空 `event_id`。用户确认 `event_id` 是 append-only History 的唯一受控关联元数据例外：原状态／备注／生效时间不可改，普通 ORM 更新及删除仍拒绝，不创建新 History。详见 [事件设计](<docs/events.md>)。

## 配置与数据目录

[配置示例](<config.example.toml>) 可复制为根目录的 `config.toml`，或传：

```powershell
python -m app.main --config C:\config\runtime.toml
```

```toml
[storage]
data_dir = ""
database = "database.sqlite3"
busy_timeout_ms = 5000
```

空data_dir使用platformdirs用户数据目录，Windows通常为 `%LOCALAPPDATA%\Relationship Intelligence System`。相对路径以TOML所在目录为基准，不以cwd为基准。

```text
<数据根目录>/
  database.sqlite3
  media/.staging/
  media/.locks/
  media/<workspace_uuid>/<hash_prefix>/<sha256>
  logs/application.log
  logs/error.log
  cache/
  backups/
```

正式数据不在源码；媒体路径只存相对POSIX路径。技术日志不序列化人物或历史备注、关系类型等私人内容。显式日志目录仍按原规则解析。

## 已有数据库升级

旧库不会静默升级。升级前关闭操作该目录的程序，手工保留整个数据根目录副本；**当前程序没有实现backup/恢复功能**。

```powershell
& .\.venv\Scripts\python.exe -s -m alembic upgrade head
& .\.venv\Scripts\python.exe -s -m alembic current
& .\.venv\Scripts\python.exe -s -m app.main
```

最新head是 `0008_memory`。自定义配置必须与应用一致：

```powershell
python -m alembic -x config=C:\config\runtime.toml upgrade head
```

外部cwd指定 `-c <项目绝对路径>\alembic.ini`。仅看到Engine ready INFO不能证明版本验证已通过；旧版本会随后明确拒绝启动并要求迁移。

**降级0006到0005会删除搜索投影（`search_document` + FTS5 索引，可重建，不丢事实）；降级0005到0004会删除 Evidence／EvidenceMedia；降级0004到0003还会删除 Event／Participant 与 event_id 引用；降级0003到0002会删除关系与历史；降到0001还会删除原五表！** 降级不是备份恢复，仅在临时测试库演练：

```text
0001 → 0002 → 0003 → 0004 → 0005 → 0006
0005 → 0006 → 0005 → 0006
0004 → 0005 → 0004 → 0005
0003 → 0004 → 0003 → 0004
```

旧0001–0005迁移未修改。0004新增两表并用SQLite-compatible batch扩展History；0005新增证据两表；0006只新增可重建的搜索投影，没有初始人物、事件或证据。

**注意：**删除 FTS5 虚表时 SQLite 需要可写的临时目录，否则会报 `SQLITE_CANTOPEN`。若宿主限制系统临时目录（本仓库的开发环境就是如此），请在执行 Alembic 前把 `SQLITE_TMPDIR`／`TMPDIR`／`TEMP`／`TMP` 指向可写目录（测试由 [conftest.py](<conftest.py>) 自动处理）。

## 分层与文件

```text
app/domain/entities/event*.py               纯UUID/UTC/date实体，无ORM/Qt
app/domain/entities/evidence*.py            纯UUID/UTC证据实体，原始内容只读
app/repositories/evidence_interfaces.py     Evidence与EvidenceMedia持久化Protocol
app/storage/models.py                       十一模型与数据库约束
app/storage/evidence_repositories.py        映射、scope、metadata-only更新
app/services/evidence.py                    类型规则、附件快照冲突与原子事务
app/ui/events_page.py                       普通Qt列表、详情、证据与明确操作
app/ui/evidence_panel.py                    Event Detail 证据区
app/ui/evidence_dialog.py                   新建/编辑metadata与Evidence Detail
alembic/versions/0006_search_projection.py  可重建搜索投影（基础设施，非事实表）
alembic/versions/0007_analysis.py           分析层 5 张表（analysis_run + 4 张来源表）
alembic/versions/0008_memory.py             用户确认的长期记忆（memory）—— Phase 10 未新增 migration
app/services/integrity.py                   只读完整性审计（外键 / 一致性 / 媒体文件与 hash / 来源）
app/services/backup.py                      一致快照备份 + manifest + 恢复校验
app/services/export.py                      Workspace 导出（不是备份；默认不含分析原文与快照）
app/ui/settings_page.py                     Data Management：四个显式手动操作
app/ui/operations.py                        唯一的 UI 操作/错误边界（Phase 10 合并 21 份副本）
app/storage/mapping.py                      唯一的行↔Domain 映射辅助
app/services/memory.py                      MemoryService：确认闸门、有效期、候选引用校验
app/storage/memory_repositories.py          软删除与"当前有效"查询
app/ui/memory_panel.py                      Person／Workspace 记忆面板
app/services/llm/                           Provider、传输层、凭据引用、配置与网关
app/services/context.py                     ContextBuilder 与 ContextSnapshot
app/services/prompt.py                      System／Context／User 三段提示词
app/services/analysis.py                    AnalysisRun 编排（事实层只读）
app/storage/search_projection.py            FTS5投影存储、中文分词与查询构造
app/services/search.py                      统一搜索Service与SearchResult DTO
app/services/timeline.py                    Timeline投影（Event + 关系变更）
app/services/dashboard.py                   Dashboard统计与最近列表（纯查询）
app/ui/graph_page.py                        Relationship Graph（QGraphicsView，无动画）
app/ui/dashboard_page.py                    真实数据Dashboard
app/ui/search_panel.py                      统一搜索框与结果跳转
```

UI → Service → Domain/Repository Interface → Storage Adapter。Repository不创建Engine/Session、不commit/rollback。Service写操作统一BEGIN IMMEDIATE、每操作独立Session。ORM guard保护身份和History不可变，但管理员raw SQL/Core bulk仍可绕过，不能将它声称为数据库触发器级保护。

详见 [数据管理（审计／备份／导出）](<docs/operations.md>)、[Memory 与 Review](<docs/memory.md>)、[分析层设计](<docs/analysis.md>)、[投影层设计](<docs/projection.md>)、[证据设计](<docs/evidence.md>)、[事件设计](<docs/events.md>)、[关系设计](<docs/relationships.md>)、[架构](<docs/architecture.md>)、[数据库](<docs/database.md>) 和 [媒体一致性](<docs/media.md>)。

## 验证与边界

```powershell
python -m pytest -q -rs
python -m pytest tests/domain tests/repositories tests/services tests/ui -q
python -m app.main --config C:\temporary\runtime.toml --smoke-test
```

Phase 4 历史基线 **662项：657 passed、5 environment-skipped、0 failed**，见 [Phase4验收记录](<docs/phase4-verification.md>)；Phase 5 见 [Phase5验收记录](<docs/phase5-verification.md>)；Phase 6 见 [Phase6验收记录](<docs/phase6-verification.md>)；Phase 7 见 [Phase7验收记录](<docs/phase7-verification.md>)。Phase 8 见 [Phase8验收记录](<docs/phase8-verification.md>)。Phase 9 见 [Phase9验收记录](<docs/phase9-verification.md>)。Phase 10 最终全量回归 **1378项：1373 passed、5 environment-skipped、0 failed，退出码0**，含只读审计、WAL 一致快照、恢复校验、导出隐私与 Settings 手动操作，并给出精简验收报告（合并 21 份 UI 操作边界与 5 份存储映射，未改 schema／未新增 migration／未改 Domain 语义），见 [Phase10验收记录](<docs/phase10-verification.md>)。5项 Windows symlink 权限 skipped 保留原测试与原因，不计为 passed。

**已知运行前提：** SQLite 删除 FTS5 虚表需要可写的临时目录；受限宿主（本仓库开发环境）必须把 `SQLITE_TMPDIR`／`TMPDIR`／`TEMP`／`TMP` 指向可写目录后再执行迁移，否则会得到 `SQLITE_CANTOPEN`。测试由 `conftest.py` 自动处理。

测试全部使用临时库和fixture，不是应用演示数据。本机受限环境无法写入用户临时目录，`conftest.py` 会把测试临时根固定到仓库内被忽略的 `.test-runs/`，并按保留策略自动清理；需要手工清空时：

```powershell
Remove-Item -Recurse -Force .test-runs, .pytest_cache, __pycache__ -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
```

未宣称完成实际PostgreSQL服务器、其他OS、人工DPI评审、完整backup/GC/崩溃恢复或安装包分发。

**本次停止在Phase10，不自动进入 Sync／Revision／ChangeLog／RAG／云／多用户。**
