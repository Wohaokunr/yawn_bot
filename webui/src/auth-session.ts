export type SessionRole = "admin" | "guest" | "user";

export interface SessionCapabilities {
  adminConsole: boolean;
  adminWrite: boolean;
  realtimeAdminStream: boolean;
  guestGroupRead: boolean;
  /** QQ 个人账号的自助页面（我管理的群、我的私聊人设） */
  selfService?: boolean;
}

export interface AuthSessionData {
  authenticated: true;
  role: SessionRole;
  csrfToken: string;
  expiresAt: number;
  capabilities: SessionCapabilities;
  /** 个人账号会话的 QQ 号 */
  userId?: string;
}
