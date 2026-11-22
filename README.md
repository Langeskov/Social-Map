# Relationship Intelligence System

本地优先的人际关系知识库：PySide6 桌面应用，SQLite + SQLAlchemy + Alembic，单用户、不联网、不同步、无账号。**所有模型调用必须由用户显式触发，模型输出永远不是事实。**

当前状态：**Phase 1–13 完成**。Phase 13（Analysis Quality & Evaluation）交付结构化结果契约、来源曝光契约与引用验证、可复现运行遥测、模型评估与对比；Phase 12（人物调查工作台）与 Phase 10 稳定性能力保持不变。最新全量回归：**1651 collected / 1646 passed / 5 environment-skipped / 0 failed（退出码 0）**。

## 核心原则

- **事实层**（11 张业务表）只由用户录入与编辑；**Memory** 是用户确认过的长期语义（USER）；模型输出/推断/派生（DERIVED）永不写回事实。
- 无 Sync / ChangeLog / Revision / OCR / RAG / 向量库 / 自动 Memory / 自动关系判断；程序不插入示例数据。
- 模型调用由用户点击触发；`include_memories` 默认关闭；分析、评估、审计都不修改事实。
- 单一 Workspace 上下文与 Context Reset；界面不显示开发阶段编号；无动画。

## 功能概览

- **人物与媒体**：人物/别名 CRUD、软归档、头像与 Gallery、Workspace 内媒体去重、原图只读。
- **关系**：身份（端点/类型/方向）创建后不可改；状态变化只追加 History；ENDED ≠ 删除；删除仅软删除可恢复。
- **事件与证据**：Event 记 FACT／OBSERVATION 与参与人；Evidence 原始内容只读、注释可编辑；三者均可软删除/恢复，恢复不级联改关系。
- **投影**：统一搜索（FTS5 + 中文分词）、Person Activity 时间线、Workspace Timeline、Relationship Graph（无动画）、Dashboard 真实统计。
- **人物调查工作台（Phase 12）**：People 页左索引 / 中档案 + Ego Network / 右 Inspector（关系、Activity、Evidence、Memory、Analysis），归档人物只读契约。
- **分析（Phase 8–13）**：用户按需触发 Person/Relationship/Event 分析，可预览上下文；结果为 schema 1 结构化 JSON（facts / observations / interpretations / uncertainties），引用 `[TYPE:uuid]` 必须与本次运行来源核对（九种状态：`VALID_FULL`、`VALID_LIMITED`、`INVALID_FORMAT`、`UNKNOWN_SOURCE`、`NOT_IN_RUN`、`WORKSPACE_MISMATCH`、`TYPE_MISMATCH`、`DELETED_SOURCE`、`SOURCE_NOT_EXPOSED`）；运行遥测记录 Prompt/上下文/请求哈希、请求与返回模型、usage、finish reason、重试与耗时；**长度截断/内容过滤显式警告，不当作正常完成**。
- **LLM Provider**：OpenAI-compatible 配置（base_url / model / temperature / timeout / extra_headers / credential_ref）与连接测试；API Key 只以环境变量引用存在，不入库、不入日志。
- **Memory**：Analysis → 用户复核 → 编辑 → 确认保存；支持有效期、软删除/恢复；候选引用状态化展示；永不修改事实。
- **数据管理（Settings，全部手动）**：只读完整性审计、一致快照备份、备份校验、Workspace 导出（导出不是备份，默认不含分析原文与快照）。
- **评估（Phase 13）**：`python -m app.evaluation` 三种显式模式——离线数据集评估、离线固定答案模型对比（`--models`）、显式在线单模型评估（`--online --provider` 且需 `--confirm-online`）；报告写入本地 `evaluation-runs/<run-id>/`（已 gitignore），分自动/观测/人工三类指标，**不是排行榜、无单一总分、LLM Judge 不是裁判**。

## 架构

```text
UI（PySide6，仅经 Service/DTO 访问数据）
  ↓
Application / Service（业务规则、Workspace 作用域、原子事务）
  ↓
Domain / Repository Interface（stdlib，不依赖 Qt / SQLAlchemy）
  ↓
SQLAlchemy Repository Adapter → SQLite / Local MediaStore
```

- 11 张业务表 + 可重建搜索投影（FTS5）+ 分析层 5 张表（`analysis_run` + 4 张来源表）+ `memory`。
- 写操作统一 `BEGIN IMMEDIATE`、每操作独立 Session；UI 错误边界唯一入口 `app/ui/operations.py`。
- 关键目录：`app/domain/`、`app/services/`（context / prompt / analysis / analysis_result_parser / reference_verification / analysis_telemetry / memory / integrity / backup / export）、`app/storage/`、`app/ui/`、`app/evaluation/`、`alembic/versions/`。

