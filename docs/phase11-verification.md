# Phase 11 — V1 UI/UX / Interaction Hardening 验收记录

Phase 11 只改 UI 层与 UI 共享基础设施。没有新增 migration，没有改业务 schema，
没有改 Domain semantics，没有引入动画、Web 前端、Sync / Revision / ChangeLog。

- 起点基线：1378 collected / 1373 passed / 5 environment-skipped / 0 failed
- 终点基线：见本文件末尾「最终回归」

## 0. 子阶段与退出条件

| 子阶段 | 范围 | 退出条件 | 状态 |
|---|---|---|---|
| 11.1 | Design System（tokens / 字体 / QSS / SVG 图标 / 共享 widgets）+ MainWindow Shell（TopBar、稳定导航、只读 Context Bar） | Shell 就位、6 页可达、历史测试全绿 | 完成 |
| 11.2 | WorkspaceContext、Context Reset 协议、DetailWindowTracker、状态组件、Conflict UI | 7 个 stale UI 风险点全部有自动化测试 | 完成 |
| 11.3 | People / Person Detail 重排、Archived filter、Evidence / Memory / Analysis Tab | 层级顺序、空字段隐藏、归档可读 | 完成 |
| 11.4 | Event / Evidence / Timeline 重排与标记、Timeline 筛选 | 阅读顺序、FACT / USER 标记、UPCOMING / PAST | 完成 |
| 11.5 | Graph 侧栏筛选、全局 Search（Ctrl+F）、Dashboard 重排 + 最近 Analysis | 跳转全通、无跨 Workspace 泄漏、查询数不变 | 完成 |
| 11.6 | Analysis 工作台 + Workspace 锁定、Memory 分组、Settings 分区与 Workspace 管理 | 显式触发、锁定生效、归档规则成立 | 完成 |
| 11.7 | 自动渲染检查、DPI 记录表、文档、最终回归 | 记录表完整，人工项明确标注 | 完成（人工项见第 6 节，**未检查**） |

## 1. Design System 落地

- 代码位置：`app/ui/design/`（`tokens.py`、`fonts.py`、`qss.py`、`icons.py`、`widgets.py`、`theme.py`）。
- 调色板共 16 个值：中性色 7 + 主强调 1 + 选中 1 + 警告 1 + 归档/删除 1 + 层标识 5。
- 层标识：Entity(Person) / Link(Relationship) / Occurrence(Event) / Proof(Evidence) /
  Inference(Analysis)，每层一个低饱和色 + 一个 SVG 图标 + 一个中文短标签。
- 认知标识与层标识正交：FACT（实线、只读）、USER（用户产出、可编辑）、DERIVED（虚线 + DERIVED + Sources）。
- 字体：正文 `Segoe UI → Microsoft YaHei UI → Microsoft YaHei → Noto Sans CJK SC`；
  等宽 `Cascadia Mono → Consolas → Microsoft YaHei UI → Courier New`。字号用 pt（随 DPI 缩放），
  尺寸由 `QFontMetrics` 推导，不硬编码像素。
- 无动画：`QApplication.setEffectEnabled` 关闭菜单/提示渐变；样式表无 transition/animation；
  测试扫描 `app/` 全目录，禁止出现动画类。

自动测试固化的规则（`tests/ui/test_phase11_design.py`）：

- 调色板规模固定；文本/强调/层色在 `surface` 上 WCAG 对比度 ≥ 4.5，层色两两色距 ≥ 48。
- `app/ui/` 下除 `design/` 外不得出现 `#rrggbb` 字面量，也不得调用 `setStyleSheet`。
- 全 `app/` 不得出现 `QPropertyAnimation` / `QMovie` / `QGraphics*Effect` 等。
- 字体链必须含 CJK 家族；共享 widget 的 layer / epistemic / status / state 属性齐全。

## 2. Shell 与导航

- 布局：TopBar（品牌 │ Workspace 选择 │ 全局搜索 │ 状态）→ 左侧稳定导航 → Context Bar（只读面包屑）→ Page。
- 决策 1：Context Bar 只做只读面包屑，**没有** Back / Forward 历史。
- 导航宽度由最长标签的 `QFontMetrics` 推导后固定，任何窗口宽度下都不被压缩。
- 页面顺序冻结：Dashboard(0) People(1) Events(2) Graph(3) Analysis(4) Settings(5)。
- 侧栏页脚是 FACT / USER / DERIVED 图例；界面不再出现 "Phase N" 字样（有测试断言）。

