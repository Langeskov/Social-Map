# Phase 12 — Person Investigation Console 验收记录

Phase 12 把既有能力组织成人物调查工作台（Person Investigation Console）。**无 schema 变化**：
未新增 migration、未新增业务表、未改 Domain 语义、未改 Phase 3 归档语义与媒体补偿规则、无动画。

## 0. 子阶段与退出条件

| 子阶段 | 范围 | 状态 |
|---|---|---|
| 12.0 | UX Contract & Layout Specification（审查 + 设计，不改代码） | 完成（方案见 [phase12-design](<phase12-design.md>)） |
| 12.1 | Person Investigation Workspace（三区布局、Dossier、Ego Network） | 完成 |
| 12.2 | Activity & Evidence Inspector（选中联动、阅读器、跳转、时间线批量读） | 完成 |
| 12.3 | Workspace Navigation & Cleanup（时间线入口、隐藏组件移除、归档契约、分析锁定） | 完成 |
| 12.4 | V1 Verification & Release Readiness（探针、文档、测试质量） | 实现完成；**发布验收待完成**（见 §9） |
| 12.5 | Archive Read-only Consistency & Release Closure（归档历史读取、状态矩阵、文档收尾） | 完成（见 §9b） |

## 1. Git 基线与提交历史

- 本地仓库（**无 remote**，不推送）。开发分支 `phase-12-investigation-console`；
  lint 清理为独立提交并另设分支指针 `chore/lint-cleanup`。
- 基线 Tag：**`phase-11-baseline` → `8bb12ad`**（Phase 11 验收记录提交）。
- 旧远端基线 `f11bc31…` **不可获取**（本地无此对象，与当前历史无共同祖先）——如实记录，
  不虚构连续历史。
- 未执行 git init / force reset / force push / 合并无关历史；`AGENTS.md` 已被跟踪且未改写。

```text
Phase 12 提交链（自基线起 13+ 提交，均为独立小步）：
93bdf4f chore: drop dead imports and unreachable code flagged by lint
ac47f61 refactor(ui): extract shared graph canvas for global and ego graphs
c0e52db feat(ui): person investigation workspace with ego network
f1cb03a test(ui): cover person investigation workspace
b3135fd perf(timeline): batch event and relationship reads per refresh
31d0add feat(ui): activity and evidence inspector
b91319f test(ui): cover activity and evidence navigation
7caa4ca feat(ui): show record status on activity rows and title in the reader
4349fcb feat(ui): workspace timeline entry on the events page
fa8a904 refactor(ui): remove hidden search and timeline panels
1574356 fix(service): archived person read-only detail contract
9660848 fix(ui): analysis lock until worker end and priority topbar status
b518f31 test(ui): replace dead assertions with real state checks
（12.4：布局探针 + 文档一致性 + 本记录）
```

## 2. 测试结果

命令：`.\.venv\Scripts\python.exe -s -m pytest -q -ra`（必须带 `-s`）

| 项 | Phase 11 基线 | 12.1 后 | 12.2 后 | 12.3 后 | 12.4 后 | 12.5 最终 |
|---|---|---|---|---|---|---|
| collected | 1483 | 1498 | 1510 | 1515 | 1515 | **1516** |
| passed | 1478 | 1493 | 1505 | 1510 | 1510 | **1511** |
| skipped | 5 | 5 | 5 | 5 | 5 | **5** |
| failed | 0 | 0 | 0 | 0 | 0 | **0** |

5 项 skipped 仍是 `tests\storage\test_media_store.py:275` 的「OS does not grant
temporary-test symlink creation」（Windows 符号链接权限），语义未变、未删除、未改写为通过。

## 3. 新增与迁移测试

- 新增（+32）：`tests/ui/test_phase12_workspace.py`（16：布局/Ego/选择级联/不落库/上下文清空）、
  `tests/ui/test_phase12_inspector.py`（12：Activity 联动、阅读器 FACT/USER、注释保存、
  所属 Event 追踪、完整跳转链、批量读守卫、记录状态）、`tests/services/test_workspace_person.py`
  +1 归档读取契约、`tests/ui/test_analysis_ui.py` +3 真实异步锁定。
