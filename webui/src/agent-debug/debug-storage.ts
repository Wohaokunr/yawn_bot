// 调试台的本机偏好：收藏的 Trace、实验预设、轮询间隔。只是便利功能，读写失败一律静默降级。
import { useCallback, useState } from "react";

const PREFIX = "yawnbot.debug.";

export function readLocal<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(PREFIX + key);
    return raw == null ? fallback : JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function writeLocal(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    // 隐私模式或配额已满时放弃持久化，页面仍可正常使用。
  }
}

export function useLocalState<T>(key: string, fallback: T): [T, (value: T | ((current: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => readLocal(key, fallback));
  const update = useCallback((next: T | ((current: T) => T)) => {
    setValue((current) => {
      const resolved = typeof next === "function" ? (next as (current: T) => T)(current) : next;
      writeLocal(key, resolved);
      return resolved;
    });
  }, [key]);
  return [value, update];
}
