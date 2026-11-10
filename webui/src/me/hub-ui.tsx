// 「我的空间」3.0 各页共用：跟着账号走的网页偏好、提醒中心的数据、本机显示偏好，
// 以及热力图、迷你折线、关键词高亮这些小图形。颜色只用主题变量。
import { Tooltip } from "antd";
import { useEffect, useSyncExternalStore } from "react";
import { hubApi, type Inbox, type UiPrefs, type UiPrefsPatch } from "./me-hub-api";
import { heatLevel, highlightParts, sparkPoints, type Delta } from "./me-hub-meta";

// ── 一个极小的外部 store：不用包 Provider，哪个组件都能读 ───────────────

interface Store<T> { value: T | null; loading: boolean; listeners: Set<() => void> }

function createStore<T>(): Store<T> {
  return { value: null, loading: false, listeners: new Set() };
}

function emit<T>(store: Store<T>): void {
  for (const listener of store.listeners) listener();
}

function useStore<T>(store: Store<T>): T | null {
  return useSyncExternalStore(
    (listener) => { store.listeners.add(listener); return () => store.listeners.delete(listener); },
    () => store.value,
    () => store.value,
  );
}

const prefsStore = createStore<UiPrefs>();
const inboxStore = createStore<Inbox>();

export const ME_CHANGED_EVENT = "yawnbot-me-changed";

/** 写操作成功后喊一声，提醒中心和侧栏角标会跟着刷新 */
export function announceMeChanged(): void {
  window.dispatchEvent(new CustomEvent(ME_CHANGED_EVENT));
}

function loadPrefs(): void {
  if (prefsStore.loading) return;
  prefsStore.loading = true;
  hubApi.uiPrefs()
    .then((value) => { prefsStore.value = value; })
    .catch(() => undefined)
    .finally(() => { prefsStore.loading = false; emit(prefsStore); });
}

export function loadInbox(): void {
  if (inboxStore.loading) return;
  inboxStore.loading = true;
  hubApi.inbox()
    .then((value) => { inboxStore.value = value; })
    .catch(() => undefined)
    .finally(() => { inboxStore.loading = false; emit(inboxStore); });
}

/** 换账号、退出登录、测试之间清掉缓存 */
export function resetHubStores(): void {
  prefsStore.value = null;
  prefsStore.loading = false;
  inboxStore.value = null;
  inboxStore.loading = false;
  emit(prefsStore);
  emit(inboxStore);
}

/** 跟着账号走的网页偏好：先改本地再存服务器，存失败就退回去。 */
export function useUiPrefs(): { prefs: UiPrefs | null; save: (patch: UiPrefsPatch) => void } {
  const prefs = useStore(prefsStore);
  useEffect(() => {
    if (prefsStore.value === null) loadPrefs();
  }, []);
  const save = (patch: UiPrefsPatch): void => {
    const before = prefsStore.value;
    if (before) {
      prefsStore.value = { ...before, ...patch };
      emit(prefsStore);
    }
    hubApi.putUiPrefs(patch)
      .then((value) => { prefsStore.value = value; })
      .catch(() => { prefsStore.value = before; })
      .finally(() => emit(prefsStore));
  };
  return { prefs, save };
}

