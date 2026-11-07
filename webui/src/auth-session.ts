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
  /** 登录时勾了“自动登录”（长效 Cookie） */
  remember?: boolean;
  /** 自动登录保持的天数（WEBUI_REMEMBER_DAYS） */
  rememberDays?: number;
  /** 刚登录时才有：这次是否新地点、上一次登录（只有 QQ 个人账号） */
  login?: LoginInfo;
}

export interface LoginRecord {
  at: string | null;
  device: string;
  location: string;
  method: string;
  methodLabel: string;
  newLocation: boolean;
  remember: boolean;
}

export interface LoginInfo {
  newLocation: boolean;
  location: string;
  device: string;
  activeCount: number;
  previous: LoginRecord | null;
}
