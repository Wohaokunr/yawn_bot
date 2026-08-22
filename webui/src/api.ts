export interface ApiEnvelope<T> {
  data: T;
  meta: Record<string, unknown> & {
    page?: number;
    pageSize?: number;
    total?: number;
  };
}

interface ApiFailure {
  error?: { code?: string; message?: string; fields?: Record<string, string> };
}

export class ApiError extends Error {
  status: number;
  fields: Record<string, string>;

  constructor(status: number, message: string, fields: Record<string, string> = {}) {
    super(message);
    this.status = status;
    this.fields = fields;
  }
}

let csrfToken = "";

export function setCsrfToken(value: string): void {
  csrfToken = value;
}

export function getCsrfToken(): string {
  return csrfToken;
}

export async function api<T>(
  path: string,
  init: RequestInit = {},
): Promise<ApiEnvelope<T>> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(`/webui/api/v1${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  const payload = (await response.json().catch(() => ({}))) as ApiEnvelope<T> & ApiFailure;
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("yawnbot-auth-lost"));
    throw new ApiError(
      response.status,
      payload.error?.message ?? `请求失败 (${response.status})`,
      payload.error?.fields,
    );
  }
  return payload;
}

export function openStatusStream(
  onMessage: (payload: { type: string; data?: unknown }) => void,
  onState: (state: "connecting" | "open" | "closed") => void,
): () => void {
  let stopped = false;
  let socket: WebSocket | null = null;
  let timer = 0;
  let attempt = 0;

  const connect = () => {
    if (stopped) return;
    onState("connecting");
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${window.location.host}/webui/api/v1/stream`);
    socket.onopen = () => {
      attempt = 0;
      onState("open");
    };
    socket.onmessage = (event) => {
      try {
        onMessage(JSON.parse(String(event.data)) as { type: string; data?: unknown });
      } catch {
        // Ignore malformed server frames and retain the authenticated connection.
      }
    };
    socket.onclose = (event) => {
      onState("closed");
      if (event.code === 4401) {
        window.dispatchEvent(new Event("yawnbot-auth-lost"));
        return;
      }
      if (!stopped) {
        const delay = Math.min(30_000, 1_000 * 2 ** attempt++);
        timer = window.setTimeout(connect, delay);
      }
    };
  };
  connect();
  return () => {
    stopped = true;
    window.clearTimeout(timer);
    socket?.close();
  };
}
