// 个人账号页面的纯逻辑：密码规则（镜像后端 webui_accounts.password_problem）、
// 人设差异（只提交改了的字段）、群角色文案。
import type { PersonaCard } from "../types";

export const PASSWORD_MIN = 8;
export const PASSWORD_MAX = 64;

const COMMON = new Set([
  "12345678", "123456789", "1234567890", "87654321", "11111111", "00000000", "88888888", "66666666",
  "password", "password1", "passw0rd", "qwertyui", "qwerty123", "qwe123456", "abc12345", "abcd1234",
  "a1234567", "a12345678", "aa123456", "1qaz2wsx", "iloveyou", "admin123", "yawnbot1", "yawnbot123",
  "woaini520", "woaini1314",
]);

/** 不合格时返回原因（不含密码本身），合格返回 null。和后端同一套规则，后端仍会再校验。 */
export function passwordProblem(password: string, qq?: string): string | null {
  if (password.length < PASSWORD_MIN) return `密码至少 ${PASSWORD_MIN} 位`;
  if (password.length > PASSWORD_MAX) return `密码最多 ${PASSWORD_MAX} 位`;
  if (/\s/.test(password)) return "密码里不能有空格或换行";
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(password)) return "密码里有不可见字符";
  if (new Set(password).size < 4) return "密码太简单了，别全用同几个字符";
  const classes = [/[A-Za-z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((pattern) => pattern.test(password)).length;
  if (classes < 2) return "密码至少要混用字母、数字、符号里的两种";
  if (COMMON.has(password.toLowerCase())) return "这个密码太常见了，换一个";
  if (qq && password.includes(qq)) return "密码里别带自己的 QQ 号";
  return null;
}

export type PasswordStrength = { score: 0 | 1 | 2 | 3; label: string };

export function passwordStrength(password: string): PasswordStrength {
  if (!password) return { score: 0, label: "" };
  let score = 0;
  if (password.length >= 12) score += 1;
  if (/[A-Za-z]/.test(password) && /[0-9]/.test(password)) score += 1;
  if (/[^A-Za-z0-9]/.test(password)) score += 1;
  const labels = ["偏弱", "一般", "不错", "很强"] as const;
  const clamped = Math.min(3, score) as 0 | 1 | 2 | 3;
  return { score: clamped, label: labels[clamped] };
}

export function normalizeSetupCode(raw: string): string {
  return raw.toUpperCase().replace(/[\s-]/g, "");
}

export function isQqNumber(raw: string): boolean {
  return /^[1-9][0-9]{4,11}$/.test(raw.trim());
}

export const GROUP_ROLE_LABEL: Record<string, string> = { owner: "群主", admin: "管理员", member: "成员" };

export function groupRoleLabel(role: string): string {
  return GROUP_ROLE_LABEL[role] ?? role;
}

/** PersonaCard 的 camelCase 字段 → 后端 snake_case 字段名 */
export function personaFieldKey(camel: string): string {
  return camel.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
}

const SKIP = new Set(["presetId"]);

/**
 * 只把改了的字段挑出来；不在“本人能改”名单里的改动单独列出，提交前就提示。
 * allowed 的键是 snake_case（和策略 selfFields 一致）。
 */
export function personaDiff(
  before: PersonaCard,
  after: PersonaCard,
  allowed: Record<string, string>,
): { fields: Record<string, unknown>; blocked: string[]; changed: string[] } {
  const fields: Record<string, unknown> = {};
  const blocked: string[] = [];
  const changed: string[] = [];
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  for (const key of keys) {
    if (SKIP.has(key)) continue;
    const a = (before as unknown as Record<string, unknown>)[key];
    const b = (after as unknown as Record<string, unknown>)[key];
    if (JSON.stringify(a) === JSON.stringify(b)) continue;
    const snake = personaFieldKey(key);
    if (snake in allowed) {
      fields[key] = b;
      changed.push(allowed[snake]);
    } else {
      blocked.push(snake);
    }
  }
  return { fields, blocked, changed };
}

export function formatQqTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
}
