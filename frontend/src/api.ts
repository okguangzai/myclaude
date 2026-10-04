export const apiBase = import.meta.env.BASE_URL.replace(/\/$/, "") + "/api";
export async function api<T = any>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(apiBase + path, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `请求失败 (${response.status})`,
    );
  }
  return response.json();
}
export function json(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}
export async function streamChat(
  body: unknown,
  signal: AbortSignal,
  receive: (event: any) => void,
) {
  const response = await fetch(apiBase + "/chat", { ...json("POST", body), signal });
  if (!response.ok) {
    const data = await response.json();
    throw new Error(data.detail || "无法启动对话");
  }
  const reader = response.body!.getReader(),
    decoder = new TextDecoder();
  let buffer = "",
    ended = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        for (const line of frame.split("\n"))
          if (line.startsWith("data: ")) {
            const event = JSON.parse(line.slice(6));
            if (event.type === "run_end") ended = true;
            receive(event);
          }
      }
    }
    if (!ended) throw new Error("连接已断开，请查看运行记录确认状态");
  } finally {
    reader.releaseLock();
  }
}
