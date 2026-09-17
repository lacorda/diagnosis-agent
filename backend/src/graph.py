# 故障诊断 LangGraph：Planner → Executor → Reviewer，含重试、死循环、HITL。

import json
import os
import re
import time
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from safety import INJECTION_SYSTEM, wrap_user_input
from tools import DIAGNOSIS_TOOLS, HIGH_RISK_TOOLS, TOOLS_BY_NAME

MAX_STEPS = 8
TOOL_RETRY = 3

PLANNER_PROMPT = """根据故障现象和已有观测，决定下一步。一次只调用一个工具。
工具串联：域名先 dns_tool，再用返回的 ip 调用 ping_tool；连通失败（status=timeout）先 interface_check_tool，再把 status 传给 log_analysis_tool。
restart_interface 仅在接口确认关闭且确需恢复时使用。
不要重复调用已经执行过的同一工具和同一参数。
证据足够、无需再探测时，不要调用工具，直接用自然语言说明可以结束。
"""

REVIEWER_PROMPT = """根据故障现象和工具观测，判断是否足够给出诊断结论。
只输出 JSON，不要 Markdown。
{"enough":true,"answer":"诊断结论与建议，必须包含根因、引用过的工具证据、处理建议"}
或
{"enough":false,"reason":"还缺什么证据"}
"""


class DiagnosisState(TypedDict):
    issue: str
    observations: list
    traces: list
    seenKeys: list
    step: int
    pendingTool: dict
    approved: bool
    answer: str
    enough: bool


def parse_json(text):
    """从模型输出里取出 JSON 对象。

    参数:
        text: 模型原始输出
    返回:
        解析后的对象
    """
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw)
    return json.loads(fenced.group(1) if fenced else raw)


def tool_key(name, args):
    """把工具名和参数做成死循环检测键。

    参数:
        name: 工具名
        args: 工具参数
    返回:
        稳定字符串键
    """
    return json.dumps({"name": name, "args": args or {}}, ensure_ascii=False, separators=(",", ":"))


def token_usage(message):
    """读取模型本轮 Token 消耗。

    参数:
        message: LLM 返回的 AIMessage
    返回:
        {inputTokens, outputTokens, totalTokens}
    """
    usage = getattr(message, "usage_metadata", None) or {}
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": usage.get("total_tokens") or (input_tokens + output_tokens),
    }


def run_tool(name, args):
    """执行单个工具，失败则有限次重试。

    参数:
        name: 工具名
        args: 工具参数
    返回:
        工具返回的字符串
    """
    selected = TOOLS_BY_NAME.get(name)
    if not selected:
        raise RuntimeError(f"未知工具: {name}")
    last_error = None
    for _ in range(TOOL_RETRY):
        try:
            return selected.invoke(args or {})
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"{name} 重试 {TOOL_RETRY} 次仍失败: {last_error}")