详见 [架构](<docs/architecture.md>)、[数据库](<docs/database.md>)。

## 安装与运行

Python >= 3.11，依赖见 [pyproject.toml](<pyproject.toml>)（Phase 10–13 未新增依赖）。

```powershell
conda create -n relationship-intelligence python=3.12
conda activate relationship-intelligence
python -m pip install -e ".[dev]"
python -m app.main
```

本机 conda Qt 与用户级 PySide6 有 DLL 冲突，务必用 `-s` 禁用用户级包：

```powershell
& .\.venv\Scripts\python.exe -s -m app.main
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

当前支持源码/可编辑安装；wheel/安装包分发尚未完成。

## 配置与数据目录

复制 [config.example.toml](<config.example.toml>) 为根目录 `config.toml`，或 `python -m app.main --config <path>`。空 `data_dir` 使用 platformdirs 用户数据目录；相对路径以 TOML 所在目录为基准。

```text
<数据根目录>/
  database.sqlite3
  media/<workspace_uuid>/<hash_prefix>/<sha256>
  logs/application.log
  logs/error.log
  cache/
  backups/
```

正式数据不在源码；媒体只存相对 POSIX 路径；技术日志不序列化人物、备注、关系类型等私人内容。

## 数据库升级

旧库不会静默升级：升级前关闭程序并用 Settings 的 `[Create Backup]`（或整个数据根目录副本）留底，升级本身不是备份。当前 head 为 `0008_memory`；**Phase 13 无新 migration、无 schema 变更**。

```powershell
& .\.venv\Scripts\python.exe -s -m alembic upgrade head
& .\.venv\Scripts\python.exe -s -m alembic current
```

降级会删除表/投影（例如降到 0001 删除原五表），不是备份恢复，仅在临时测试库演练。受限宿主需把 `SQLITE_TMPDIR`／`TMPDIR`／`TEMP`／`TMP` 指向可写目录后再删 FTS5 虚表（测试由 `conftest.py` 自动处理）。

## 测试与验收

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

- **当前基线：1651 collected / 1646 passed / 5 environment-skipped / 0 failed（退出码 0）**（Phase 13.4 末）。
- 5 项 skipped 是 Windows 临时测试 symlink 权限（`tests/storage/test_media_store.py`），保留原测试与原因，**不计为 passed**。
- 既有阶段基线：Phase 10 `1373 passed`、Phase 11 `1478 passed`、Phase 12 `1511 passed`；详见各阶段验收记录。
- 真实 Windows DPI 人工验收在 Phase 11/12 记录中标注为「未检查」，不记为通过。
- 测试全部使用 `.test-runs/` 内临时库，不产生演示数据；手工清理：

```powershell
Remove-Item -Recurse -Force .test-runs, .pytest_cache -ErrorAction SilentlyContinue
```

## 文档索引

- 设计与领域：[架构](<docs/architecture.md>)、[数据库](<docs/database.md>)、[投影层](<docs/projection.md>)、[分析层](<docs/analysis.md>)、[Memory](<docs/memory.md>)、[数据管理](<docs/operations.md>)、[证据](<docs/evidence.md>)、[事件](<docs/events.md>)、[关系](<docs/relationships.md>)、[媒体](<docs/media.md>)
- Phase 记录：[Phase 10](<docs/phase10-verification.md>)、[Phase 11](<docs/phase11-verification.md>)、[Phase 12](<docs/phase12-verification.md>)、[Phase 13 评估设计](<docs/phase13-evaluation.md>)、[Phase 13 验收](<docs/phase13-verification.md>)

## 边界与已知局限

- 不做 Revision / ChangeLog / Sync / 云 / 多用户 / OCR / RAG / 向量库 / 自动 Memory / 自动模型调用。
- 分析层、评估层只读事实；评估产物只存本地 `evaluation-runs/`，不进入仓库、不写生产分析历史。
- 自动评估指标只判断结构与引用合法性；语义质量仍需人工复核（报告显式列出复核清单）。
- 尚未宣称：wheel/exe 分发、真实 Windows DPI 人工验收、安装包、完整 backup/GC/崩溃恢复。
- 外部管理员 raw SQL/Core bulk 仍可绕过 ORM 保护；不宣称触发器级不可变。

## 下一步

Phase 13 已完成，不会自动进入下一阶段。后续工作（如独立 Evaluation Viewer、人工复核结果采集、Judge 实验记录、Revision 等）需用户明确指派。代码已发布到用户自有 GitHub 仓库（origin: Langeskov/Social-Map）；仓库无自动同步、无 CI。
