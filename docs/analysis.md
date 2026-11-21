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

### 来源曝光契约（Phase 13.1）

`ContextSnapshot` 同时产出一份版本化的 **Context Entity Manifest**，记录每个实体的
类型、UUID、上下文角色与实际曝光等级（PERSON：`PROFILE`／`IDENTITY_ONLY`／
`PARTICIPANT_CONTEXT`；EVIDENCE：`METADATA_ONLY`／`CONTENT_INCLUDED` 等，按实体类型
区分）。同一份 Manifest 驱动：

1. `rendered_context` 中的 `[TYPE:<uuid>]` 实体标记（有限曝光人物会注明"仅身份信息，
   未提供完整档案"）；
2. `ContextSnapshot.source_ids()`；
3. `AnalysisService._persist_sources()` 写入的四张 `analysis_*_source` 表；
4. Phase 13.2 的引用验证。

因此关系端点、事件参与人等"只被表示、未提供档案"的人物也会成为 Person 来源，但
曝光等级如实记录为 `IDENTITY_ONLY`／`PARTICIPANT_CONTEXT`。**AnalysisSource 成员资格
不代表完整资料已提供，也不代表任何结论有依据。** 旧 Snapshot（无 Manifest）按旧
scope 规则读取，不回填、不升级。详见
[Phase 13 评估框架 §10](<phase13-evaluation.md>)。

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

System Prompt 明确要求：不修改事实、区分 Fact／Observation／Inference、不编造记录、
不引用未提供的信息、明确不确定性、只使用给定证据，引用来源只能使用上下文中
`[TYPE:<uuid>]` 的标记（不得编造 UUID），并且**不给出好感度／危险度／人格类别或任何
0-100 评分**。

输出契约（Phase 13.1 起）是 **JSON schema 1**：`schema_version` + `facts` /
`observations` / `interpretations` / `uncertainties` 四个数组；每项
`{"content", "references": [{"type", "id"}], "confidence_description", "notes"}`。
Provider 不支持 JSON mode 时也按普通文本返回，解析器负责降级；旧版四段 Markdown 继续
兼容。**原始回答永远保留**，结构化只是解析产物。

## 结构化结果与解析器（Phase 13.1）

- DTO：`app/domain/entities/analysis_result.py`（`AnalysisResult` / `AnalysisEntry` /
  `AnalysisReference`，纯 stdlib）；
- 解析器：`app/services/analysis_result_parser.py`（纯 Python、无 Qt、无数据库、无
  LLM、无网络），支持合法 JSON、JSON 前后夹杂文本/代码围栏、缺字段、类型错误、非法
  引用结构、重复条目、多语言标题、旧版 Markdown、纯文本与超大文本；
- `parse_status`：`PARSED_JSON`／`PARSED_JSON_FRAGMENT`／`LEGACY_MARKDOWN`／
  `UNSTRUCTURED_TEXT`／`EMPTY`／`TOO_LARGE`；无法识别的内容进入
  `unparsed_content`，解析告警进入 `warnings`，**不静默删除任何内容**；
- UI：结果对话框默认展示结构化小节（引用标注为"未校验"），并提供"模型原始输出"
  只读区与解析提示；解析失败降级为原文展示，解析告警**不代表模型调用失败**；
- 解析出的引用只是**结构**：是否真实存在、是否属于本次 Run、曝光等级是否足够，
  由 Phase 13.2 的验证器判定。

## 来源引用验证（Phase 13.2）

引用验证只回答一个问题：**这条引用是否合法**；它不判断句子的语义是否被材料支持。

- 实现：`app/services/reference_verification.py` 的 `ReferenceVerifier`，使用
  `AnalysisSource`（本次运行来源集合）+ `Context Entity Manifest`（曝光等级）+
  跨 Workspace 只读查找（`ReferenceLookupRepository`）；同一验证器同时服务
  候选引用、结果对话框与离线评估 Runner，不存在第二套 accepted 规则。
- 状态（互斥，附原因）：`VALID_FULL`、`VALID_LIMITED`、`INVALID_FORMAT`、
  `UNKNOWN_SOURCE`、`NOT_IN_RUN`、`WORKSPACE_MISMATCH`、`TYPE_MISMATCH`、
  `DELETED_SOURCE`、`SOURCE_NOT_EXPOSED`。
- `VALID_LIMITED` 表示实体只被有限曝光（IDENTITY_ONLY / PARTICIPANT_CONTEXT /
  METADATA_ONLY / EVENT_REFERENCE）或旧快照无 Manifest——**旧记录不升级为
  VALID_FULL**，也不回填。
- UI：结果对话框逐条显示引用状态；候选引用表显示"有效（完整/有限曝光）/无效（状态 ·
  原因）"，只有有效引用可跳转；解析出的 FACT 条目缺少引用时不会被补造。
- 确定性检查与语义评价分离：引用存在/归属/类型/曝光可自动判定；"这条推断是否真的
  由证据支持"只能人工复核，Phase 13.2 不声称能自动证明。

## AnalysisRun 与 AnalysisSource

`analysis_run`：id、workspace_id、target_type、target_id、analysis_type、provider_name、model_name、scope、status、context_snapshot、result、error_message、created_at／started_at／completed_at／duration_ms。状态 `PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED`。**不保存 API Key**（scope 里只有 `credential_ref` 文本）。

四张来源关联表（`analysis_person_source`／`analysis_relationship_source`／`analysis_event_source`／`analysis_evidence_source`）各自记录 `analysis_run_id + workspace_id + target_id`，并带两个复合 FK：`(workspace_id, analysis_run_id) → analysis_run(workspace_id, id)` 与 `(workspace_id, target_id) → 对应实体(workspace_id, id)`。**不使用** `source_type/source_id` 这种无法 FK 校验的多态列。

**Source 与 Snapshot 都要保存**：Source 回答"引用了哪些数据库实体"，Snapshot 回答"真正发给模型的文本是什么"。事实会变，所以过去的分析必须能重现当时的上下文。

**来源集合由 Manifest 驱动（Phase 13.1）**：`source_ids()` 现在来自
`ContextSnapshot.entity_manifest`（而非 scope 列表），`_persist_sources()` 因此自动
记录 `IDENTITY_ONLY` 端点与 `PARTICIPANT_CONTEXT` 参与人；Manifest 连同
`version` 存在 `context_snapshot` JSON 里，**没有数据库迁移**。旧 Snapshot 无
Manifest 时按旧 scope 规则读取（不回填、不修改历史 Run）。

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

## 离线评估（Phase 13）

分析质量评估是**独立的离线评估层**（`app/evaluation/`），与事实层、分析层严格分离：

- 版本化合成评估数据集（`tests/evaluation/dataset/`，Scenario A–J）通过真实
  ContextBuilder / PromptBuilder 构建**模型输入**并做确定性验证：来源集合绑定、
  参考答案防泄漏、跨 Workspace 隔离、Token 预算边界、预期上下文断言。
- 评估不写事实、不写 Memory、不访问网络、不调用模型；普通 pytest 完全离线。
- 结果按指标分列（PASS / FAIL / N/A + 原因 + 证据），无单一总分；模型输出相关
  指标在解析器（13.1）与来源校验（13.2）落地前一律 N/A。
- Token 估算为保守近似（CJK 1 字符/token）；超预算时明确拒绝并提示缩小范围，
  不静默截断。详见 [Phase 13 评估框架](<phase13-evaluation.md>)。
