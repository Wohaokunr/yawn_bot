// 网页搜索页的纯函数：状态文案、设置范围与校验、用量描述。
import type { WebSearchRecentCall, WebSearchSettings, WebSearchState } from "./types";

export const WEB_STATE_META: Record<WebSearchState, { label: string; tone: "ok" | "warn" | "off"; hint: string }> = {
  ready: { label: "可用", tone: "ok", hint: "Agent 遇到时效性问题会先联网搜索再回答" },
  no_key: { label: "未配置密钥", tone: "off", hint: "在「环境配置 → Agent 网页搜索」填 AGENT_WEB_SEARCH_API_KEY，或用 DeepSeek 作为模型提供商" },
  paused: { label: "已暂停", tone: "warn", hint: "总开关关闭或今日预算为 0，Agent 发现不到搜索工具" },
};

export const RECENT_STATUS_META: Record<WebSearchRecentCall["status"], { label: string; color?: string }> = {
  ok: { label: "成功", color: "green" },
  empty: { label: "无结果", color: "default" },
  cached: { label: "缓存", color: "blue" },
  error: { label: "失败", color: "red" },
};

export const ERROR_LABELS: Record<string, string> = {
  auth: "密钥无效",
  balance: "余额不足",
  rate_limited: "被限流",
  timeout: "超时",
  network: "连不上",
  upstream: "服务出错",
  forbidden: "站点拒绝抓取",
  not_found: "没取到正文",
  too_large: "页面太大",
  bad_response: "返回异常",
  bad_request: "参数不合法",
  no_key: "未配置密钥",
};

type NumericKey =
  | "memberDailyCalls"
  | "groupDailyCalls"
  | "globalDailyCalls"
  | "memberCooldownSeconds"
  | "maxCallsPerTurn"
  | "defaultCount"
  | "maxCount"
  | "maxFetchChars"
  | "cacheTtlMinutes";

export const WEB_SETTING_RANGES: Record<NumericKey, { label: string; min: number; max: number; unit: string }> = {
  memberDailyCalls: { label: "每人每天", min: 0, max: 1000, unit: "次" },
  groupDailyCalls: { label: "每群每天", min: 0, max: 10000, unit: "次" },
  globalDailyCalls: { label: "全站每天预算", min: 0, max: 100000, unit: "次" },
  memberCooldownSeconds: { label: "成员冷却", min: 0, max: 600, unit: "秒" },
  maxCallsPerTurn: { label: "每轮最多", min: 1, max: 20, unit: "次" },
  defaultCount: { label: "默认结果条数", min: 1, max: 20, unit: "条" },
  maxCount: { label: "最多结果条数", min: 1, max: 50, unit: "条" },
  maxFetchChars: { label: "单次读网页上限", min: 1000, max: 20000, unit: "字" },
  cacheTtlMinutes: { label: "结果缓存", min: 0, max: 1440, unit: "分钟" },
};

export const WEB_NUMERIC_KEYS = Object.keys(WEB_SETTING_RANGES) as NumericKey[];
export type WebNumericKey = NumericKey;

const GROUP_ID_RE = /^\d{4,12}$/;
const DOMAIN_RE = /^(?:[a-z0-9-]+\.)+[a-z0-9-]{2,}$/;

export function normalizeDomain(raw: string): string {
  let text = raw.trim().toLowerCase();
  if (text.includes("://")) text = text.split("://", 2)[1];
  text = text.split(/[/?#]/, 1)[0].split("@").pop()!.split(":", 1)[0].replace(/^\*\./, "").replace(/^www\./, "").replace(/\.+$/, "");
  return DOMAIN_RE.test(text) ? text : "";
}

export function parseDomainList(text: string): { domains: string[]; invalid: string[] } {
  const domains: string[] = [];
  const invalid: string[] = [];
  for (const raw of text.split(/[\s,，、;；]+/)) {
    if (!raw.trim()) continue;
    const domain = normalizeDomain(raw);
    if (!domain) invalid.push(raw.trim());
    else if (!domains.includes(domain)) domains.push(domain);
  }
  return { domains, invalid };
}

export function parseGroupIds(text: string): { ids: string[]; invalid: string[] } {
  const ids: string[] = [];
  const invalid: string[] = [];
  for (const raw of text.split(/[\s,，、;；]+/)) {
    const value = raw.trim();
    if (!value) continue;
    if (!GROUP_ID_RE.test(value)) invalid.push(value);
    else if (!ids.includes(value)) ids.push(value);
  }
  return { ids, invalid };
}

export function validateWebSettings(settings: WebSearchSettings): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const key of WEB_NUMERIC_KEYS) {
    const range = WEB_SETTING_RANGES[key];
    const value = settings[key];
    if (!Number.isInteger(value) || value < range.min || value > range.max) {
      errors[key] = `${range.label}需要在 ${range.min}–${range.max} 之间`;
    }
  }
  if (!errors.defaultCount && !errors.maxCount && settings.defaultCount > settings.maxCount) {
    errors.defaultCount = "默认条数不能大于最多条数";
  }
  if (settings.groupMode === "allowlist" && settings.allowedGroups.length === 0) {
    errors.allowedGroups = "白名单模式下至少填一个群号，否则所有群都不能搜索";
  }
  if (settings.blockedDomains.length > 200) errors.blockedDomains = "屏蔽域名最多 200 个";
  return errors;
}

export function webSettingsEqual(left: WebSearchSettings, right: WebSearchSettings): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function describeWebAccess(settings: WebSearchSettings): string {
  if (!settings.enabled) return "已关闭：所有群都不能联网搜索";
  const who = settings.adminOnly ? "群主/管理员" : "成员";
  const where = settings.groupMode === "allowlist"
    ? `${settings.allowedGroups.length} 个白名单群`
    : settings.blockedGroups.length
      ? `除 ${settings.blockedGroups.length} 个禁用群外的所有群`
      : "所有群";
  return `${where}的${who}都可用，每人每天 ${settings.memberDailyCalls} 次，全站每天最多 ${settings.globalDailyCalls} 次`;
}

export function formatYuan(value: number): string {
  if (!value) return "¥0";
  return value < 0.1 ? `¥${value.toFixed(3)}` : `¥${value.toFixed(2)}`;
}

export function monthlyEstimate(dailyCalls: number, pricePerCall: number): string {
  return formatYuan(dailyCalls * pricePerCall * 30);
}

export function cacheHitRate(stats: { hits: number; misses: number }): string {
  const total = stats.hits + stats.misses;
  if (!total) return "—";
  return `${Math.round((stats.hits / total) * 100)}%`;
}

export function scoreLabel(score: number | null, authority: number | null): { label: string; color?: string } | null {
  if (score == null && authority == null) return null;
  if ((score != null && score >= 2.2) || (authority != null && authority >= 3.5)) return { label: "高", color: "green" };
  if ((score != null && score >= 1.5) || (authority != null && authority >= 2.5)) return { label: "较高", color: "cyan" };
  if (score != null && score < 0.8) return { label: "低", color: "default" };
  return { label: "一般", color: "blue" };
}
