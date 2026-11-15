# Memory 与 Review：用户确认的长期记忆（Phase 9）

## 三层必须严格区分

```text
Fact      Person / Relationship / RelationshipHistory / Event / Evidence   ← 数据库事实
Analysis  AnalysisRun（LLM 输出、上下文快照、来源引用）                      ← 模型推断，永不回写事实
Memory    用户明确确认、要求系统长期记住的语义信息                            ← 只有用户能创建
```

唯一允许的路径是：

```text
Analysis → 用户复核（Review）→ 明确确认（[确认保存]）→ Memory
```

**禁止 `Analysis → 自动 Memory`**。Memory 也可以完全由用户手工创建。创建 Memory 不会修改任何事实：它既不写 Person／Relationship／Event／Evidence，也不新建事件或关系。

## Memory 数据模型

| 字段 | 说明 |
| --- | --- |
| `id` | UUID |
| `workspace_id` | 必填，复合 FK 到 workspace |
| `person_id` | 可空；NULL = Workspace 级记忆，非 NULL = 该人物专属记忆 |
| `kind` | `FACT` / `PREFERENCE` / `CONTEXT`（CHECK 约束） |
| `content` | 1–4000 字符，必须等于 trim 后的自己 |
| `source_type` | `MANUAL` / `ANALYSIS` |
| `source_analysis_id` | 仅 `ANALYSIS` 时有值，复合 FK 到 `analysis_run(workspace_id, id)` |
| `confirmed_at` | 必填：Memory 的存在就意味着用户已确认 |
| `valid_from` / `valid_until` | 可空，`valid_until >= valid_from` |
| `created_at` / `updated_at` / `deleted_at` | 时间戳与软删除 |

**不使用** `target_type` / `target_id` 多态引用。跨 Workspace 引用 Person 或 AnalysisRun 会被复合外键拒绝，Service 也会再校验一次。

不建立 `UNIQUE(person_id, content)`：同一人物可以同时存在"张三喜欢摄影"和"张三喜欢摄影活动"，内容也可能被编辑。

## 生命周期

```text
手工：Person Detail → Memories → Add       → MANUAL, source_analysis_id=NULL, confirmed_at=now
分析：Analysis → Save as Memory → 编辑 → [确认保存] → ANALYSIS + source_analysis_id
        ↓
   当前有效（默认查询）── 编辑 content/kind/有效期 ──▶ updated_at 更新
        ↓
   soft delete（deleted_at=now）── restore ──▶ deleted_at=NULL，来源分析引用保留
```

**"当前有效"定义**（Repository 查询与 `Memory.is_current` 一致）：

```text
deleted_at IS NULL
AND (valid_from  IS NULL OR valid_from  <= now)
AND (valid_until IS NULL OR valid_until >  now)
```

过期与尚未生效的 Memory **都保留在库里**，只是默认查询不把它们当作当前记忆；`include_expired=True` 可以查看全部。

本阶段**没有** MemoryRevision：编辑会覆盖 `content`（`updated_at` 更新），历史版本留到 Revision 阶段。

## 用户确认

`MemoryService.create_from_analysis(...)` 的 `confirmed` 是必需的关键字参数，且必须**字面为 `True`**：

- 打开确认对话框**不算**确认，取消不调用 Service，因此不创建任何 Memory；
- `content` 由用户提供并可在对话框中编辑，Service **绝不**自动复制 `raw_result`；
- `confirmed_at` 在确认时生成；
- 只有状态为 `SUCCEEDED` 的分析才能保存为 Memory。

## 来源可追溯（Source Traceability）

`AnalysisSource` 只是**引用**（`analysis_*_source` 四张关联表），Review UI 从 Source 直接跳到真实事实详情：Person → Person Detail，Relationship → 关系详情，Event → Event Detail，Evidence → 其所属 Event 详情并选中该证据。**不复制事实正文**，也不在分析层创建事实副本。

### 模型引用只作候选

若模型输出里出现 `[EVENT:uuid]`、`[EVIDENCE:uuid]`、`[PERSON:uuid]`、`[RELATIONSHIP:uuid]`，`candidate_references()` 会解析出来，但**只作为候选**：

1. 标记体必须是合法 UUID（否则记为"不是合法的 UUID"）；
2. 该对象必须属于**本次运行记录的来源集合**（否则"该对象不在本次分析的来源中"）；
3. 该对象必须仍然存在且属于同一 Workspace（否则"该对象已不存在或不属于当前 Workspace"）。

三条都通过的候选才标记为 `accepted`，UI 也只给这些提供跳转。解析过程**不写任何数据**，候选永不自动变成 Source 或 Memory。若模型引用不可靠，Review UI 仍只提供来源列表与人工跳转。

## 与 ContextBuilder 的边界

`AnalysisRequest.include_memories` **默认 `False`**：Memory 不会被偷偷喂给所有分析。显式开启后：

- 只收集**当前有效**的 Memory——目标人物的 + Workspace 级的（关系／事件目标则取其相关人物的）；
- 已删除、已过期、尚未生效的 Memory 永不进入上下文，**即使用户显式指定 id 也不会**；
- `ContextSnapshot.scope["included_memory_ids"]` 记录本次到底用了哪些 Memory，`scope["include"]["memories"]` 记录开关状态；
- Memory 小节带注释"由用户确认，可能已经过时，不是数据库事实"。

这样未来可以回答："这次分析当时参考了哪些用户确认的记忆。"

## 隐私

Memory 属于长期私人信息：普通日志与错误日志**都不写 `content`**（只记异常类型与 id）；不进入 HTTP 日志或 SQLite debug 日志。只有用户显式开启 `include_memories` 时，Memory 文本才进入 ContextSnapshot 与 LLM 请求。

## 数据库

`0008_memory`（`down_revision = 0007_analysis`）只新增 `memory` 一张表与其 CHECK／复合 FK／索引，不改 0001–0007，不触碰事实表与 FTS5 搜索投影。业务事实表始终是 11 张。

## 边界

本阶段**不实现**：Memory 自动生成、Memory 自动整合、Memory 相似度搜索、embedding／向量库、RAG、Sync、ChangeLog、Revision、Vision、OCR、自动事实修改、自动关系修改。也没有 Memory 的 Revision 表或向量表。
