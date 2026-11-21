# Phase 12 — Person Investigation Console 设计文档

> UX Contract & Layout Specification。本方案在 Phase 12.0 经产品负责人批准后实施；
> 文末记录负责人已作出的决定。实现验收见 [Phase12 验收记录](<phase12-verification.md>)。

## 1. 当前架构审查（Phase 12.0 结论摘要）

- 仓库为本地 git（无 remote）；旧远端基线 `f11bc31` 不可获取，本地基线以 Tag
  `phase-11-baseline`（`8bb12ad`）归档。测试基线 1483/1478/5/0（5 项为 Windows
  符号链接环境 Skip，语义不变）。
- 复核的 13 项上轮问题：隐藏 SearchPanel×2、隐藏 workspace_timeline 且仍被查询、
  Workspace Timeline 无可见入口、`PersonService.detail` 无 `include_deleted`、
  别称仓库排除归档、TopBar 状态互相覆盖、`or True` 失效断言、`.verification/`
  被忽略却被文档引用、README/Media/Phase11 文档矛盾——均在 Phase 12.1–12.4 处理；
  「归档解绑 PersonMedia」为 Phase 3 语义（禁止改动，只做诚实展示）；
  「AGENTS.md 忽略规则」复核为已不存在（文件已跟踪）。
- 新发现：TimelinePanel 逐行查询（N+1）、Graph 头像逐节点查询、跨 Workspace 全表
  扫描、分析解锁锚点在对话框而非 QThread、进度对话框从未 show()（12.3 修复）。

## 2. 信息架构

```text
Workspace（唯一 WorkspaceContext）
 └─ People Investigation Workspace（选中 Person 为入口）
     ├─ People Index      列表 / 本地过滤 / 归档过滤 / CRUD            [ENTITY]
     ├─ Person Dossier    身份档案（空字段隐藏）                        [ENTITY/FACT]
     ├─ Ego Network       一层直接关系（GraphCanvas 复用）              [LINK/FACT]
     └─ Inspector（Tab）
         ├─ 关系          RelationshipPanel 管理                       [LINK]
         ├─ Activity      Person Timeline（Event + 关系状态变更）       [OCCURRENCE]
         │    └─ 选中 Event → Event Inspector；关系变更 → Relationship Detail
         ├─ Evidence      证据列表 + 阅读器（主从）                     [PROOF/FACT]
         │    ├─ content_text / 来源 / captured_at   只读 FACT
         │    └─ annotation                         USER 可编辑
         ├─ Memory        用户确认记忆（独立语义）                       [USER]
         └─ Analysis      分析记录 + Sources                           [INFERENCE/DERIVED]

跨页跳转：navigate_requested → MainWindow.open_target（无历史导航栈）
全局搜索唯一入口：TopBar（Ctrl+F）；Workspace Timeline 唯一入口：Events 页 Tab
```

五层语义不变：Entity→Link→Occurrence→Proof→Inference；Memory 是 USER 层，不插入
因果链；不新增 PersonEvidence，Person↔Evidence 只经 Event 参与展示。

## 3. 布局

### 3.1 宽屏 1920×1080（三栏）

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ TopBar：SOCIAL MAP │ Workspace▾ │ 全局搜索(Ctrl+F) │ 状态(带优先级)        │
├───────┬──────────────────────────────────────────────────────────────────┤
│ Nav   │ ContextBar：Workspace › People › <人物名>                        │
│       ├────────────┬───────────────────────────────┬─────────────────────┤
│       │ People     │ Person Dossier                │ Inspector           │
│       │ Index      ├───────────────────────────────┤ 关系│Activity│证据  │
│       │ [过滤/归档]│ Ego Network                   │ 记忆│分析           │
│       │ 人物列表   │ (一层直接关系)                 │                     │
│       │ (≥220px)   │ (弹性, ≥360px)                │ (≥300px)            │
└───────┴────────────┴───────────────────────────────┴─────────────────────┘
```

### 3.2 紧凑 1280×720（嵌套 QSplitter + 底部 Inspector）

```text
┌────────────────────────────────────────────────────────────┐
│ TopBar │ ContextBar                                        │
├───────┬────────────────────────────────────────────────────┤
│ People│ Person Dossier（紧凑；Gallery/别称随卡片滚动可达）   │
│ Index ├────────────────────────────────────────────────────┤
│       │ Ego Network（min-h 240）                            │
│       ├────────────────────────────────────────────────────┤
│       │ Inspector（底部 Tab 抽屉，min-h 200）                │
└───────┴────────────────────────────────────────────────────┘
```

机制：外层纵向 QSplitter{上=横向[Index|中心]，下=Inspector}；宽屏时 Inspector 加入
横向 QSplitter 成右栏。工具栏「Inspector：侧栏⇄底部」手动切换 + 内容宽度约 1400px
阈值自动切换（手动选择后停止自动）。各面板 `setMinimumWidth/Height` 且不可折叠，
任何一列都不会被挤到不可用；不缩字号、不隐藏核心操作。

## 4. Person Investigation Workspace 交互流程

```text
选中人物 → Dossier 刷新 + Ego 重建 + Activity 加载 + Inspector 清空
Ego 节点单击 → 选中该人物（重新定中心）；边单击 → RelationshipDetailDialog
Activity 选中事件行 → Event Inspector（内联只读详情）
     ├─「查看该事件的证据 →」→ Evidence Tab 预选该事件证据
     └─「打开完整事件详情」→ EventDetailDialog（含编辑/参与人/History 操作）
