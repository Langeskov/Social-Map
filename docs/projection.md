# 投影层：Search / Timeline / Graph / Dashboard（Phase 7）

## 定位

事实层是 Source of Truth：

```text
Person / PersonAlias / Relationship / RelationshipHistory
Event / EventParticipant / Evidence / EvidenceMedia / Media
```

Phase 7 只增加**投影层（Projection / Retrieval Layer）**，它们全部可以从事实层重新计算：

| 投影 | 形态 | 是否落库 |
| --- | --- | --- |
| Unified Search | FTS5 索引 + `search_document` 投影行 | 是，但只是可重建基础设施 |
| Person / Workspace Timeline | 运行时 DTO | 否 |
| Relationship Graph | 运行时 DTO，Qt 场景由 UI 构建 | 否 |
| Dashboard | 运行时 DTO（统计 + 最近列表） | 否 |

**没有** `GraphNode`／`GraphEdge`／`TimelineRecord`／`DashboardRecord` 等事实表，也没有统计缓存。搜索投影被删掉、损坏或被手工清空时，事实数据不受影响，`rebuild_index()` 或下一次查询即可重建。

## Unified Search

### 索引结构（Migration 0006）

```text
search_document(id INTEGER PK, workspace_id, entity_type, entity_id, parent_id,
                title, body, title_tokens, body_tokens,
                occurred_at, deleted_at, updated_at)
  UNIQUE(workspace_id, entity_type, entity_id)
  CHECK(entity_type IN ('PERSON','ALIAS','EVENT','EVIDENCE'))
search_document_fts(title_tokens, body_tokens)      -- FTS5 虚表，keyed by rowid
```

`search_document` 是**投影**：`title/body` 保存原始文本（用于展示与摘要），`*_tokens` 保存分词结果供 FTS5 索引。两者都由事实层派生。

### 中文分词（本机实测约束）

FTS5 默认 `unicode61` 会把一整段中文当成**一个 token**，所以 `摄影` 无法匹配 `共同参加摄影活动`；本机 SQLite 3.53.2 又**没有编译 trigram 分词器**。因此：

- **索引时**：CJK 连续段拆成单字（`共 同 参 加 摄 影 活 动`），拉丁/数字串保持完整；
- **查询时**：CJK 段构造为引号短语 `"摄 影"`，拉丁段构造为前缀 `"photo"*`，多段用 `AND` 连接。

用户输入**永不**直接进入 `MATCH`：`*`、`"`、`-`、`AND`、`OR`、`NOT`、`(`、`)`、`:` 等一律作为普通文本处理，只保留其中的词元。空查询与纯符号查询直接返回空结果，不构造表达式。

摘要（snippet）在 Service 侧用**原始文本**按查询词定位生成，因此中文连续可读，不依赖 FTS5 的 `snippet()`（那会输出被空格拆开的单字）。

### 搜索字段

| 实体 | 可搜索字段 |
| --- | --- |
| Person | `display_name`、`real_name` |
| Alias | `alias`（并继承所属 Person 的归档状态） |
| Event | `title`、`event_type`、`description`、`location` |
| Evidence | `title`、`content_text`、`annotation`、`source_label`、`source_reference` |

Evidence 的可见性跟随其 Event：Event 被软删除时，它的 Evidence 在默认搜索中一并隐藏。

### SearchResult DTO

`id`（投影行 id）、`entity_type`、`entity_id`、`workspace_id`、`title`、`snippet`、`rank`（bm25，越小越靠前）、`metadata`（`occurred_at`／`deleted`／`parent_id`／`event_id`／`person_id` 等可展示字段）。**不返回 ORM**；跳转由 UI 用 `entity_id` 再走 Service 取详情。

### Workspace 与软删除

每次搜索都强制 `workspace_id`（Service 先校验 Workspace 存在且未归档），投影行同时冗余保存 `deleted_at`，因此过滤不需要回查事实表。默认 `deleted_at IS NULL`；`include_deleted=True` 仅供管理／恢复 UI。Relationship 的 `ENDED` 不是删除，历史始终可见。

### 索引同步与重建

