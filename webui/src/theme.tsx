/* ── 主题（浅色 / 深色）唯一入口 ─────────────────────────────
   - 用户偏好：system（跟随系统）/ light / dark，存 localStorage `yawnbot.theme`；
   - 实际生效的主题写到 <html data-theme="light|dark">，CSS 变量据此切换
     （浅色取值在 theme-anime.css 的 :root，深色覆盖在 theme-dark.css）；
   - antd 的 ConfigProvider 也在这里，深色时换 darkAlgorithm 和对应 token。
   新页面只要颜色都走 CSS 变量或 antd token，就自动支持深色；
   TSX 里需要按主题分支时用 useThemeMode().resolved。 */
import { MoonOutlined, SunOutlined, DesktopOutlined } from "@ant-design/icons";
import { Button, ConfigProvider, Dropdown, Tooltip, theme } from "antd";
import type { MenuProps, ThemeConfig } from "antd";
import zhCN from "antd/locale/zh_CN";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type ThemeMode = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "yawnbot.theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

// 中英文都用系统 UI 字体：PingFang / HarmonyOS / 微软雅黑与 SF / Segoe 字重和字宽一致，
// 不再混一套圆体拉丁字体，数字和表格更整齐。
const FONT_FAMILY =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "HarmonyOS Sans SC", "MiSans", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif';

/** 两套主题共用的 token（形状、字体、按钮手感）。 */
const SHARED_TOKEN = {
  borderRadius: 8,
  borderRadiusLG: 12,
  fontFamily: FONT_FAMILY,
  fontSize: 14,
  controlHeight: 34,
};

const SHARED_COMPONENTS = {
  Card: { borderRadius: 12, borderRadiusLG: 12, headerFontSize: 15, headerHeight: 52, bodyPadding: 20 },
  Button: { borderRadius: 8, borderRadiusLG: 10, controlHeight: 34, controlHeightLG: 40, fontWeight: 500, primaryShadow: "none", defaultShadow: "none", dangerShadow: "none" },
  Menu: { itemBg: "transparent", itemBorderRadius: 8, activeBarBorderWidth: 0, itemMarginInline: 10, itemHeight: 38, groupTitleFontSize: 12 },
  Table: { headerSplitColor: "transparent", cellPaddingBlock: 12 },
  Tabs: { horizontalItemGutter: 24, titleFontSize: 14 },
};

/** 颜色与 theme-anime.css :root 保持一致。 */
export const LIGHT_THEME: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  token: {
    ...SHARED_TOKEN,
    colorPrimary: "#e14d84",
    colorInfo: "#3b82f6",
    colorSuccess: "#16a34a",
    colorWarning: "#d97706",
    colorError: "#dc2626",
    colorTextBase: "#1b1d23",
    colorLink: "#c93d72",
    colorLinkHover: "#e14d84",
    colorBgLayout: "transparent",
    colorTextSecondary: "#5d6270",
    colorTextTertiary: "#8d93a0",
    colorBorder: "#e3e5ea",
    colorBorderSecondary: "#eef0f3",
  },
  components: {
    Layout: { siderBg: "#ffffff", headerBg: "rgba(255, 255, 255, 0.88)", bodyBg: "transparent" },
    Menu: {
      ...SHARED_COMPONENTS.Menu,
      itemColor: "#5d6270",
      itemHoverBg: "#f2f3f6",
      itemHoverColor: "#1b1d23",
      itemSelectedBg: "#fdf1f5",
      itemSelectedColor: "#b8336a",
    },
    Card: { ...SHARED_COMPONENTS.Card, colorBorderSecondary: "#eef0f3", boxShadowTertiary: "0 1px 2px rgba(24, 20, 34, 0.04)" },
    Table: { ...SHARED_COMPONENTS.Table, headerBg: "#f8f9fb", headerColor: "#5d6270", rowHoverBg: "#f8f9fb", borderColor: "#eef0f3" },
    Tabs: { inkBarColor: "#e14d84", itemSelectedColor: "#b8336a", itemHoverColor: "#e14d84" },
    // colorBgLayout 设成了透明（页面底色由 CSS 给），Segmented 的轨道默认取它，会变成“看不见的分段”，这里显式给底色
    Segmented: { trackBg: "#eef0f3", itemSelectedBg: "#ffffff", itemColor: "#5d6270", itemHoverColor: "#1b1d23", itemHoverBg: "rgba(0, 0, 0, 0.04)" },
    Button: SHARED_COMPONENTS.Button,
  },
};

