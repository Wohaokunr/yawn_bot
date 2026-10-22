import { ApiOutlined, BugOutlined, SafetyOutlined, SearchOutlined, TeamOutlined } from "@ant-design/icons";
import { Input, Modal, Spin, Typography } from "antd";
import type { InputRef } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { NavEntry } from "./nav-config";
import type { GroupSummary } from "./types";

const { Text } = Typography;

export interface PaletteItem {
  id: string;
  to: string;
  title: string;
  hint: string;
  icon: React.ReactNode;
  section: string;
}

function matches(entry: NavEntry, query: string): boolean {
  const haystack = `${entry.label} ${entry.group} ${entry.description} ${entry.keywords ?? ""} ${entry.key}`.toLowerCase();
  return query.split(/\s+/).every((part) => haystack.includes(part));
}

// 群组结果：主动作进 Agent 工作台，另附直达功能权限与调试台
function groupItems(rows: GroupSummary[]): PaletteItem[] {
  return rows.flatMap((row) => {
    const name = row.groupName || "未命名群";
    return [
      { id: `agent-${row.groupId}`, to: `/agent/${row.groupId}`, title: name, hint: `${row.groupId} · Agent 工作台`, icon: <ApiOutlined />, section: "群组" },
      { id: `access-${row.groupId}`, to: `/groups/${row.groupId}`, title: name, hint: `${row.groupId} · 功能权限`, icon: <SafetyOutlined />, section: "群组" },
      { id: `debug-${row.groupId}`, to: `/agent/${row.groupId}?tab=debug`, title: name, hint: `${row.groupId} · 调试台`, icon: <BugOutlined />, section: "群组" },
    ];
  });
}

export function CommandPalette({
  open,
  entries,
  searchGroups,
  onClose,
  onNavigate,
}: {
  open: boolean;
  entries: NavEntry[];
  searchGroups: boolean;
  onClose: () => void;
  onNavigate: (to: string) => void;
}): React.JSX.Element {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [groups, setGroups] = useState<GroupSummary[]>([]);
  const [loadingGroups, setLoadingGroups] = useState(false);
  const inputRef = useRef<InputRef>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const normalized = query.trim().toLowerCase();

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    setGroups([]);
  }, [open]);

  // 输入停顿 200ms 再查群，避免每个按键都打一次接口
  useEffect(() => {
    if (!open || !searchGroups || !normalized) {
      setGroups([]);
      setLoadingGroups(false);
      return undefined;
    }
    const controller = new AbortController();
    setLoadingGroups(true);
    const timer = window.setTimeout(() => {
      api<GroupSummary[]>(`/groups?page=1&pageSize=5&search=${encodeURIComponent(normalized)}`, { signal: controller.signal })
        .then((response) => setGroups(response.data))
        .catch(() => setGroups([]))
        .finally(() => {
          if (!controller.signal.aborted) setLoadingGroups(false);
        });
    }, 200);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [normalized, open, searchGroups]);

  const items = useMemo<PaletteItem[]>(() => {
    const pages = entries
      .filter((entry) => !normalized || matches(entry, normalized))
      .map((entry) => ({
        id: `page-${entry.key}`,
        to: entry.key,
        title: entry.label,
        hint: entry.description,
        icon: entry.icon,
        section: "页面",
      }));
    return [...pages, ...groupItems(groups)];
  }, [entries, groups, normalized]);

  useEffect(() => {
    setActive((current) => Math.min(current, Math.max(items.length - 1, 0)));
  }, [items.length]);

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${active}"]`)
      ?.scrollIntoView?.({ block: "nearest" });
  }, [active]);

  const choose = (item: PaletteItem | undefined) => {
    if (!item) return;
    onClose();
    onNavigate(item.to);
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => (items.length ? (current + 1) % items.length : 0));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => (items.length ? (current - 1 + items.length) % items.length : 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(items[active]);
    }
  };

  let lastSection = "";
  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      closable={false}
      width={560}
      className="command-palette"
      afterOpenChange={(visible) => {
        if (visible) inputRef.current?.focus();
      }}
      destroyOnHidden
    >
      <Input
        ref={inputRef}
        size="large"
        variant="borderless"
        prefix={<SearchOutlined />}
        suffix={loadingGroups ? <Spin size="small" /> : <kbd className="palette-kbd">Esc</kbd>}
        placeholder={searchGroups ? "跳转到页面，或输入群名 / 群号" : "跳转到页面"}
        aria-label="搜索页面或群组"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setActive(0);
        }}
        onKeyDown={onKeyDown}
      />
      <div className="palette-list" ref={listRef} role="listbox" aria-label="搜索结果">
        {items.length === 0 && !loadingGroups && (
          <div className="palette-empty">
            <TeamOutlined />
            <Text type="secondary">没有找到匹配的页面或群组</Text>
          </div>
        )}
        {items.map((item, index) => {
          const header = item.section !== lastSection ? item.section : "";
          lastSection = item.section;
          return (
            <div key={item.id}>
              {header && <div className="palette-section">{header}</div>}
              <button
                type="button"
                role="option"
                aria-selected={index === active}
                data-index={index}
                className={`palette-item${index === active ? " is-active" : ""}`}
                onMouseMove={() => setActive(index)}
                onClick={() => choose(item)}
              >
                <span className="palette-item-icon">{item.icon}</span>
                <span className="palette-item-text">
                  <span className="palette-item-title">{item.title}</span>
                  <span className="palette-item-hint">{item.hint}</span>
                </span>
                {index === active && <kbd className="palette-kbd">↵</kbd>}
              </button>
            </div>
          );
        })}
      </div>
      <div className="palette-footer">
        <span><kbd className="palette-kbd">↑</kbd><kbd className="palette-kbd">↓</kbd> 选择</span>
        <span><kbd className="palette-kbd">↵</kbd> 打开</span>
        <span className="palette-footer-hint"><kbd className="palette-kbd">Ctrl</kbd><kbd className="palette-kbd">K</kbd> 随时打开</span>
      </div>
    </Modal>
  );
}
