# Social Map / Relationship Intelligence System — 长期项目规则

本地优先（local-first）的人际关系情报工作台：PySide6 桌面应用，SQLite + SQLAlchemy + Alembic，
单用户、不联网、不同步、无账号。仓库为本地 git 工作区，**不上传**。

## 当前阶段

Phase 1–10 已完成（功能、投影层、分析层、用户确认 Memory、稳定化）。
**Phase 11（V1 UI/UX / Interaction Hardening）已实现并验收**，见
[Phase 11 验收记录](<docs/phase11-verification.md>)。

下一步**不会自动开始** Revision / ChangeLog / Sync；任何新阶段都必须由用户明确指派。

## 架构边界（不可跨越）

```text
UI（Domain 值 / DTO / Service）
  ↓
Application / Service（业务规则、Workspace 作用域、原子事务）
  ↓
Domain / Repository Interface（stdlib，不依赖 Qt／SQLAlchemy）
  ↓
SQLAlchemy Repository Adapter → SQLite / Local MediaStore
```

- **事实层**：Workspace、Person、PersonAlias、PersonMedia、Media、Relationship、
  RelationshipHistory、Event、EventParticipant、Evidence、EvidenceMedia（11 张业务表）。
- **投影层**：Search（FTS5，可重建）、Timeline、Graph、Dashboard——不落表，只从事实层重算。
- **分析层**：AnalysisRun + 4 张 `analysis_*_source`。模型输出**永远不是事实**。
- **记忆层**：Memory 是**用户确认过的长期语义**，不是事实链中的下一层。

硬性规则：

1. 不新增 Alembic migration、不改业务 schema、不改 Domain 语义——除非用户明确批准。
2. 不实现 Revision / ChangeLog / Sync / OCR / RAG / 向量库 / 自动 Memory / 自动关系判断。
3. 所有模型调用必须由用户显式点击触发；不自动审计、不自动备份、不上传。
4. 不做动画（过渡、粒子、节点呼吸、流光、动态连线）；Analysis 的耗时文字计数例外。
5. UI 只能通过 Service 访问数据；页面不得直接构造 SQL 或 ORM 对象。

## 冲突与并发

- 统一走 `app/ui/operations.py` 的 `run_operation`，不得另建错误边界。
- `ConflictError` 显示 Service 提供的安全用户消息；**不按错误文本关键词分类**。
- 当前 stale snapshot 保护只覆盖已有 expected-state guard 的操作：
  Evidence 附件、Event ↔ RelationshipHistory。其余 metadata 更新是 last-write-wins。
  Phase 11 不引入 `expected_updated_at`，不重构 `ConflictError` taxonomy。

## UI 约定（Phase 11 起生效）

- Design System 唯一来源：`app/ui/design/`（tokens / fonts / qss / icons / widgets / theme）。
  页面**不得**自带颜色字面量或 `setStyleSheet`（有测试扫描强制）。
- 单一 `WorkspaceContext` 是 Current Workspace 的唯一真相；页面内 `workspace_combo` 只是兼容代理。
- 切换 Workspace 必须走 `about_to_change` → `reset_context()` → `changed` 协议，
  不得让旧 Workspace 的详情、选中或搜索结果残留。
- 危险操作（删除 / 归档 / 归档 Workspace）必须二次确认。
- 界面不显示开发阶段编号（PHASE N）。

## 测试与验收

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

- **必须带 `-s`**：用户级 PySide6 与 conda Qt 会冲突，导致 `DLL load failed`。
- 当前基线：**1483 collected / 1478 passed / 5 environment-skipped / 0 failed**。
- 5 项 skipped 是 `tests/storage/test_media_store.py` 的
  「OS does not grant temporary-test symlink creation」（Windows symlink 权限）。
  **不得删除、不得弱化、不得描述为通过。**
- 自动 UI 测试使用 offscreen；真实 Windows DPI 人工验收必须在验收记录中如实标注，
  未执行就写「未检查」，不得宣称通过。

## 文档约定

- `docs/phase3..phase10-verification.md` 是**历史记录，不修改**，也不得因文档更新改动其测试数字。
- 现行文档：`README.md`、`docs/architecture.md`、`docs/database.md`、`docs/projection.md`、
  `docs/analysis.md`、`docs/memory.md`、`docs/operations.md`、`docs/relationships.md`、
  `docs/events.md`、`docs/evidence.md`、`docs/media.md`。
- 每个阶段的验收记录单独成文件，写清 collected / passed / skipped / failed 与实际未完成项。
