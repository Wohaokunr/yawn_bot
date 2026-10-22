import { useEffect, useRef } from "react";

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

// Alt+1…9、Alt+0 依次切换工作台的十个面板；输入框里不拦截，避免和输入法冲突。
export function useAgentTabShortcuts(onIndex: (index: number) => void): void {
  const handlerRef = useRef(onIndex);
  handlerRef.current = onIndex;
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (isEditable(event.target)) return;
      const match = /^Digit(\d)$/.exec(event.code);
      if (!match) return;
      const digit = Number(match[1]);
      event.preventDefault();
      handlerRef.current(digit === 0 ? 9 : digit - 1);
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);
}
