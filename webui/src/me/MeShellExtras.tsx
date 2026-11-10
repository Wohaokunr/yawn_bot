// 「我的空间」外壳上的三件事：提醒铃铛、随手记、键盘快捷键。只给 QQ 个人账号挂载。
import { BellOutlined, CheckOutlined, PlusOutlined, QuestionCircleOutlined, RightOutlined } from "@ant-design/icons";
import { App as AntApp, Badge, Button, Empty, Input, Modal, Popover, Segmented, Switch, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { announceMeChanged, useInbox, useLocalPrefs, useUiPrefs } from "./hub-ui";
import type { Notice } from "./me-hub-api";
import { GO_KEYS, isTypingTarget, parseQuickTodo, SHORTCUTS, todayIso } from "./me-hub-meta";
import { spaceApi } from "./me-space-api";
import { MOOD_OPTIONS, PRIORITY_META } from "./me-space-meta";

import "./me-space.css";
import "./me-v3.css";

const { Text } = Typography;

export type CaptureKind = "todo" | "note" | "mood" | "countdown";
export const CAPTURE_EVENT = "yawnbot-me-capture";

/** 别的页面想打开随手记（比如日历上点某一天）时用 */
export function openCapture(detail: { kind?: CaptureKind; date?: string } = {}): void {
  window.dispatchEvent(new CustomEvent(CAPTURE_EVENT, { detail }));
}

const noticeKey = (notice: Notice, today: string): string => `${notice.id}@${today}`;

function NoticeList({ items, seen, today, onOpen }: { items: Notice[]; seen: Set<string>; today: string; onOpen: (notice: Notice) => void }): React.JSX.Element {
  if (items.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有要处理的事" />;
  return <ul className="me-notice-list">
    {items.map((notice) => (
      <li key={notice.id}>
        <button type="button" className={`me-notice is-${notice.tone}${seen.has(noticeKey(notice, today)) ? " is-seen" : ""}`} onClick={() => onOpen(notice)}>
          <span className="me-notice-dot" aria-hidden="true" />
          <span className="me-notice-main">
            <strong>{notice.title}</strong>
            {notice.detail && <span className="me-notice-detail">{notice.detail}</span>}
          </span>
          <RightOutlined className="me-card-arrow" />
        </button>
      </li>
    ))}
  </ul>;
}

export function NotificationBell({ onNavigate }: { onNavigate: (to: string) => void }): React.JSX.Element {
  const { inbox, reload } = useInbox();
  const { prefs, save } = useUiPrefs();
  const [open, setOpen] = useState(false);
  const today = (inbox?.now ?? "").slice(0, 10);
  const seen = useMemo(() => new Set(prefs?.seenNotices ?? []), [prefs]);
  const items = inbox?.items ?? [];
  const unseen = items.filter((item) => (item.tone === "danger" || item.tone === "warning") && !seen.has(noticeKey(item, today))).length;
  const markAll = (): void => save({ seenNotices: items.map((item) => noticeKey(item, today)).slice(0, 40) });
  return <Popover
    open={open}
    onOpenChange={(next) => { setOpen(next); if (next) reload(); }}
    trigger="click"
    placement="bottomRight"
    title={<span className="me-notice-head">等你处理{items.length > 0 && <Button type="link" size="small" icon={<CheckOutlined />} disabled={unseen === 0} onClick={markAll}>都知道了</Button>}</span>}
    content={<div className="me-notice-pop"><NoticeList items={items} seen={seen} today={today} onOpen={(notice) => { setOpen(false); onNavigate(notice.to); }} /></div>}
  >
    <Tooltip title="提醒">
      <Badge count={unseen} size="small" offset={[-4, 4]}>
        <Button type="text" icon={<BellOutlined />} aria-label={unseen ? `提醒，${unseen} 条没看` : "提醒"} className="me-head-btn" />
      </Badge>
    </Tooltip>
  </Popover>;
}

const KIND_OPTIONS: Array<{ value: CaptureKind; label: string }> = [
  { value: "todo", label: "待办" },
  { value: "note", label: "笔记" },
  { value: "mood", label: "心情" },
  { value: "countdown", label: "倒数日" },
];

export function QuickCapture({ open, initial, onClose }: { open: boolean; initial: { kind?: CaptureKind; date?: string }; onClose: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [kind, setKind] = useState<CaptureKind>("todo");
  const [text, setText] = useState("");
  const [date, setDate] = useState("");
  const [score, setScore] = useState<number | null>(null);
  const [keepOpen, setKeepOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const input = useRef<HTMLTextAreaElement | null>(null);
  const today = todayIso();
  const parsed = useMemo(() => parseQuickTodo(text, today), [text, today]);

  useEffect(() => {
    if (!open) return;
    setKind(initial.kind ?? "todo");
    setDate(initial.date ?? "");
    setText("");
    setScore(null);
  }, [open, initial]);

  const ready = kind === "mood" ? score !== null : kind === "countdown" ? Boolean(text.trim() && date) : Boolean(text.trim());
  const save = async (): Promise<void> => {
    if (!ready || saving) return;
    setSaving(true);
    try {
      if (kind === "todo") {
        const due = date || parsed.due;
        await spaceApi.addTodo({ content: parsed.content, priority: parsed.priority, due });
        message.success(`记下了${due ? `，${due === today ? "今天" : due.slice(5)} 到期` : ""}`);
      } else if (kind === "note") {
        await spaceApi.addNote(text.trim());
        message.success("笔记存好了");
      } else if (kind === "mood" && score !== null) {
        await spaceApi.logMood(score, text.trim());
        message.success("心情记下了");
      } else if (kind === "countdown") {
        await spaceApi.addCountdown(text.trim(), date, true);
        message.success("倒数日加好了");
      }
      announceMeChanged();
      setText("");
      setScore(null);
      if (keepOpen) input.current?.focus();
      else onClose();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };

  return <Modal
    open={open}
    title="随手记"
    onCancel={onClose}
    destroyOnHidden
    width={520}
    footer={<div className="me-capture-foot">
      <span className="me-inline-switch"><Switch size="small" checked={keepOpen} onChange={setKeepOpen} aria-label="连续记" /> 连续记</span>
      <Text type="secondary" className="me-hint">Ctrl+Enter 保存</Text>
      <span className="me-toolbar-spacer" />
      <Button onClick={onClose}>取消</Button>
      <Button type="primary" disabled={!ready} loading={saving} onClick={() => void save()}>保存</Button>
    </div>}
  >
    <div className="me-stack">
      <Segmented<CaptureKind> block value={kind} onChange={setKind} options={KIND_OPTIONS} aria-label="记什么" />
      {kind === "mood" && (
        <div className="me-mood-picker" role="radiogroup" aria-label="心情">
          {MOOD_OPTIONS.map((option) => (
            <button key={option.score} type="button" role="radio" aria-checked={score === option.score} className={`me-mood-option${score === option.score ? " is-active" : ""}`} onClick={() => setScore(option.score)}>
              <span className="me-mood-emoji" aria-hidden="true">{option.emoji}</span><span>{option.label}</span>
            </button>
          ))}
        </div>
      )}
      <Input.TextArea
        ref={(node) => { input.current = node?.resizableTextArea?.textArea ?? null; }}
        autoFocus
        autoSize={{ minRows: kind === "note" ? 3 : 1, maxRows: 8 }}
        maxLength={kind === "note" ? 300 : kind === "mood" ? 60 : kind === "countdown" ? 40 : 120}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey || (kind !== "note" && !event.shiftKey))) {
            event.preventDefault();
            void save();
          }
        }}
        placeholder={kind === "todo" ? "要做什么？试试：明天 交报告 !重要" : kind === "note" ? "随手记点什么" : kind === "mood" ? "想说一句吗？（可选）" : "什么日子？比如：期末考试"}
        aria-label="内容"
      />
      {(kind === "todo" || kind === "countdown") && (
        <div className="me-capture-row">
          <Input type="date" value={date} onChange={(event) => setDate(event.target.validity.badInput ? "" : event.target.value)} aria-label={kind === "todo" ? "截止日期（可选）" : "日期"} className="me-composer-date" />
          {kind === "todo" && !date && parsed.due && <Tag color="blue">识别到日期 {parsed.due}</Tag>}
          {kind === "todo" && parsed.priority !== "normal" && <Tag color={PRIORITY_META[parsed.priority].color}>{PRIORITY_META[parsed.priority].label}</Tag>}
          {kind === "todo" && text.trim() && parsed.content !== text.trim() && <Text type="secondary" className="me-hint">会记成：{parsed.content}</Text>}
        </div>
      )}
    </div>
  </Modal>;
}

export function ShortcutHelp({ open, onClose }: { open: boolean; onClose: () => void }): React.JSX.Element {
  return <Modal open={open} title="键盘快捷键" footer={null} onCancel={onClose} width={480}>
    <ul className="me-key-list">
      {SHORTCUTS.map((row) => <li key={row.keys}><kbd>{row.keys}</kbd><span>{row.what}</span></li>)}
    </ul>
    <Text type="secondary" className="me-hint">先按 G，再按：</Text>
    <div className="me-key-grid">
      {Object.entries(GO_KEYS).map(([key, target]) => <span key={key}><kbd>{key.toUpperCase()}</kbd> {target.label}</span>)}
    </div>
  </Modal>;
}

/** 顶栏右侧的工具 + 全局快捷键。 */
export function MeHeaderTools({ onNavigate }: { onNavigate: (to: string) => void }): React.JSX.Element {
  const [capture, setCapture] = useState<{ open: boolean; initial: { kind?: CaptureKind; date?: string } }>({ open: false, initial: {} });
  const [help, setHelp] = useState(false);
  const go = useRef(onNavigate);
  go.current = onNavigate;
  useLocalPrefs();

  useEffect(() => {
    let pendingGo = 0;
    const onKey = (event: KeyboardEvent): void => {
      if (event.ctrlKey || event.metaKey || event.altKey || isTypingTarget(event.target)) return;
      // 弹窗开着时不抢键
      if (document.querySelector(".ant-modal-wrap:not([style*='display: none']) .ant-modal")) return;
      const key = event.key.toLowerCase();
      if (pendingGo && Date.now() - pendingGo < 1500) {
        pendingGo = 0;
        const target = GO_KEYS[key];
        if (target) {
          event.preventDefault();
          go.current(target.to);
        }
        return;
      }
      if (key === "g") { pendingGo = Date.now(); return; }
      if (key === "n") { event.preventDefault(); setCapture({ open: true, initial: {} }); return; }
      if (key === "/") { event.preventDefault(); go.current("/me/search"); return; }
      if (event.key === "?") { event.preventDefault(); setHelp(true); }
    };
    const onCapture = (event: Event): void => {
      setCapture({ open: true, initial: (event as CustomEvent<{ kind?: CaptureKind; date?: string }>).detail ?? {} });
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener(CAPTURE_EVENT, onCapture);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(CAPTURE_EVENT, onCapture);
    };
  }, []);

  return <>
    <Tooltip title="随手记（N）">
      <Button type="text" icon={<PlusOutlined />} aria-label="随手记" className="me-head-btn" onClick={() => setCapture({ open: true, initial: {} })} />
    </Tooltip>
    <NotificationBell onNavigate={onNavigate} />
    <Tooltip title="快捷键（?）">
      <Button type="text" icon={<QuestionCircleOutlined />} aria-label="快捷键" className="me-head-btn me-head-help" onClick={() => setHelp(true)} />
    </Tooltip>
    <QuickCapture open={capture.open} initial={capture.initial} onClose={() => setCapture((current) => ({ ...current, open: false }))} />
    <ShortcutHelp open={help} onClose={() => setHelp(false)} />
  </>;
}
