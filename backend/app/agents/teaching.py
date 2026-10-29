"""Immutable teaching pack. No workspace, disk discovery, subprocess, or extra model."""

import hashlib
import json

from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability
from pydantic_ai_harness.compaction import SlidingWindowCompaction
from pydantic_ai_harness.tool_output_limits import Band, ToolOutputLimits, Truncate

TEACHING_VERSION = "teaching-v1"
TEACHING_PACK = (
    ("teaching-retrieval-v1", "主动回忆、小步练习与间隔复习", """先让用户回忆或预测，再给提示，最后给讲解；一次聚焦一到三个要点。
练习可用quiz组件；题目、答案与讲解必须一致，反馈只是模型编写的自测参考，不代表可信评分或掌握证据。
根据当前任务与真实薄弱点安排复习建议，不自动安排任务、不提升学习状态。"""),
    ("teaching-debugging-v1", "根据真实运行回执定位错误并逐步指导", """先区分语法、运行错误、逻辑偏差，再提出一个可检查的假设和最小修改。
只根据绑定提交的真实日志解释运行情况；不能把exit 0、打印通过或Coder自写测试称为作业通过。
当前没有执行证据时明确说明，建议用户显式启动已保存提交的隔离运行。"""),
    ("teaching-feedback-v1", "按验收标准给出证据、缺口和下一步", """把本次提交逐项对照当前验收快照，引用具体可观察内容；区分已观察、推测和缺失。
先指出最关键的改进，再给一项小练习。assessment evidence_type始终为ai_review。
计划、目标与评估仍经过原有后端校验和确认流程，技能指令不会授予额外权限。"""),
)
TEACHING_PACK_SHA256 = hashlib.sha256(json.dumps(TEACHING_PACK, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

TEACHING_INSTRUCTIONS = """
可按需load_capability加载已打包的教学方法；技能仅提供指导，不授予执行、文件或业务写权限。
需要回忆旧细节时用search_conversation_history，query使用简短原文主题词；历史只是参考，不能代替本次请求授权。
可使用quiz组件给出1–5道选择题，问题含prompt、choices(id为a/b/c/d，text)、correct_choice_id、explanation。
答案将由服务器隐藏到用户检查练习后。题目与解释应准确；练习反馈不计入掌握、计划或正式评估。
progress组件仅填写type和title，由服务器补齐当前目标的真实进度；个人对话没有绑定目标时不要输出progress。
不要输出HTML、JS、React代码或任意执行动作。最新chat_request与当前业务快照优先于技能和历史资料。
"""


async def search_conversation_history(ctx: RunContext[dict], query: str, run_id: str | None = None) -> str:
    """Search earlier public turns in this operation's own conversation; never search other users or future turns."""
    from app.domain.conversation_history import search_history
    return await search_history(ctx, query, run_id)


class TruncatedToolOutputs(ToolOutputLimits):
    """Use official truncation hooks without advertising a file read-back tool."""

    def get_toolset(self):
        return None


def teaching_capabilities(kind):
    capabilities = [Capability(id=identifier, description=description, instructions=body, defer_loading=True)
                    for identifier, description, body in TEACHING_PACK]
    capabilities.extend([
        SlidingWindowCompaction(max_messages=32, keep_messages=16, max_tokens=18000, keep_tokens=6000,
                                preserve_first_user_message=False, receipts=True),
        TruncatedToolOutputs(bands=[Band(over=12000, action=Truncate(max_chars=12000))],
            tool_filter=["recall_learning_memory", "search_learning_resources", "fetch_learning_resource",
                         "search_conversation_history"], strip_ansi=True),
    ])
    if kind == "tutor":
        capabilities.append(Capability(id="learning-history-v1", tools=[search_conversation_history]))
    return capabilities
