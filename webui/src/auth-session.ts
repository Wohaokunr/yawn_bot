export type SessionRole = "admin" | "guest";

export interface SessionCapabilities {
  adminConsole: boolean;
  adminWrite: boolean;
  realtimeAdminStream: boolean;
  guestGroupRead: boolean;
}

export interface AuthSessionData {
  authenticated: true;
  role: SessionRole;
  csrfToken: string;
  expiresAt: number;
  capabilities: SessionCapabilities;
}