- 迁移（等价断言，无整测试删除）：`test_dashboard_ui`（隐藏对象→TopBar 搜索/Events 时间线 Tab）、
  `test_phase7_navigation` 与 `test_phase7_panels`（页面搜索副本→全局搜索+Shell 路由）、
  `test_phase11_context`（同上 + 对冲断言改真实断言）、`test_phase11_events`（时间线宿主→
  Events 页）、`test_phase11_dashboard_search_graph`（「隐藏」→「已移除」）、
  `test_phase11_person_detail`／`test_phase12_workspace`（tab 顺序）、`test_graph_ui` 等
  （符号搬家导入）、`test_phase11_analysis_memory_settings`（`or True` 与 `if False else`
  死断言→真实状态断言）。
- 重点工作流覆盖（1–10）：选人物→Ego→开关系→Activity→事件→证据→人物上下文回归→
  Analysis Sources 跳转→切 Workspace→旧上下文清零，均有测试。

## 4. 功能与修复记录

- **调查工作台**：三区布局（Index／Dossier+Ego／Inspector 五 Tab），Inspector 侧栏⇄底部
  抽屉（显式切换 + 宽度自动切换）；1280×720 下核心控件可达（探针 + 测试断言）。
- **Ego Network**：一层直接关系，复用全局 Graph 绘制实现（`graph_canvas.py` 唯一绘图层）；
  显隐规则与 GraphPage 一致；不推断、不落库（有行数守卫测试）。
- **Inspector**：事件行→内联事件详情→该事件证据→所属 Event／Analysis Sources 完整跳转；
  关系变更行→真实 Relationship Detail（字段+历史）；Evidence 主从阅读器（FACT 只读／
  USER 注释经 Service 保存），Evidence 归属 Event 始终可追踪。
- **Workspace Timeline**：Events 页第二 Tab（共享 TimelinePanel 工作区模式），唯一可见入口；
  Dashboard 只保留统计与最近列表。
- **隐藏组件清除**：People/Dashboard 隐藏 SearchPanel、Dashboard 隐藏 workspace_timeline
  （含 `refresh()` 隐藏查询）全部**移除**；全局搜索唯一入口=TopBar。
- **归档只读契约**：`PersonService.detail(include_deleted=True)` 读身份+存留别称；归档时
  解绑的图片引用不恢复不伪造（UI 如实标注）；写入口全拒绝（Service+UI 双层测试）。
- **分析锁定**：锚定 `QThread.finished`（QueuedConnection→GUI 槽）；取消/关对话框不提前
  解锁；TopBar 状态优先级（lock 级不被页面切换覆盖）；真实异步测试覆盖成功/失败/取消。
- **缺陷修复**：进度对话框（耗时 + 运行时取消按钮）自 Phase 8 起被构建并启动 Worker 但
  **从未 show()**——12.3 修复为真实显示（7.4 的取消/关闭语义此前一直悬空）。
- **性能**：TimelinePanel 刷新由逐行查询改为批量读（1 次事件列举 + 1 次关系列举），
  行载荷契约不变；打开详情仍按需读取。
- **测试质量**：`or True`／`if False else`／`hasattr…else True` 失效断言全部清除（复查为 0）。

## 5. 布局探针（可重复，干净仓库可执行）

`tools/verification/phase12_layout_probe.py`（输出入 `.verification/phase12-layout/`，不入库）：

```powershell
& .\.venv\Scripts\python.exe -s tools\verification\phase12_layout_probe.py --matrix
& .\.venv\Scripts\python.exe -s tools\verification\phase12_layout_probe.py --scale 1.25 --sizes 1280x720
```

覆盖 1280×720 / 1920×1080 / 2560×1440 × 100% / 125% / 150% / 175% / 200%，页面含
Dashboard、People（侧栏与底部两种 Inspector 形态）、Events List、Workspace Timeline、
Graph、Analysis、Settings。检查控件裁切、横向溢出、核心操作可见性、分区尺寸与代表性
Dialog 宽度。

实跑记录（2026-11-21）：**标签裁切 0；核心操作缺失/隐藏 0；Dialog 全部适配窗口**。
35 条 advisory 发现全部为窄宽度下表格/滚动区的正常横向滚动（relationshipList、
附件表、分析引用表等），已逐类确认非缺陷。

**这不是 Windows 系统 DPI 验收**：离屏平台不加载 Windows 字体（中文可能渲染为方框，
宽度度量偏移），也不验证原生控件度量与系统缩放。

## 6. 人工 Windows DPI 验收（产品负责人执行）

