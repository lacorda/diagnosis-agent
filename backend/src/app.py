# 故障诊断 HTTP：提交现象、人工确认高风险操作、SSE 推送规划/工具/评审。

import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

from graph import (
    build_diagnosis_graph,
    create_llm,
    get_interrupt_payload,
    initial_state,
    public_state,
    resume_command,
)
from safety import setup_langsmith
from tools import all_function_calling_schemas

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT_DIR / ".env")

PORT = int(os.environ.get("PORT") or 3001)
FRONTEND_ORIGIN = "http://127.0.0.1:5174"

setup_langsmith()
llm = create_llm()
graph = build_diagnosis_graph(llm)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.json.ensure_ascii = False
CORS(app, origins=[FRONTEND_ORIGIN])


def thread_config(thread_id):
    """组装 LangGraph 运行配置。

    参数:
        thread_id: 会话 id
    返回:
        runnable config
    """
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 20,
    }


def write_event(payload):
    """把 SSE 事件写成 data 行。

    参数:
        payload: 事件对象
    返回:
        SSE 文本
    """
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def interrupt_value(item):
    """取出 interrupt 对象上的 value。

    参数:
        item: Interrupt 或原始值
    返回:
        给前端的中断内容
    """
    return getattr(item, "value", item)


def update_to_event(event):
    """把图更新转成前端可消费的 SSE 事件。

    参数:
        event: stream_mode=updates 的单步更新
    返回:
        事件对象或 None
    """
    if "planner" in event:
        planner = event["planner"] or {}
        return {"type": "plan", "pendingTool": planner.get("pendingTool") or {}, "step": planner.get("step")}
    if "executor" in event:
        traces = (event["executor"] or {}).get("traces") or []
        return {"type": "tool", "trace": traces[-1] if traces else None, "traces": traces}
    if "reviewer" in event:
        reviewer = event["reviewer"] or {}
        return {
            "type": "review",
            "enough": bool(reviewer.get("enough")),
            "answer": reviewer.get("answer") or "",
        }
    if "rejected" in event:
        return {"type": "rejected", "answer": (event["rejected"] or {}).get("answer") or ""}
    if "__interrupt__" in event:
        item = event["__interrupt__"][0]
        return {"type": "need_confirm", "interrupt": interrupt_value(item)}
    return None


def run_graph(input_data, config, on_event=None):
    """跑完一轮图并收集最终响应。

    参数:
        input_data: 初始状态或 Command
        config: thread config
        on_event: 可选，流式回调
    返回:
        给 HTTP 的 JSON
    """
    for event in graph.stream(input_data, config, stream_mode="updates"):
        mapped = update_to_event(event)
        if mapped and on_event:
            on_event(mapped)
    snapshot = graph.get_state(config)
    interrupt_payload = get_interrupt_payload(graph, config)
    body = {
        "thread_id": config["configurable"]["thread_id"],
        **public_state(snapshot.values),
    }
    if interrupt_payload:
        return {"status": "need_confirm", "interrupt": interrupt_payload, **body}
    return {"status": "done", **body}


def sse_headers():
    """SSE 响应头。

    返回:
        Flask 响应头字典
    """
    return {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def stream_run(input_data, config):
    """执行图并把过程事件与终态写成 SSE。

    参数:
        input_data: 初始状态或 Command
        config: thread config
    返回:
        SSE 文本生成器
    """
    try:
        for event in graph.stream(input_data, config, stream_mode="updates"):
            mapped = update_to_event(event)
            if mapped:
                yield write_event(mapped)
        snapshot = graph.get_state(config)
        interrupt_payload = get_interrupt_payload(graph, config)
        body = {
            "thread_id": config["configurable"]["thread_id"],
            **public_state(snapshot.values),
        }
        if interrupt_payload:
            result = {"status": "need_confirm", "interrupt": interrupt_payload, **body}
        else:
            result = {"status": "done", **body}
        yield write_event({"type": result["status"], **result})
    except Exception as exc:
        yield write_event({"type": "error", "error": str(exc)})


@app.get("/api/health")
def health():
    """健康检查。

    返回:
        {ok: true}
    """
    return jsonify({"ok": True})


@app.get("/api/tools/schema")
def tools_schema():
    """导出诊断工具的 Function-Calling Schema。

    返回:
        {tools: [...]}
    """
    return jsonify({"tools": all_function_calling_schemas()})


@app.post("/api/diagnose")
def diagnose():
    """提交故障现象，跑完一轮图后返回 JSON。"""
    try:
        body = request.get_json(silent=True) or {}
        issue = str(body.get("issue") or "").strip()
        if not issue:
            return jsonify({"error": "issue 不能为空"}), 400
        thread_id = str(body.get("thread_id") or "").strip() or str(uuid.uuid4())
        config = thread_config(thread_id)
        result = run_graph(initial_state(issue), config)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/diagnose/confirm")
def diagnose_confirm():
    """对高风险操作做人工确认后续跑。"""
    try:
        body = request.get_json(silent=True) or {}
        thread_id = str(body.get("thread_id") or "").strip()
        if not thread_id:
            return jsonify({"error": "thread_id 不能为空"}), 400
        if "approved" not in body:
            return jsonify({"error": "approved 不能为空"}), 400
        config = thread_config(thread_id)
        result = run_graph(resume_command(body["approved"]), config)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/diagnose/stream")
def diagnose_stream():
    """提交故障现象，SSE 推送规划/工具/评审。"""
    body = request.get_json(silent=True) or {}
    issue = str(body.get("issue") or "").strip()
    if not issue:
        return jsonify({"error": "issue 不能为空"}), 400
    thread_id = str(body.get("thread_id") or "").strip() or str(uuid.uuid4())
    config = thread_config(thread_id)
    return Response(
        stream_with_context(stream_run(initial_state(issue), config)),
        mimetype="text/event-stream; charset=utf-8",
        headers=sse_headers(),
    )


@app.post("/api/diagnose/confirm/stream")
def diagnose_confirm_stream():
    """对高风险操作做人工确认，SSE 续跑。"""
    body = request.get_json(silent=True) or {}
    thread_id = str(body.get("thread_id") or "").strip()
    if not thread_id:
        return jsonify({"error": "thread_id 不能为空"}), 400
    if "approved" not in body:
        return jsonify({"error": "approved 不能为空"}), 400
    config = thread_config(thread_id)
    return Response(
        stream_with_context(stream_run(resume_command(body["approved"]), config)),
        mimetype="text/event-stream; charset=utf-8",
        headers=sse_headers(),
    )


if __name__ == "__main__":
    print(f"diagnosis-agent backend http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