/** 颜色与 theme-dark.css 保持一致。 */
export const DARK_THEME: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    ...SHARED_TOKEN,
    colorPrimary: "#ec6a9a",
    colorInfo: "#60a5fa",
    colorSuccess: "#34c47a",
    colorWarning: "#f0a13a",
    colorError: "#f06a6a",
    colorTextBase: "#ecebf1",
    colorLink: "#f08cb1",
    colorLinkHover: "#f6a9c6",
    colorBgBase: "#141318",
    colorBgContainer: "#1d1c22",
    colorBgElevated: "#25242b",
    colorBgLayout: "transparent",
    colorBorder: "#34323c",
    colorBorderSecondary: "#2a2930",
  },
  components: {
    Layout: { siderBg: "#1a191f", headerBg: "rgba(20, 19, 24, 0.86)", bodyBg: "transparent" },
    Menu: {
      ...SHARED_COMPONENTS.Menu,
      itemColor: "#a9a6b4",
      itemHoverBg: "#26252c",
      itemHoverColor: "#ecebf1",
      itemSelectedBg: "#3a2230",
      itemSelectedColor: "#f6a9c6",
      darkItemBg: "transparent",
    },
    Card: { ...SHARED_COMPONENTS.Card, colorBorderSecondary: "#2a2930", boxShadowTertiary: "0 1px 2px rgba(0, 0, 0, 0.3)" },
    Table: { ...SHARED_COMPONENTS.Table, headerBg: "#222128", headerColor: "#a9a6b4", rowHoverBg: "#232229", borderColor: "#2a2930" },
    Tabs: { inkBarColor: "#ec6a9a", itemSelectedColor: "#f6a9c6", itemHoverColor: "#f08cb1" },
    Segmented: { trackBg: "#25242b", itemSelectedBg: "#3a3842", itemColor: "#a9a6b4", itemHoverColor: "#ecebf1", itemHoverBg: "rgba(255, 255, 255, 0.06)" },
    Button: SHARED_COMPONENTS.Button,
  },
};

export function readThemeMode(): ThemeMode {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

function writeThemeMode(mode: ThemeMode): void {
  try {
    if (mode === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    // 隐私模式下无法持久化，只在本次会话生效
  }
}

function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(DARK_QUERY).matches;
}

export function resolveTheme(mode: ThemeMode, prefersDark: boolean): ResolvedTheme {
  if (mode === "system") return prefersDark ? "dark" : "light";
  return mode;
}

/** 把生效主题写到 <html>，index.html 里的内联脚本在首帧前做同样的事以免闪白。 */
export function applyThemeToDocument(resolved: ResolvedTheme): void {
  const root = document.documentElement;
  root.dataset.theme = resolved;
  root.style.colorScheme = resolved;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", resolved === "dark" ? "#141318" : "#f5f6f8");
}

interface ThemeContextValue {
  mode: ThemeMode;
  resolved: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeContextValue>({ mode: "system", resolved: "light", setMode: () => undefined });

export function useThemeMode(): ThemeContextValue {
  return useContext(ThemeContext);
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(readThemeMode);
  const [prefersDark, setPrefersDark] = useState<boolean>(systemPrefersDark);
  const resolved = resolveTheme(mode, prefersDark);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(DARK_QUERY);
    const onChange = (event: MediaQueryListEvent) => setPrefersDark(event.matches);
    query.addEventListener?.("change", onChange);
    return () => query.removeEventListener?.("change", onChange);
  }, []);

  // 其他标签页切换主题时同步
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === THEME_STORAGE_KEY) setModeState(readThemeMode());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    applyThemeToDocument(resolved);
  }, [resolved]);

  const setMode = useCallback((next: ThemeMode) => {
    writeThemeMode(next);
    setModeState(next);
  }, []);

  const value = useMemo(() => ({ mode, resolved, setMode }), [mode, resolved, setMode]);

  return (
    <ThemeContext.Provider value={value}>
      {/* button.autoInsertSpace=false：antd 默认会把两个汉字的按钮写成“刷 新”“保 存” */}
      <ConfigProvider locale={zhCN} theme={resolved === "dark" ? DARK_THEME : LIGHT_THEME} button={{ autoInsertSpace: false }}>
        {children}
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}

const MODE_LABELS: Record<ThemeMode, string> = { system: "跟随系统", light: "浅色", dark: "深色" };

/** 顶栏 / 登录页用的主题切换按钮：点开选 跟随系统 / 浅色 / 深色。 */
export function ThemeToggle({ className }: { className?: string }) {
  const { mode, resolved, setMode } = useThemeMode();
  const [menuOpen, setMenuOpen] = useState(false);
  const items: MenuProps["items"] = [
    { key: "system", icon: <DesktopOutlined />, label: MODE_LABELS.system },
    { key: "light", icon: <SunOutlined />, label: MODE_LABELS.light },
    { key: "dark", icon: <MoonOutlined />, label: MODE_LABELS.dark },
  ];
  const label = `外观：${MODE_LABELS[mode]}${mode === "system" ? `（当前${MODE_LABELS[resolved]}）` : ""}`;
  return (
    <Dropdown
      trigger={["click"]}
      open={menuOpen}
      onOpenChange={setMenuOpen}
      menu={{ items, selectable: true, selectedKeys: [mode], onClick: ({ key }) => { setMode(key as ThemeMode); setMenuOpen(false); } }}
    >
      <Tooltip title={label} open={menuOpen ? false : undefined}>
        <Button
          className={["theme-toggle", className].filter(Boolean).join(" ")}
          type="text"
          aria-label={label}
          icon={resolved === "dark" ? <MoonOutlined /> : <SunOutlined />}
        />
      </Tooltip>
    </Dropdown>
  );
}
