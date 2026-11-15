# 分析层：LLM Provider / ContextBuilder / Analysis（Phase 8）

## 定位：分析层不是事实层

```text
事实层：Person / Relationship / RelationshipHistory / Event / Evidence / Media
投影层：Search / Timeline / Graph / Dashboard（Phase 7）
分析层：AnalysisRun / AnalysisSource（Phase 8）← 只能读事实，永不写事实
```

模型输出**只能**进入 `AnalysisRun`。LLM 永远不能修改 Person、Relationship、RelationshipHistory、Event、Evidence 或 Media。未来若要由分析结果改事实，必须经过「Analysis → 用户复核 → 显式 Service 操作 → 事实变更」，本阶段**不实现**后两步。

## Provider

只实现 OpenAI-compatible Chat Completions，厂商无关：

```text
ChatMessage(role, content) / ChatRequest(model, messages, temperature, timeout, extra_headers)
ChatResponse(content, model, finish_reason, usage, raw_id)

LLMProvider: name / chat(request) / test_connection() / list_models()
OpenAICompatibleProvider → POST {base_url}/chat/completions
```

**不引入 HTTP 依赖**：默认传输层用标准库 `urllib.request`，并且传输层可注入（`HttpTransport` 协议），测试一律使用 `FakeTransport`——**没有任何测试会发出真实网络请求**。`/models` 在兼容 API 中是可选的，返回 404 时视为"不支持"，不算连接失败。

## 配置

Provider 配置保存在数据目录内的 `llm-providers.toml`（主配置仍只接受 `[app]`/`[logging]`/`[storage]`，保持 Phase 1 的严格校验）：

```toml
[llm]
default_provider = "local"

[[llm.providers]]
provider_name = "local"
base_url = "http://127.0.0.1:1234/v1"
model = "local-model"
timeout = 30.0
temperature = 0.2
credential_ref = "env:OPENAI_API_KEY"
```

只有非敏感字段；`extra_headers` 中禁止出现 `Authorization`（必须走 `credential_ref`）。UI 可新增／编辑／删除／设为默认／测试连接。

## 凭据

数据库、配置与日志只保存 `credential_ref`：

| 形式 | 含义 |
| --- | --- |
| `env:NAME` | 从环境变量读取 |
| `os:NAME` | 从操作系统安全存储读取（Windows 凭据管理器，经 ctypes；其他平台可用时用 keyring） |
| 空 / `none` | 无凭据（本地端点常见） |

`Authorization` 头只在请求瞬间由解析出的密钥构造；日志与 `AnalysisRun` 只记录引用文本。程序**不会**自动写入或删除系统凭据。

## ContextBuilder

输入 `AnalysisRequest`：

```text
workspace_id / target_type(PERSON|RELATIONSHIP|EVENT) / target_id / analysis_type / as_of
time_scope(LAST_7_DAYS|LAST_30_DAYS|LAST_90_DAYS|ALL|CUSTOM) / custom_start / custom_end
include_relationships / include_history / include_observations / include_evidence
selected_event_ids / selected_evidence_ids / max_context_tokens
```

输出 `ContextSnapshot`：`analysis_run_id`、`generated_at`、`scope`（时间窗、as_of、各开关、选中的 id）、`sections`（按 Fact／Observation 分组的行）、`rendered_context`（真正发送的文本）。

- **直接读事实层 Repository，不经过 SearchService**：Search 负责"找到对象"，ContextBuilder 负责"构造事实上下文"。
- **默认上下文**：目标实体本身 + 当前关系 + 近期关系历史 + 近期事件 + **证据元数据**；**证据原文只有显式勾选才发送**。
- **时间窗**：Event 用 `started_at`（日期事件用 `occurred_on` 当天 UTC 零点），History 用 `effective_at`，Evidence 用 `captured_at` 或 `created_at`；`as_of` 是所有范围的上界——未来记录尚未成为事实，默认不进入上下文，但用户显式选中的事件／证据总是包含。
- **未来生效 History** 会被保留并标注"计划中，截至 as_of 尚未生效"，绝不参与"当前状态"。
- **大小守卫**：估算 token 超限时**明确报错**（`Selected context exceeds model context limit.`），不静默截断；本阶段不实现摘要／压缩／分块。

## 事实 / 观察 / 推断

System Prompt 明确要求：不修改事实、区分 Fact／Observation／Inference、不编造记录、不引用未提供的信息、明确不确定性、只使用给定证据，并且**不给出好感度／危险度／人格类别或任何 0-100 评分**。输出模板要求四节：Facts／Observations／Interpretations／Uncertainties；UI 用 `split_result_sections` 做结构化展示，**不强制 JSON**。

## AnalysisRun 与 AnalysisSource

`analysis_run`：id、workspace_id、target_type、target_id、analysis_type、provider_name、model_name、scope、status、context_snapshot、result、error_message、created_at／started_at／completed_at／duration_ms。状态 `PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED`。**不保存 API Key**（scope 里只有 `credential_ref` 文本）。

四张来源关联表（`analysis_person_source`／`analysis_relationship_source`／`analysis_event_source`／`analysis_evidence_source`）各自记录 `analysis_run_id + workspace_id + target_id`，并带两个复合 FK：`(workspace_id, analysis_run_id) → analysis_run(workspace_id, id)` 与 `(workspace_id, target_id) → 对应实体(workspace_id, id)`。**不使用** `source_type/source_id` 这种无法 FK 校验的多态列。

**Source 与 Snapshot 都要保存**：Source 回答"引用了哪些数据库实体"，Snapshot 回答"真正发给模型的文本是什么"。事实会变，所以过去的分析必须能重现当时的上下文。

## 错误处理与重试

| 情况 | 结果 |
| --- | --- |
| 401／403 | `LLMAuthError`，立即失败，不重试 |
| 404 | `LLMNotFoundError` |
| 429 | `LLMRateLimitError`，重试 |
| 5xx | `LLMServerError`，重试 |
| 超时／连接失败 | `LLMTimeoutError`／`LLMConnectionError`，重试 |
| 非法 JSON／缺少 content | `LLMResponseError` |
| 上下文过长 | `LLMContextTooLargeError` |

全部落到 `AnalysisRun.status = FAILED` 与**安全**的 `error_message`（不含响应体、Authorization、密钥）；UI 直接显示该文本。重试只针对瞬时错误，默认最多 2 次尝试，绝不重复发送 401/403/4xx 请求。

## 取消

`CancelToken` 是**协作式**取消：每次尝试前与响应后检查，取消后立即停止继续发送请求；已经在飞的 HTTP 请求由自身超时结束。取消只影响这一次模型调用，不修改任何事实；运行记为 `CANCELLED`。

## 隐私

- ContextBuilder 决定发送范围：**禁止"把整个 Workspace 发给模型"**，默认只发目标与有限相关范围，扩大范围必须用户显式勾选。
- 图片／文件证据只发 metadata（title／kind／captured_at／source／mime／size），**不发二进制、不做 Vision**。
- 普通日志只记录异常类型与 run id，不写完整 context、完整 result 或证据原文。

## 迁移 0007

`0007_analysis`（`down_revision = 0006_search_projection`）建立 5 张分析表，不改 0001–0006，也不触碰事实表。业务事实表仍是 11 张；分析表属于分析层。降级只删这 5 张表。

## 边界

不实现 Memory、Vision、OCR、聊天导入、自动事件提取、向量数据库、RAG、Sync、ChangeLog、Revision；不建立 Memory／Vector／Conversation 表。**所有模型调用都必须由用户明确点击触发**：打开页面、加载 Dashboard、修改事件或创建关系都不会自动调用模型。
