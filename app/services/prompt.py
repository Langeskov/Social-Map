"""PromptBuilder: the three-part prompt lives in the service layer, not in the UI.

System rules are deliberately explicit about the fact/observation/inference
boundary, about not inventing records and about stating uncertainty. The model is
never asked for affinity/danger scores or personality labels. Since Phase 13.1 the
output contract is structured JSON (schema 1) whose entries carry explicit
references to ``[TYPE:<uuid>]`` markers from the context; the parser still accepts
the legacy four-section Markdown for old answers.
"""
from dataclasses import dataclass

from app.services.analysis_result_parser import split_legacy_sections
from app.services.context import ContextSnapshot
from app.services.llm import ChatMessage, ROLE_SYSTEM, ROLE_USER

SYSTEM_PROMPT = """你是一个本地人际关系知识库的分析助手。你只能使用用户提供的上下文。

必须遵守：
1. 你永远不能修改数据库中的任何事实（人物、关系、关系历史、事件、证据、媒体）。你的输出只是分析文本。
2. 严格区分三类信息，并在输出中分开陈述：
   - Fact（事实）：上下文中来自数据库的记录，例如事件、参与人、关系、关系历史、证据元数据。
   - Observation（观察）：用户在上下文中明确记录的观察（标记为 OBSERVATION 的内容或用户注释）。
   - Inference（推断）：你自己基于上述内容做出的推测。推断不得写成事实。
3. 不得编造上下文中不存在的事件、人物、关系、时间或证据。
4. 不得引用未提供给你的信息，也不得假装知道未提供的内容。
5. 明确说明不确定性：证据不足时写“证据不足以确认……”，推测时使用“根据记录……可能……”这类措辞。
6. 不要使用“这个人就是……”“对方一定……”这类断言式人格判断。
7. 只使用上下文中给出的证据；引用来源时只能使用上下文中 [TYPE:<uuid>] 形式的标记（如 [EVENT:<uuid>]），不得编造 UUID，也不得引用没有出现在上下文中的实体。
8. 不要给出好感度、危险度、人格类别或任何 0-100 的评分，除非用户在请求中明确要求某种专门评分。
9. 使用与用户请求相同的语言作答（本应用默认中文）。"""

OUTPUT_TEMPLATE = """请只输出一个 JSON 对象（schema 1），不要输出 Markdown 标题，也不要在 JSON 之外写解释文字：

{
  "schema_version": "1",
  "facts": [
    {"content": "只写上下文中明确记录的内容", "references": [{"type": "EVENT", "id": "上下文中的 UUID"}]}
  ],
  "observations": [
    {"content": "只写用户明确记录的观察", "references": [{"type": "EVENT", "id": "上下文中的 UUID"}]}
  ],
  "interpretations": [
    {"content": "你的推测", "references": [{"type": "EVENT", "id": "上下文中的 UUID"}], "confidence_description": "依据与不确定程度"}
  ],
  "uncertainties": [
    {"content": "说明缺什么信息、哪个结论证据不足", "references": []}
  ]
}

要求：
1. references 的 type 只能是 PERSON、RELATIONSHIP、EVENT、EVIDENCE；id 必须来自上下文中 [TYPE:<uuid>] 标记的 UUID，并且该实体确实出现在上下文里。没有可靠来源时 references 输出空数组 []，绝不编造 UUID。
2. interpretations 必须逐条写明依据（references）与不确定程度（confidence_description），使用“根据记录……可能……”这类措辞。
3. 没有某类内容时输出空数组 []。
4. content 中不要包含 [TYPE:<uuid>] 标记本身，来源写在 references 里。
5. 不得编造事件、人物、时间或证据；不得给出人物评分或人格类别断言。"""

ANALYSIS_GOAL = {
    "PERSON_PROFILE": "分析该人物近期行为记录：他/她参与了哪些事件、与谁有关系、关系状态如何变化。",
    "RELATIONSHIP_ANALYSIS": "分析该关系的状态与变化：关系类型、方向、状态历史、共同参与的事件。",
    "EVENT_ANALYSIS": "分析该单一事件及其证据：发生了什么、有哪些证据支持、证据是否充分。",
}


@dataclass(frozen=True)
class PromptBuilder:
    """Builds the system / context / user messages for one analysis run."""

    extra_instructions: str | None = None

    def system_message(self) -> ChatMessage:
        content = SYSTEM_PROMPT
        if self.extra_instructions:
            content += "\n\n附加要求：\n" + self.extra_instructions.strip()
        return ChatMessage(ROLE_SYSTEM, content)

    def context_message(self, snapshot: ContextSnapshot) -> ChatMessage:
        return ChatMessage(ROLE_USER, "以下是本次分析可用的全部上下文（来自本地数据库）：\n\n"
                                      + snapshot.rendered_context)

    def request_message(self, *, target_type: str, analysis_type: str, time_scope: str,
                        include_evidence: bool) -> ChatMessage:
        goal = ANALYSIS_GOAL.get(analysis_type, "分析给定目标。")
        scope_line = f"时间范围：{time_scope}；证据原文：{'已包含' if include_evidence else '未包含（只有元数据）'}。"
        return ChatMessage(ROLE_USER,
                           f"分析目标类型：{target_type}\n分析任务：{goal}\n{scope_line}\n\n"
                           + OUTPUT_TEMPLATE)

    def build(self, snapshot: ContextSnapshot) -> tuple[ChatMessage, ...]:
        include_evidence = bool(snapshot.scope.get("include", {}).get("evidence"))
        scope = snapshot.scope.get("time_window", {}).get("scope", "ALL")
        return (self.system_message(),
                self.context_message(snapshot),
                self.request_message(target_type=snapshot.target_type,
                                     analysis_type=snapshot.analysis_type, time_scope=scope,
                                     include_evidence=include_evidence))


def split_result_sections(result: str) -> dict:
    """Best-effort split of a plain-text answer into the four legacy sections.

    Kept for stored answers from before Phase 13.1; new answers are parsed by
    ``app.services.analysis_result_parser.parse_analysis_result``.
    """
    return split_legacy_sections(result or "")
