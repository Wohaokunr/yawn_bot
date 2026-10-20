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
let sessionRole: string | null = null;

const CSRF_FAILURE_MESSAGE = "CSRF 校验失败";
const MUTATING_METHODS = ["POST", "PUT", "PATCH", "DELETE"];

export function setCsrfToken(value: string, role?: string | null): void {
  csrfToken = value;
  if (role !== undefined) sessionRole = role;
}

export function getCsrfToken(): string {
  return csrfToken;
}

async function request<T>(path: string, init: RequestInit, method: string): Promise<{ response: Response; payload: ApiEnvelope<T> & ApiFailure }> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (MUTATING_METHODS.includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(`/webui/api/v1${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  const payload = (await response.json().catch(() => ({}))) as ApiEnvelope<T> & ApiFailure;
  return { response, payload };
}

/**
 * CSRF token 按标签页缓存；在其他标签页重新登录后旧 token 失效。
 * 重新读取一次会话：角色未变则换新 token 重试，角色变了则整页重载回到正确界面。
 */
async function refreshCsrfToken(): Promise<boolean> {
  const { response, payload } = await request<{ role: string; csrfToken: string }>("/auth/session", {}, "GET");
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("yawnbot-auth-lost"));
    return false;
  }
  const previousRole = sessionRole;
  setCsrfToken(payload.data.csrfToken, payload.data.role);
  if (previousRole !== null && previousRole !== payload.data.role) {
    window.location.reload();
    return false;
  }
  return true;
}

export async function api<T>(
  path: string,
  init: RequestInit = {},
): Promise<ApiEnvelope<T>> {
  const method = (init.method ?? "GET").toUpperCase();
  let { response, payload } = await request<T>(path, init, method);
  if (
    response.status === 403
    && MUTATING_METHODS.includes(method)
    && payload.error?.message === CSRF_FAILURE_MESSAGE
    && await refreshCsrfToken().catch(() => false)
  ) {
    ({ response, payload } = await request<T>(path, init, method));
  }
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
      // 4403：会话有效但无实时流权限，重连也不会成功。
      if (event.code === 4403) return;
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