| DPI | 分辨率 | 页面 | 结果 |
|---|---|---|---|
| 100% / 125% / 150% / 175% / 200% | 1280×720 | 全部核心页面 | **未检查**（待产品负责人执行） |
| 100% / 125% / 150% / 175% / 200% | 1920×1080 | 同上 | **未检查** |
| 100% / 150% | 2560×1440 | 同上 | **未检查** |

人工检查项：三栏/紧凑布局、Ego 节点与边、时间线表格、Evidence 阅读器、Settings、
Dialog、Workspace 选择器、TopBar 状态提示。**未执行不得记为通过。**

## 7. 文档一致性修正（12.4）

- `README.md`：删除「当前程序没有实现 backup/恢复功能」的自相矛盾（升级≠备份，指向
  Settings Create Backup）；补 Phase 12 能力与回归数字（12.5 末为 1516/1511/5/0）；时间线表述更新。
- `docs/media.md`：「当前 backup 尚未实现」改为 Phase 10 实现事实（保留 WAL 警示）。
- `docs/verification.md`：「备份恢复实现」移出未完成清单（指向数据管理文档）。
- `docs/phase11-verification.md`：决策对照表不再声称保留已删除的 EventTimeline（注明
  已删除+断言迁移，隐藏副本由 Phase 12.3 移除）。
- `docs/architecture.md`：新增「UI 层（Phase 12）」。
- 新增 `docs/phase12-design.md`（本方案）与本文档。
- `docs/phase3..phase10-verification.md` **未修改**（历史记录）。

## 8. 架构边界 / 数据库

- 层次结构、五层语义、Memory 独立语义、Privacy 规则均未变化；Service 仅新增只读参数。
- 数据库：**零 migration、零新表、零 Domain 语义变化**。

## 9. 未完成项与已知问题

1. **真实 Windows DPI 人工验收未执行**（§6，产品负责人执行中）——因此本阶段结论为
   **「Phase 12 实现完成，发布验收待完成」，不标记 V1 Release Ready**。
2. Windows CI 工作流按负责人决定**本轮不做**。
3. ~~归档人物的 Relationship/Timeline 面板仍弹读拒绝~~ **Phase 12.5 已解决**：归档主体的
   关系与时间线以只读历史视图展示（`include_deleted=True` 归档读取路径），不再弹拒绝提示；
   组合行为见 [关系文档的归档状态矩阵](<relationships.md>)。
4. ~~`AGENTS.md` 测试基线过时~~ **Phase 12.5 已解决**（负责人授权「版本控制说明」清理时
   一并更新基线与阶段说明）。
5. 跨 Workspace 全表扫描类查找（`_workspace_of` 等）仍是 O(n)（既有行为，未在本期改）。
6. 离屏探针的字体度量限制（§5）。

## 9b. Phase 12.5 — 归档只读一致性与验收收尾

- **12.5.1 归档人物历史读取**：`RelationshipService.list_by_person` 增加只读归档路径
  （`include_deleted=True` 可读归档主体，默认行为不变）；RelationshipPanel / Person
  Timeline / Ego Network 对归档主体展示既有历史（时间线「显示已删除记录」开关继续过滤行），
  不再弹 Service 拒绝提示；新增/状态/恢复/分析入口锁止。
- **12.5.2 归档状态矩阵**：矩阵文档化于 [relationships.md](<relationships.md>)，组合测试
  `test_archive_state_matrix_combinations` 等覆盖：归档人物、归档端点、已删除关系、
  History 记录的读取/写入组合（审计删除保留可用，恢复与状态写入永不绕过端点归档）。
- **12.5.3 文档修正**：README 旧 Settings 描述（「Settings 只读」失实）与 Phase 10 残留、
  `AGENTS.md` 版本控制说明（分支/基线 Tag/无 remote/旧基线不可获取）与测试基线更新。

## 10. 结论

Phase 12 五项最终退出条件中，除「真实 Windows DPI 人工验收记录完整」待产品负责人执行外
全部达成：调查工作台、Ego 与全局 Graph 共用绘图实现、Activity/Evidence 完整路径、
Workspace Timeline 可见入口、Dashboard 无隐藏查询、搜索单一入口、归档读取契约明确且
经测试、归档媒体解绑规则未变、分析锁定贯穿 Worker 生命周期、Context Reset 正确、
布局探针可从干净仓库执行、文档与实现一致、最终回归 1516 collected / 1511 passed /
5 environment-skipped / 0 failed、5 项环境 Skip 未伪装、Git 基线清晰。