## 3. Workspace Context 与隔离

- 单一 `WorkspaceContext`（`app/ui/context.py`）持有 Current Workspace，TopBar 是唯一可见选择器；
  页面内旧的 `workspace_combo` 保留为隐藏代理（对象仍可用、仍同步），以保证历史测试语义不变。
- 统一 Context Reset 协议：`about_to_change` → 各页 `reset_context()`（关闭所有非模态窗口 + 清空选中/详情/搜索）→
  `changed` → 各页 `load_workspace()`。
- 自动化覆盖（`tests/ui/test_phase11_context.py`）：人物详情、事件详情 + 证据面板、非模态关系窗口、
  搜索输入与结果、Graph 场景、Analysis 当前记录，Workspace A → B 后全部不残留。
- 搜索不跨 Workspace：结果的 `workspace_id` 与当前上下文一致；跳转会先切换上下文。

## 4. Conflict UI（决策 2，方案 A）

- 不引入 `expected_updated_at`，不重构 `ConflictError` taxonomy，UI 不按错误文本分类。
- `run_operation` 新增 `on_conflict(message)`：`ConflictError` 仍以 Service 原文弹窗（历史断言不变），
  同时把同一段安全用户消息交给页面上的 `ConflictBanner` 持久显示；冲突无文本时回退到
  「当前记录已经被其他窗口修改，请刷新后重试。」
- **明确记录：当前 stale snapshot protection 只覆盖已有 expected-state guard 的操作**
  （Evidence 附件 `expected_attachment_ids`、Event↔History `expected_event_id`）。
  Person / Event / Memory 等普通 metadata 更新仍是 last-write-wins，UI 无从检测并发修改，
  不会显示该冲突文案。

## 5. 页面 × 状态矩阵（草案已落地为组件与文案）

组件：`StateStack`（NORMAL / EMPTY / LOADING / ERROR / UNAVAILABLE 替换内容；CONFLICT / ARCHIVED /
DELETED 以横幅叠加在仍可读的内容之上）、`StateNotice`、`ConflictBanner`、`StatusPill`。

| 页面 / 区块 | Empty | Loading | Error | Conflict | Archived / Deleted | Unavailable |
|---|---|---|---|---|---|---|
| Dashboard 各列表 | 「这里还没有记录。」 | 静态「正在读取数据……」 | run_operation 文案 | — | Deleted（开关） | 「对应服务尚未装配」 |
| People 列表 | 同左 | 同左 | 同左 | — | Archived 过滤 + ARCHIVED 徽标 | 同左 |
| Person Detail | 各区块空态文案 | 同左 | 同左 | 无（无 guard） | ARCHIVED 徽标，历史可读 | 对象消失时清空 |
| Relationship | 空表 | 同左 | 同左 | 重复身份（Service 原文） | Deleted 行标记 | 同左 |
| Timeline | 空表 | 同左 | 同左 | — | Deleted 行标记 | 同左 |
| Event Detail | 清空所有区块 | 同左 | 同左 | History 关联（Service 原文） | DELETED 徽标 | 同左 |
| Evidence Detail | 「未选择事件」 | 同左 | 同左 | 附件快照（Service 原文） | Deleted 标记 | 同左 |
| Graph | 「无节点」空提示 | 同左 | 同左 | — | Archived / Deleted 过滤 | 同左 |
| Search | 「没有找到匹配记录。」 | 同左 | 同左 | — | Deleted 开关 | 「FTS5 索引不可用」 |
| Analysis | 空历史 | 运行中状态行 | 同左 | 取消已结束（Service 原文） | — | 「分析服务尚未装配」 |
| Memory | 空表 | 同左 | 同左 | — | 过期 / 已删除分组 | 同左 |
| Settings 各操作 | 「尚未运行」 | 后台 + 忙碌提示 | run_operation 文案 | — | — | 同左 |

## 6. DPI 与窗口尺寸：自动检查 vs 人工验收