/** 提醒中心：进来时拉一次，之后每 5 分钟、切回标签页、有写操作时再拉。 */
export function useInbox(): { inbox: Inbox | null; reload: () => void } {
  const inbox = useStore(inboxStore);
  useEffect(() => {
    if (inboxStore.value === null) loadInbox();
    let ticket: number | undefined;
    const soon = (): void => {
      window.clearTimeout(ticket);
      ticket = window.setTimeout(loadInbox, 400);
    };
    const onVisible = (): void => { if (document.visibilityState === "visible") soon(); };
    const timer = window.setInterval(loadInbox, 5 * 60_000);
    window.addEventListener(ME_CHANGED_EVENT, soon);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearTimeout(ticket);
      window.clearInterval(timer);
      window.removeEventListener(ME_CHANGED_EVENT, soon);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  return { inbox, reload: loadInbox };
}

// ── 本机显示偏好（只影响这台设备） ─────────────────────────────────────

export type Density = "cozy" | "compact";
export type FontScale = "s" | "m" | "l";
export interface LocalPrefs { density: Density; font: FontScale; calm: boolean; chatFont: FontScale }

const LOCAL_KEY = "yawnbot.me.display";
const LOCAL_DEFAULT: LocalPrefs = { density: "cozy", font: "m", calm: false, chatFont: "m" };
const localStore: Store<LocalPrefs> = createStore<LocalPrefs>();

export function readLocalPrefs(): LocalPrefs {
  try {
    const raw = JSON.parse(window.localStorage.getItem(LOCAL_KEY) ?? "{}") as Partial<LocalPrefs>;
    return {
      density: raw.density === "compact" ? "compact" : "cozy",
      font: raw.font === "s" || raw.font === "l" ? raw.font : "m",
      calm: raw.calm === true,
      chatFont: raw.chatFont === "s" || raw.chatFont === "l" ? raw.chatFont : "m",
    };
  } catch {
    return LOCAL_DEFAULT;
  }
}

function applyLocalPrefs(prefs: LocalPrefs): void {
  const root = document.documentElement;
  root.dataset.meDensity = prefs.density;
  root.dataset.meFont = prefs.font;
  if (prefs.calm) root.dataset.meCalm = "1";
  else delete root.dataset.meCalm;
}

export function clearLocalPrefsFromDom(): void {
  const root = document.documentElement;
  delete root.dataset.meDensity;
  delete root.dataset.meFont;
  delete root.dataset.meCalm;
}

export function useLocalPrefs(): { local: LocalPrefs; setLocal: (patch: Partial<LocalPrefs>) => void } {
  const stored = useStore(localStore);
  const local = stored ?? readLocalPrefs();
  useEffect(() => {
    if (localStore.value === null) {
      localStore.value = readLocalPrefs();
      emit(localStore);
    }
    applyLocalPrefs(localStore.value);
  }, [stored]);
  const setLocal = (patch: Partial<LocalPrefs>): void => {
    const next = { ...(localStore.value ?? readLocalPrefs()), ...patch };
    localStore.value = next;
    try {
      window.localStorage.setItem(LOCAL_KEY, JSON.stringify(next));
    } catch {
      // 隐私模式存不了也照样生效到本次会话
    }
    emit(localStore);
  };
  return { local, setLocal };
}

// ── 小图形 ─────────────────────────────────────────────────────────────

/** 按周排的热力格（列 = 周，行 = 周一到周日） */
export function Heatmap({ columns, max, label, tip }: {
  columns: Array<Array<{ day: string; value: number } | null>>;
  max: number;
  label: string;
  tip: (cell: { day: string; value: number }) => string;
}): React.JSX.Element {
  return <div className="me-heat" role="img" aria-label={label}>
    {columns.map((column, index) => (
      <div key={index} className="me-heat-col">
        {column.map((cell, row) => (cell === null
          ? <span key={row} className="me-heat-cell is-blank" />
          : <Tooltip key={row} title={tip(cell)}><span className={`me-heat-cell is-l${heatLevel(cell.value, max)}`} /></Tooltip>))}
      </div>
    ))}
  </div>;
}

export function HeatLegend({ less = "少", more = "多" }: { less?: string; more?: string }): React.JSX.Element {
  return <span className="me-heat-legend" aria-hidden="true">
    {less}{[0, 1, 2, 3, 4].map((level) => <span key={level} className={`me-heat-cell is-l${level}`} />)}{more}
  </span>;
}

export function Spark({ values, width = 120, height = 32, label }: { values: number[]; width?: number; height?: number; label: string }): React.JSX.Element {
  return <svg className="me-spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
    <polyline points={sparkPoints(values, width, height)} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
  </svg>;
}

/** 一排等宽的柱子；values 里的 null 画成空槽 */
export function Columns({ values, labels, max, label, tip, tone }: {
  values: Array<number | null>;
  labels?: string[];
  max?: number;
  label: string;
  tip?: (value: number | null, index: number) => string;
  tone?: (value: number | null, index: number) => string;
}): React.JSX.Element {
  const top = max ?? Math.max(1, ...values.map((value) => value ?? 0));
  return <div className="me-cols" role="img" aria-label={label}>
    {values.map((value, index) => (
      <Tooltip key={index} title={tip ? tip(value, index) : String(value ?? "—")}>
        <span className="me-col">
          <span className={`me-col-bar${value ? "" : " is-empty"}${tone ? ` ${tone(value, index)}` : ""}`} style={{ height: value ? `${Math.max(6, Math.round((value / top) * 100))}%` : "4%" }} />
          {labels && <span className="me-col-label">{labels[index]}</span>}
        </span>
      </Tooltip>
    ))}
  </div>;
}

export function Highlight({ text, needle }: { text: string; needle: string }): React.JSX.Element {
  return <>{highlightParts(text, needle).map((part, index) => (part.hit ? <mark key={index} className="me-mark">{part.text}</mark> : <span key={index}>{part.text}</span>))}</>;
}

export function DeltaNote({ delta }: { delta: Delta }): React.JSX.Element {
  return <span className={`me-delta-note is-${delta.direction}`}>{delta.direction === "up" ? "▲ " : delta.direction === "down" ? "▼ " : ""}{delta.label}</span>;
}

/** 下载一段文本成文件（导出 Markdown 用） */
export function downloadText(filename: string, text: string, type = "text/markdown;charset=utf-8"): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
