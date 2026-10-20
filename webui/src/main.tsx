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
import "./theme-anime.css";

installGlassGlow();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: "#f2608d",
          colorInfo: "#5b9df6",
          colorSuccess: "#34c896",
          colorWarning: "#f6a94a",
          colorError: "#ff6b7d",
          colorTextBase: "#3d2c37",
          colorLink: "#e0457b",
          colorLinkHover: "#f2608d",
          colorBgLayout: "transparent",
          colorBorder: "#f0cfdc",
          colorBorderSecondary: "#f8e3eb",
          borderRadius: 14,
          fontFamily:
            '"Nunito Variable", -apple-system, "Segoe UI", "PingFang SC", "HarmonyOS Sans SC", "MiSans", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
        },
        components: {
          Layout: { siderBg: "#fff7fa", headerBg: "rgba(255, 252, 254, 0.62)", bodyBg: "transparent" },
          Menu: {
            itemBg: "transparent",
            itemColor: "#7d6672",
            itemHoverBg: "#fff0f5",
            itemHoverColor: "#e4587f",
            itemSelectedBg: "#ffe3ec",
            itemSelectedColor: "#d63f71",
            itemBorderRadius: 12,
            activeBarBorderWidth: 0,
            itemMarginInline: 10,
          },
          Card: {
            borderRadius: 16,
            borderRadiusLG: 20,
            colorBorderSecondary: "#fbe4ec",
            boxShadowTertiary: "0 10px 30px rgba(242, 96, 141, 0.10)",
          },
          Table: {
            headerBg: "#fff2f6",
            headerColor: "#a37186",
            rowHoverBg: "#fff8fb",
            borderColor: "#fbe4ec",
            headerSplitColor: "transparent",
          },
          Tabs: { inkBarColor: "#f2608d", itemSelectedColor: "#d23f72", itemHoverColor: "#f2608d" },
          Button: { borderRadius: 999, borderRadiusLG: 999, controlHeight: 36, controlHeightLG: 44, fontWeight: 600 },
        },
      }}
    >
      <BrowserRouter basename="/webui">
        <App />
      </BrowserRouter>
    </ConfigProvider>
  </React.StrictMode>,
);