### 6.1 自动渲染检查（本阶段实际执行）

工具：`.verification/phase11_layout_probe.py`，offscreen 渲染 1280×720，
`QT_SCALE_FACTOR` = 1.0 / 1.25 / 1.5 / 2.0 × 6 页；记录窗口尺寸、设备像素比、标签裁切检测和 PNG。

| 缩放 | 页面数 | 标签被裁切的页面 | 截图 |
|---|---|---|---|
| 100% | 6 | 0 | `.verification/phase11-dpi/scale1-*.png` |
| 125% | 6 | 0 | `.verification/phase11-dpi/scale1.25-*.png` |
| 150% | 6 | 0 | `.verification/phase11-dpi/scale1.5-*.png` |
| 200% | 6 | 0 | `.verification/phase11-dpi/scale2-*.png` |

该检查发现并修复了一个真实缺陷：全局搜索最初把完整 `SearchPanel` 放进 TopBar，
把顶栏撑到约 300px 并挤掉页面；已改为工具栏模式（只有输入框 + 按需出现的结果列表）。

### 6.1b 原生 Windows Qt 启动 smoke（自动化，非 DPI 验收）

`python -s -m app.main --config .verification/smoke.toml --smoke-test`（`QT_QPA_PLATFORM` 未设置）：

- 退出码 0；日志 `Qt platform=windows`、`GUI smoke test passed: window visible and 6 pages navigable`；
- 6 页导航全部可达，空库经 Alembic 初始化，无 error.log 条目。

这证明窗口在真实 Windows Qt（非 offscreen）上可创建、可导航，**不**包含 DPI／布局人工评审。

**这不是 Windows 系统 DPI 验收。** offscreen 平台不加载 Windows 字体（截图中文字为方框），
系统 DPI 缩放、原生控件度量、真实字体回退都未在此路径内被验证。

### 6.2 人工检查记录表

| DPI | 分辨率 | 页面 | 结果 | 问题 | 截图/备注 |
|---|---|---|---|---|---|
| 100% | 1280×720 | 全部 6 页 | **未检查** | — | 需在真实 Windows 上执行 |
| 125% | 1280×720 | 全部 6 页 | **未检查** | — | 同上 |
| 150% | 1280×720 | 全部 6 页 | **未检查** | — | 同上 |
| 175% | 1280×720 | 全部 6 页 | **未检查** | — | 同上 |
| 200% | 1280×720 | 全部 6 页 | **未检查** | — | 同上 |
| 100% | 1920×1080 | 全部 6 页 | **未检查** | — | 同上 |
| 125% | 1920×1080 | 全部 6 页 | **未检查** | — | 同上 |
| 150% | 1920×1080 | 全部 6 页 | **未检查** | — | 同上 |
| 175% | 1920×1080 | 全部 6 页 | **未检查** | — | 同上 |
| 200% | 1920×1080 | 全部 6 页 | **未检查** | — | 同上 |
| 100% | 2560×1440 | 全部 6 页 | **未检查** | — | 可用性确认，同上 |
| 150% | 2560×1440 | 全部 6 页 | **未检查** | — | 同上 |

人工检查项（未执行，不得记为通过）：控件不重叠、文本不裁切、表格列可用、Dialog 不超出窗口、
Graph 工具栏不遮挡、Detail 面板不塌陷、导航不被压缩、长文本可滚动、高 DPI 图标清晰。

### 6.3 实现约束

- 使用 layout 与 `QFontMetrics`，无硬编码控件尺寸；SVG 图标多尺寸 `QIcon` + devicePixelRatio 渲染。
- MainWindow 最小尺寸 800×560（决策 9：不为极端「低分辨率 + 高 DPI」组合继续下调）；
  极端组合下允许页面内部滚动。
- 已知遗留：部分历史 Dialog 仍使用 `resize(px, px)`（Phase 5–10 既有代码，未在本阶段改写）。

## 7. 本阶段发现但未擅自穿透架构的问题（按熔断规则报告）

### 7.1 归档人物的详情无法读取（Service 缺口，未修改）

- 现象：`PersonService.detail(workspace_id, person_id)` 没有 `include_deleted` 参数，软删除后抛
  `NotFoundError`；而 `PersonService.list` / `EventService.detail` / `EvidenceService.detail` 都有该参数。