def create_llm():
    """创建诊断用的通义聊天模型。

    返回:
        ChatOpenAI（DashScope 兼容模式）
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("请设置环境变量 DASHSCOPE_API_KEY")
    return ChatOpenAI(
        model="qwen-plus",
        api_key=api_key,
        temperature=0,
        timeout=60,
        max_retries=2,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )


def build_diagnosis_graph(llm):
    """编译 Planner-Executor-Reviewer 图。

    参数:
        llm: ChatOpenAI
    返回:
        带 InMemorySaver 的编译图
    """
    bound_planner = llm.bind_tools(DIAGNOSIS_TOOLS)

    def planner_node(state):
        """根据现象和观测决定下一步工具。

        参数:
            state: 诊断状态
        返回:
            pendingTool 与 step
        """
        obs = "\n".join(state.get("observations") or []) or "（尚无观测）"
        started = time.time()
        response = bound_planner.invoke(
            [
                SystemMessage(content=f"{INJECTION_SYSTEM}\n{PLANNER_PROMPT}"),
                HumanMessage(content=f"故障现象: {wrap_user_input(state['issue'])}\n已有观测:\n{obs}"),
            ]
        )
        duration_ms = int((time.time() - started) * 1000)
        calls = response.tool_calls or []
        if not calls:
            return {"pendingTool": {}, "step": (state.get("step") or 0) + 1}
        first = calls[0]
        return {
            "pendingTool": {
                "name": first["name"],
                "args": first.get("args") or {},
                "durationMs": duration_ms,
                "tokens": token_usage(response),
            },
            "step": (state.get("step") or 0) + 1,
        }

    def route_after_planner(state):
        """Planner 之后分流到 HITL、执行器或评审。

        参数:
            state: 诊断状态
        返回:
            hitl / executor / reviewer
        """
        pending = state.get("pendingTool") or {}
        if not pending.get("name"):
            return "reviewer"
        if pending["name"] in HIGH_RISK_TOOLS:
            return "hitl"
        return "executor"

    def hitl_node(state):
        """高风险工具执行前中断，等待人工确认。

        参数:
            state: 诊断状态
        返回:
            approved 字段
        """
        pending = state["pendingTool"]
        decision = interrupt(
            {
                "tool": pending["name"],
                "args": pending["args"],
                "reason": "高风险操作需要人工确认",
            }
        )
        if isinstance(decision, dict):
            approved = bool(decision.get("approved"))
        else:
            approved = bool(decision)
        return {"approved": approved}

    def route_after_hitl(state):
        """人工确认通过则执行，否则直接给结论。

        参数:
            state: 诊断状态
        返回:
            executor 或 rejected
        """
        return "executor" if state.get("approved") else "rejected"

    def rejected_node(state):
        """用户拒绝高风险操作。

        参数:
            state: 诊断状态
        返回:
            最终答案
        """
        pending = state.get("pendingTool") or {}
        return {
            "enough": True,
            "answer": f"用户拒绝执行高风险操作 {pending.get('name') or ''}，诊断中止。",
            "pendingTool": {},
        }

    def executor_node(state):
        """执行当前工具，检测死循环，失败重试。

        参数:
            state: 诊断状态
        返回:
            新的观测与轨迹
        """
        pending = state.get("pendingTool") or {}
        name = pending.get("name")
        args = pending.get("args") or {}
        key = tool_key(name, args)
        seen = list(state.get("seenKeys") or [])
        if key in seen:
            raise RuntimeError(f"检测到死循环：重复调用 {name} {json.dumps(args, ensure_ascii=False)}")
        started = time.time()
        output = run_tool(name, args)
        duration_ms = int((time.time() - started) * 1000)
        seen.append(key)
        traces = list(state.get("traces") or [])
        traces.append({"name": name, "args": args, "output": output, "durationMs": duration_ms})
        observations = list(state.get("observations") or [])
        observations.append(f"{name}({json.dumps(args, ensure_ascii=False, separators=(',', ':'))}): {output}")
        return {
            "seenKeys": seen,
            "traces": traces,
            "observations": observations,
            "pendingTool": {},
            "approved": False,
        }

    def reviewer_node(state):
        """判断证据是否足够，足够则给出诊断结论。

        参数:
            state: 诊断状态
        返回:
            enough / answer
        """
        if state.get("enough"):
            return {}
        if (state.get("step") or 0) >= MAX_STEPS:
            obs = "\n".join(state.get("observations") or [])
            return {
                "enough": True,
                "answer": f"已达最大步数 {MAX_STEPS}，基于现有观测给出结论：\n{obs}",
            }
        obs = "\n".join(state.get("observations") or []) or "（尚无观测）"
        response = llm.invoke(
            [
                SystemMessage(content=f"{INJECTION_SYSTEM}\n{REVIEWER_PROMPT}"),
                HumanMessage(content=f"故障现象: {wrap_user_input(state['issue'])}\n工具观测:\n{obs}"),
            ]
        )
        parsed = parse_json(response.content)
        if parsed.get("enough"):
            answer = parsed.get("answer") or ""
            if not answer:
                raise RuntimeError("Reviewer 判定足够但未给出 answer")
            return {"enough": True, "answer": answer}
        return {"enough": False}

    def route_after_reviewer(state):
        """证据不足且未超步数则回到 Planner。

        参数:
            state: 诊断状态
        返回:
            planner 或 end
        """
        if state.get("enough") or (state.get("step") or 0) >= MAX_STEPS:
            return "end"
        return "planner"

    graph = StateGraph(DiagnosisState)
    graph.add_node("planner", planner_node)
    graph.add_node("hitl", hitl_node)
    graph.add_node("executor", executor_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("rejected", rejected_node)
    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {"hitl": "hitl", "executor": "executor", "reviewer": "reviewer"},
    )
    graph.add_conditional_edges(
        "hitl",
        route_after_hitl,
        {"executor": "executor", "rejected": "rejected"},
    )
    graph.add_edge("executor", "reviewer")
    graph.add_edge("rejected", END)
    graph.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {"planner": "planner", "end": END},
    )
    return graph.compile(checkpointer=InMemorySaver())


def initial_state(issue):
    """构造图的初始状态。

    参数:
        issue: 用户故障描述
    返回:
        DiagnosisState
    """
    return {
        "issue": issue,
        "observations": [],
        "traces": [],
        "seenKeys": [],
        "step": 0,
        "pendingTool": {},
        "approved": False,
        "answer": "",
        "enough": False,
    }


def get_interrupt_payload(graph, config):
    """从图快照取出 HITL 中断内容。

    参数:
        graph: 编译后的诊断图
        config: 含 thread_id 的 runnable config
    返回:
        interrupt 的 value；若未中断则 None
    """
    snapshot = graph.get_state(config)
    tasks = snapshot.tasks or ()
    for task in tasks:
        items = getattr(task, "interrupts", None) or ()
        if items:
            item = items[0]
            return getattr(item, "value", item)
    interrupts = getattr(snapshot, "interrupts", None) or ()
    if interrupts:
        item = interrupts[0]
        return getattr(item, "value", item)
    return None


def public_state(values=None):
    """挑给前端的字段。

    参数:
        values: 图状态
    返回:
        traces / answer / step / observations
    """
    values = values or {}
    return {
        "traces": values.get("traces") or [],
        "answer": values.get("answer") or "",
        "step": values.get("step") or 0,
        "observations": values.get("observations") or [],
    }


def resume_command(approved):
    """用 Command 恢复 HITL。

    参数:
        approved: 是否批准高风险操作
    返回:
        Command
    """
    return Command(resume={"approved": bool(approved)})
