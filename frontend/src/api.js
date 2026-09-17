/**
 * 读取 SSE 流，按 data 行回调，直到 done / need_confirm / error。
 *
 * 参数:
 *   url: SSE 接口
 *   body: JSON 请求体
 *   onEvent: 每个事件的回调
 * 返回:
 *   终止事件对象
 */
async function readSse(url, body, onEvent) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let data = {};
    try {
      data = await res.json();
    } catch {
      data = {};
    }
    throw new Error(data.error || "请求失败");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop();
    for (const part of parts) {
      const line = part.split("\n").find((item) => item.startsWith("data: "));
      if (!line) {
        continue;
      }
      const event = JSON.parse(line.slice(6));
      onEvent(event);
      if (event.type === "done" || event.type === "need_confirm" || event.type === "error") {
        return event;
      }
    }
  }
  throw new Error("流式响应意外结束");
}

/**
 * 提交故障现象，走 SSE 诊断。
 *
 * 参数:
 *   issue: 故障描述
 *   threadId: 可选会话 id
 *   onEvent: SSE 回调
 * 返回:
 *   终止事件
 */
export function diagnoseStream(issue, threadId, onEvent) {
  return readSse("/api/diagnose/stream", { issue, thread_id: threadId || "" }, onEvent);
}

/**
 * 对高风险操作做人工确认，走 SSE 续跑。
 *
 * 参数:
 *   threadId: 会话 id
 *   approved: 是否批准
 *   onEvent: SSE 回调
 * 返回:
 *   终止事件
 */
export function confirmDiagnoseStream(threadId, approved, onEvent) {
  return readSse("/api/diagnose/confirm/stream", { thread_id: threadId, approved }, onEvent);
}