- 影响：Person Detail 无法显示归档人物的别称与图片引用（关系 / Timeline / Memory / Evidence /
  Analysis 仍可读）。
- 当前 UI 处理：`load_person_detail()` 捕获 `NotFoundError`，用管理列表里的 Person 构造
  `ArchivedPersonSnapshot`，明确标注「已归档人物：别称引用需要读取归档详情，当前不可用」，不伪造数据。
- 推荐方案（需要批准，属 Service 层）：给 `PersonService.detail` 增加 `include_deleted: bool = False`，
  与 Event / Evidence 一致；不涉及 schema / migration。

### 7.2 ConflictError 语义重叠（已知，方案 A 下不处理）

`ConflictError` 同时表示 stale snapshot、身份重复、状态不允许。方案 A 决定不重构 taxonomy；
因此 UI 只显示 Service 原文，并在第 4 节记录 guard 覆盖范围。

### 7.3 Dashboard 最近分析的读取方式

`DashboardService.snapshot()` 的查询数是 Phase 7 冻结契约（≤12），直接加一条列表查询会破坏该断言。
因此新增只读方法 `DashboardService.recent_analysis(workspace_id, limit)`（决策 5 授权），
Dashboard 页只依赖 DashboardService，snapshot 的查询数保持不变。

## 8. 决策落地对照

| 决策 | 落地 |
|---|---|
| 1 只读面包屑 | `ContextBar.set_crumbs`；无 back/forward 属性（有测试断言） |
| 2 方案 A | `run_operation(on_conflict=…)` + `ConflictBanner`；文档记录 guard 范围 |
| 3 Memory 位置 | Person Detail Tab 顺序 Evidence → Memory → Analysis |
| 4 删除冗余 UI | Dashboard 内嵌搜索 / Workspace Timeline、People 的 Event-only Timeline 均不再显示（对象保留以维持历史契约） |
| 5 最近 Analysis | `DashboardService.recent_analysis`，UI 不直接调用 AnalysisService |
| 6 运行中锁定 | `AnalysisPage.workspace_lock_changed` → TopBar 选择器禁用；结束后解锁；可取消 |
| 7 Workspace 管理 | Settings 分区内 新建 / 重命名 / 归档；当前不可归档、至少保留一个、名称遵循 Service 规则 |
| 8 短 UUID | Identity Card 显示 `ID · xxxxxxxx…xxxx`，tooltip 为完整 id，纯展示 |
| 9 DPI | 自动渲染检查与人工表分开记录；人工项未检查；最小尺寸不下调 |
| 10 无 schema 变化 | 无新 migration、无业务 schema 改动、无 Domain 语义改动 |

## 9. 最终回归

命令：`.\.venv\Scripts\python.exe -s -m pytest -q -ra -p no:cacheprovider`
（必须带 `-s`，否则用户级 PySide6 与 conda Qt 冲突导致 DLL 加载失败。）

| 项 | 起点（Phase 10 基线） | 终点（Phase 11） |
|---|---|---|
| collected | 1378 | **1470** |
| passed | 1373 | **1465** |
| skipped | 5 | **5** |
| failed | 0 | **0** |

5 项 skipped 仍是 `tests\storage\test_media_store.py:275` 的
「OS does not grant temporary-test symlink creation」（Windows symlink 权限），
语义未变、未被删除、未被改写为通过。

新增 Phase 11 测试文件（全部为新增，未删除或弱化任何历史断言）：

- `tests/ui/test_phase11_design.py` — Design System、无硬编码颜色、无动画、Shell
- `tests/ui/test_phase11_context.py` — Workspace 隔离、Context Reset、Conflict UI
- `tests/ui/test_phase11_person_detail.py` — Person Detail 层级、身份卡、归档、Tab
- `tests/ui/test_phase11_events.py` — Event 阅读顺序、Evidence 标记、Timeline 筛选
- `tests/ui/test_phase11_dashboard_search_graph.py` — Dashboard、全局搜索、Graph 筛选
- `tests/ui/test_phase11_analysis_memory_settings.py` — Analysis 工作台与锁定、Memory 分组、Settings 分区与 Workspace 管理