事实写入路径（Person／Alias／Event／Evidence 的创建、编辑、软删、恢复）在**同一个 Service 事务**内更新投影：适配器在 `save`／`update_metadata` 之后调用 `app/storage/search_projection.py` 的 `sync_*`。因此不会出现"事实已提交、投影未更新"的窗口；投影写失败会让整个事务回滚，而即使投影被外部破坏，也能用 `rebuild_index(workspace_id=None)` 从事实层整体重建（只读事实表）。

`SearchService.search()` 还做了一个自愈检查：若 FTS 对象不存在或该 Workspace 的投影为空，则先重建再匹配。

## Timeline

`TimelineItem`：`timestamp`、`kind`（`EVENT` / `RELATIONSHIP_CHANGE`）、`title`、`summary`、`entity_id`、`person_ids`、`relationship_id`、`event_id`。

- 数据来源只有 **Event** 与 **RelationshipHistory**；Evidence 绝不作为独立 Timeline 条目（它是 Event Detail 的子项）。
- 排序：`timestamp DESC` → 同刻 `EVENT` 先于 `RELATIONSHIP_CHANGE` → `entity_id` 字符串决赛。History 用 `effective_at`；Event 用 `started_at`，日期事件用该日 00:00 UTC 作为排序时刻（**DATE 语义本身不被改写**）。
- **未来时间照常显示**：不因 `timestamp > now` 过滤，过去／未来／补录事件都出现在时间线上。
- Person Timeline = 该人参与的 Event + 端点为该人的 RelationshipHistory；Workspace Timeline = 该 Workspace 全部 Event + 全部 History。
- 查询为固定条数的批量 SQL（无 N+1），并且**不写任何表**（测试断言调用前后所有表名与行数不变）。

## Relationship Graph

Person = node，Relationship = edge，每次从 `PersonRepository`／`RelationshipRepository` 读取事实后在内存构建并绘制，**不保存为独立数据**。Edge 的当前状态由 Relationship + History 派生，不写入图数据。默认隐藏已归档 Person 与已删除 Relationship，可切换显示。显示内容仅限事实：姓名、头像、关系数量、关系类型、方向（DIRECTED 画箭头 / UNDIRECTED 无方向）、状态（ACTIVE／PAUSED／ENDED）。**没有** AI 分析、人格、好感度或推断分数，也没有粒子／流光／自动布局动画。

## Dashboard

统计与最近列表直接来自事实表（每条查询带 `LIMIT`，统计用 `COUNT`），**不落缓存、不建统计表**：

- 人物数量、活跃关系数量（当前状态为 ACTIVE，且按全序 `(effective_at, created_at, id)` 派生，未来状态不算当前）；
- 最近 7 / 30 天 Event 数量（按发生时刻窗口）与 Evidence 数量（按 `created_at` 窗口）；
- 最近人物、最近 Event、最近关系变更、最近 Evidence 四个列表（各 5 条）。

不提供"关系强度""好感度""人物危险度"这类没有明确统计学意义的指标。

## Migration 0006

`0006_search_projection`（`down_revision = 0005_evidence`）创建 `search_document` 与 `search_document_fts`，**不新增业务事实表**（仍是 11 张），不改任何事实表，也不改 0001–0005。降级删除这两个投影对象并保留全部事实数据。

### SQLite 临时目录（重要运行前提）

`DROP TABLE` 一个 FTS5 虚表时 SQLite 需要**可写的临时目录**。当宿主禁止写系统临时目录时，SQLite 返回 `SQLITE_CANTOPEN`（"unable to open database file"），而不是权限错误——本项目在有此限制的环境里通过把 `SQLITE_TMPDIR`／`TMPDIR`／`TEMP`／`TMP` 指向可写目录解决（见 [conftest.py](<../conftest.py>)）。升级／降级都需要这个前提；真实用户机器上系统临时目录可写，不受影响。

## PostgreSQL

PostgreSQL **没有** SQLite FTS5。本阶段只对事实与投影的 ORM 模型做 DDL compile 验证，搜索仅在 SQLite 上实现；未来替换为 `tsvector`／`tsquery` 时应替换 Search Adapter，而不改变 Service 与 DTO 契约。不得把 compile 验证描述为 PostgreSQL 部署验证。

## 边界

不实现 LLM、Analysis、AnalysisSource、Memory、OCR、自动摘要、自动关系判断、向量数据库、Sync、ChangeLog、Revision。Graph 不推断关系，Timeline 不修改事实，Search 不上传任何数据。