Activity 选中关系变更行 → Relationship Detail（字段 + 完整历史）
     └─「打开关系详情窗口」→ RelationshipDetailDialog
Evidence 选中行 → 阅读器（FACT 原文只读 / USER 注释可编辑 / 所属 Event 可跳）
Analysis 双击运行记录 → AnalysisResultDialog → Sources 双击 → open_target 回跳
Workspace 切换 → 完整 Context Reset（清空一切 + 关闭非模态窗口）
```

## 5. Ego Network 组件职责

- 数据源仅 `RelationshipService.list_by_person`（端点来自 RelationshipView）；
  归档端点/已删除关系默认隐藏、开关显示（与 GraphPage 完全一致）。
- 绘制复用 [graph_canvas.py](<../app/ui/graph_canvas.py>)：`PersonNode`／
  `RelationshipEdge`／`GraphView`／`ego_positions`（中心 + 邻居环）／`build_graph_items`。
  当前人物节点 `current` 强调（粗描边 + tooltip「当前人物」）。
- 有向=箭头+粗线；状态画笔 ACTIVE 实线／PAUSED 虚线／ENDED 点线／删除边归档虚线。
- 空态显式「该人物还没有关系记录。」；归档人物诚实降级（关系读取受限）。
- 不存储坐标、不创建关系、不从 Event 共同参与推断边、不扩展二层。

## 6. Inspector 状态与联动规则

- 单一选择所有者：人物选中只在 People Index；事件选中只在 Activity 表；证据选中只在
  Evidence 列表。子组件发信号由 PeoplePage 路由（本地或 `navigate_requested`）。
- 人物切换：清空 Inspector（活动检查器回空态、阅读器清空、关系字段清空）、上一人物的
  证据/分析选择零残留（刷新自动选中行不触发检查器填充——只有用户点击驱动）。
- 来源标注：每块带 LayerBadge／EpistemicTag；Evidence 标题/类型/来源/captured_at/所属
  Event 齐全；普通文本证据只以只读纯文本呈现（无聊天伪装；数据模型无 Conversation/Message）。

## 7. Workspace Context Reset 规则

沿用 `about_to_change → reset_context() → changed → load_workspace()`。Phase 12 扩展：
Ego 场景、Activity 检查器、Evidence 阅读器随人物/Workspace 清空；TimelinePanel／
RelationshipPanel／EvidencePanel 的非模态详情窗改经共享 dialog_pool 由
DetailWindowTracker 在切换时全部关闭。

## 8. 组件复用 / 新建 / 删除清单

- **复用**：TimelinePanel、RelationshipPanel、MemoryPanel、EvidencePanel、全部对话框、
  design widgets、WorkspaceContext/ContextPage、run_operation、open_target、
  RelationshipService/TimelineService/EventService/EvidenceService/AnalysisService。
- **新建**：`app/ui/graph_canvas.py`（绘图层搬家 + 点击 handler 接缝 + ego 布局）、
  `EvidenceReader`（FACT/USER 分离的唯一渲染）、Inspector 布局机制（侧栏⇄底部）。
- **删除**：PeoplePage 隐藏 SearchPanel、DashboardPage 隐藏 SearchPanel 与
  workspace_timeline（对象与查询一并移除）；`ArchivedPersonSnapshot`（被真实归档读取取代）。

## 9. 文件修改清单（实施结果）

见 [Phase12 验收记录](<phase12-verification.md>) 第 1 节的逐阶段文件清单。

## 10. 分阶段实施顺序

12.1 调查工作台（graph_canvas 抽取 → 三区布局 + Ego）→ 12.2 Activity & Evidence
Inspector（时间线批量读 + 阅读器 + 跳转）→ 12.3 Workspace 导航与清理（时间线入口、
隐藏组件移除、归档契约、锁定）→ 12.4 验证与发布准备（探针、文档、测试质量）。
每阶段独立提交、独立全量回归。

## 11. 测试计划

工作台选择级联与清空、Ego 四态与跳转、Inspector 完整跳转链
（Person→Event→Evidence→Analysis Sources）、Workspace Timeline 入口、归档只读契约、
真实异步分析锁定、N+1 批量读守卫、布局探针；历史测试等价迁移（Dashboard/Phase7/
Phase11 的隐藏对象断言迁至新边界）。

## 12. 决定记录（产品负责人）

| 问题 | 决定 |
|---|---|
| lint 清理处置 | 独立提交（`chore/lint-cleanup` 分支指针同指 `93bdf4f`） |
| Git 基线 | 本地 Tag `phase-11-baseline`；不虚构与旧远端的连续历史 |
| 归档读取契约 | 批准；按 7.3 于 12.3 实施 |
| Inspector 紧凑形态 | 嵌套 QSplitter + 侧栏/底部切换（推荐方案） |
| TimelinePanel N+1 | 改批量/按需读（12.2 实施） |
| Ego 默认显隐 | 与 GraphPage 一致（归档/删除默认隐藏 + 开关） |
| Windows CI | 本轮不做 |
| 真实 Windows DPI | 产品负责人人工执行 |
| 文档修正范围 | 含 `docs/verification.md` 一并修正 |
