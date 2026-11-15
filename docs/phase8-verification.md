# Phase 8 验收记录

## 最终结果

最终执行 Phase 1–8 全量回归，退出码 **0**：

| 收集项 | passed | environment-skipped | failed |
| ---: | ---: | ---: | ---: |
| **1250** | **1245** | **5** | **0** |

耗时 208.74 秒。Phase 5/6/7 的结果见各自验收记录；本次保留全部旧业务测试，只把 head／表集合断言更新到 `0007_analysis`，并把分析层 5 张表计入"完整 schema"、不计入 11 张业务事实表。**没有删除、弱化或改写任何断言，5 项 symlink 跳过原样保留。**

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -ra
```

真实跳过原文：

```text
SKIPPED [5] tests\storage\test_media_store.py:275:
OS does not grant temporary-test symlink creation
1245 passed, 5 skipped in 208.74s
```

## 交付范围

- **LLM Provider**：[provider.py](<../app/services/llm/provider.py>)（OpenAI-compatible、可注入传输层）、[transport.py](<../app/services/llm/transport.py>)（标准库 HTTP + 测试用 FakeTransport）、[errors.py](<../app/services/llm/errors.py>)（类型化安全错误）。
- **配置与凭据**：[config.py](<../app/services/llm/config.py>)（数据目录内 `llm-providers.toml`，只有非敏感字段）、[credentials.py](<../app/services/llm/credentials.py>)（`env:` / `os:` / 无凭据）、[gateway.py](<../app/services/llm/gateway.py>)（装配、重试策略、取消）。
- **ContextBuilder**：[context.py](<../app/services/context.py>)（AnalysisRequest → ContextSnapshot，直接读事实层，不经过 Search）。
- **PromptBuilder**：[prompt.py](<../app/services/prompt.py>)（system／context／user 三段，Prompt 不写死在 UI）。
- **AnalysisService**：[analysis.py](<../app/services/analysis.py>) + [analysis_repositories.py](<../app/services/analysis_repositories.py>) + [analysis_repositories.py（存储）](<../app/storage/analysis_repositories.py>)。
- **Domain**：[analysis.py](<../app/domain/entities/analysis.py>)（AnalysisRun／AnalysisSource，含状态与类型约束）。
- **Migration 0007**：[0007_analysis.py](<../alembic/versions/0007_analysis.py>)（5 张分析表，不动事实表）。
- **UI**：[analysis_page.py](<../app/ui/analysis_page.py>)、[analysis_dialog.py](<../app/ui/analysis_dialog.py>)，以及 Person／Relationship／Event 的 Analyze 入口。
- [分析层设计](<analysis.md>)、[架构](<architecture.md>)、[数据库](<database.md>)、[README](<../README.md>) 已更新。

## 规格测试项对照

| 规格条目 | 覆盖 |
| --- | --- |
| Provider 1–10（base_url、API key reference、success、401/403/429/500、timeout、malformed、context too large） | [tests/services/llm/test_provider.py](<../tests/services/llm/test_provider.py>) |
| ContextBuilder 11–23（person／relationship／event 上下文、workspace 隔离、deleted 过滤、未来 history、as_of、时间范围、选中事件／证据、观察、证据元数据、无图片二进制） | [tests/services/test_context_builder.py](<../tests/services/test_context_builder.py>) |
| Analysis 24–31（创建、快照持久化、来源持久化、成功、失败、取消、历史、不覆盖旧结果） | [tests/services/test_analysis_service.py](<../tests/services/test_analysis_service.py>) |
| Privacy 32–33（API Key 不入日志／不入库、私密上下文与结果不入普通日志） | 同上（`test_api_key_is_never_logged_or_stored`、`test_private_context_and_result_are_not_logged`） |
| Migration 34–36（0006→0007、0007→0006、事实表不变） | [tests/migrations/test_analysis_schema.py](<../tests/migrations/test_analysis_schema.py>) |
| 完整回归 Phase 1–7 | 1245 passed 全量回归 |

## 关键实现决定与实测结论

1. **不引入 HTTP 依赖。** 默认传输层用标准库 `urllib.request`，并抽象为 `HttpTransport`；测试一律注入 `FakeTransport`，**没有任何测试会发出真实网络请求**。
2. **凭据只以引用形式存在。** 数据库、TOML 与日志只保存 `credential_ref`（`env:NAME` / `os:NAME` / 空）。`Authorization` 头只在请求瞬间构造；`analysis_run` 表**没有任何凭据列**，测试对列名做关键词断言。
3. **Provider 配置放在数据目录的独立文件**，主配置继续只接受 `[app]`/`[logging]`/`[storage]`，保持 Phase 1 的严格校验不被放松；`extra_headers` 里出现 `Authorization` 会被直接拒绝。
4. **`as_of` 是所有时间范围的上界。** 未来记录尚未成为事实，默认不进入上下文；用户显式选中的事件／证据总是包含。未来生效的关系历史会保留并标注"计划中，截至 as_of 尚未生效"，且不参与"当前状态"。
5. **证据默认只发元数据。** `content_text` 与 `annotation` 只有在用户显式勾选 `include_evidence` 时才发送；图片／文件证据永远只发 mime／size 等元数据，不做 Vision。
6. **超限明确报错，不静默截断**（`Selected context exceeds model context limit.`）。
7. **重试只针对瞬时错误**（429／5xx／超时／连接失败），默认最多 2 次尝试；401/403/404/400 立即失败，避免重复外发可能含私人信息的请求。
8. **取消是协作式的**（请求前后检查 token），只影响这一次模型调用，不改任何事实；运行记为 `CANCELLED`。
9. **`/models` 缺失不算连接失败**：兼容 API 不一定实现它，404 时返回空列表。

## 验证方式

- Provider：全部通过假传输层覆盖 200/401/403/404/429/500/超时/连接失败/非法 JSON/缺字段/context 过长；断言错误文本里**不含**服务端私有响应体与密钥。
- ContextBuilder：真实 SQLite + 真实迁移，覆盖三类目标、Workspace 隔离、软删除过滤、未来历史标记、as_of 与五种时间范围、显式选中、观察开关、证据元数据默认值与原文显式开启、图片证据只出 metadata，以及超限报错。
- AnalysisService：断言每次运行前后**所有事实表逐行不变**（Core select 原始列值比较），快照与四张来源表正确落库，失败/取消/重试/历史/多运行不覆盖，以及日志中不出现密钥、证据原文与结果正文。
- Migration：`0006 → 0007 → 0006 → 0007` 往返保留 11 张事实表全部数据与约束，降级后无 `%analysis%` 对象，注入失败后 revision 与 schema 不变，离线 `--sql` 在真实 SQLite 执行成功，`PRAGMA foreign_key_check` 干净；PostgreSQL 仅 DDL compile。

## 桌面启动

全新空临时库、原生 Windows Qt，退出码 0：

```text
Empty database initialized through Alembic
Database schema version verified
Application started (Phase 8)
GUI smoke test passed: window visible and 6 pages navigable; Qt platform=windows
Application stopped: 0
```

自动 UI 测试使用 offscreen；此 smoke 不等同于人工 DPI／布局评审。所有验收 fixture 都在 pytest 临时目录，没有生成示例业务数据。

## 边界

没有 Memory、Vision／OCR、RAG／向量库、聊天导入、自动事件提取、自动摘要、自动关系判断、Sync／ChangeLog／Revision，也没有这些表。分析层不修改任何事实；由分析结果改事实的流程（Analysis → 用户复核 → 显式 Service 操作）本阶段不实现。**所有模型调用都必须由用户点击触发**：打开页面、加载 Dashboard、修改事件或创建关系都不会自动调用模型。PostgreSQL 只做 DDL compile，未做真实部署验证。

**完成 Phase 8 后停止。**
