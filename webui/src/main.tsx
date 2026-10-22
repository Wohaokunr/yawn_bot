import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { BrowserRouter } from "react-router-dom";
import "@fontsource/zcool-kuaile/400.css";
import "@fontsource-variable/nunito/wght.css";
import App from "./App";
import { installGlassGlow } from "./glass";
import "./styles.css";
import "./interaction.css";
import "./layout.css";
import "./design-system.css";
import "./agent-workspace/agent-workspace.css";
import "./theme-anime.css";

installGlassGlow();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: "#e4558a",
          colorInfo: "#3b82f6",
          colorSuccess: "#16a34a",
          colorWarning: "#d97706",
          colorError: "#dc2626",
          colorTextBase: "#1f1d24",
          colorLink: "#d2427a",
          colorLinkHover: "#e4558a",
          colorBgLayout: "transparent",
          colorBorder: "#e7e6ec",
          colorBorderSecondary: "#f0eff3",
          borderRadius: 8,
          fontFamily:
            '"Nunito Variable", -apple-system, "Segoe UI", "PingFang SC", "HarmonyOS Sans SC", "MiSans", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
        },
        components: {
          Layout: { siderBg: "#ffffff", headerBg: "rgba(255, 255, 255, 0.88)", bodyBg: "transparent" },
          Menu: {
            itemBg: "transparent",
            itemColor: "#6c6977",
            itemHoverBg: "#f4f3f7",
            itemHoverColor: "#1f1d24",
            itemSelectedBg: "#fdf2f6",
            itemSelectedColor: "#bb3469",
            itemBorderRadius: 8,
            activeBarBorderWidth: 0,
            itemMarginInline: 10,
          },
          Card: {
            borderRadius: 14,
            borderRadiusLG: 14,
            colorBorderSecondary: "#f0eff3",
            boxShadowTertiary: "0 1px 2px rgba(24, 20, 34, 0.04)",
          },
          Table: {
            headerBg: "#fafafb",
            headerColor: "#6c6977",
            rowHoverBg: "#fafafb",
            borderColor: "#f0eff3",
            headerSplitColor: "transparent",
          },
          Tabs: { inkBarColor: "#e4558a", itemSelectedColor: "#bb3469", itemHoverColor: "#e4558a" },
          Button: { borderRadius: 8, borderRadiusLG: 10, controlHeight: 34, controlHeightLG: 42, fontWeight: 600, primaryShadow: "none", defaultShadow: "none" },
        },
      }}
    >
      <BrowserRouter basename="/webui">
        <App />
      </BrowserRouter>
    </ConfigProvider>
  </React.StrictMode>,
);
