// 开发专用预览页（vite dev 下访问 /dev-preview.html）：以 mock 数据离线调试
// 关系图谱的视觉与交互，不进入生产构建（vite build 默认只打包 index.html）。
import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import "@fontsource/zcool-kuaile/400.css";
import "./styles.css";
import { RelationGraphView } from "./relation-graph";
import type { AgentRelationGraph, AgentRelationGraphNode, AgentRelationItem } from "./types";

const members: Array<{
  userId: string;
  nickname: string;
  groupNickname: string | null;
  role: string;
}> = [
  { userId: "10001", nickname: "小樱", groupNickname: "樱樱子", role: "owner" },
  { userId: "10002", nickname: "阿澈", groupNickname: "澈哥", role: "admin" },
  { userId: "10003", nickname: "缓存姬", groupNickname: null, role: "bot" },
  { userId: "10004", nickname: "团子", groupNickname: "糯米团子", role: "member" },
  { userId: "10005", nickname: "沐沐", groupNickname: null, role: "member" },
  { userId: "10006", nickname: "阿力", groupNickname: "大力出奇迹", role: "member" },
  { userId: "10007", nickname: "青禾", groupNickname: null, role: "member" },
  { userId: "10008", nickname: "栗子", groupNickname: "糖炒栗子", role: "member" },
  { userId: "10009", nickname: "临风", groupNickname: null, role: "member" },
  { userId: "10010", nickname: "泡芙", groupNickname: "泡泡", role: "member" },
  { userId: "10011", nickname: "拾柒", groupNickname: null, role: "member" },
  { userId: "10012", nickname: "雾岛", groupNickname: "岛岛", role: "member" },
  { userId: "10013", nickname: "路过的风", groupNickname: null, role: "member" },
  { userId: "10014", nickname: "潜水员七号", groupNickname: null, role: "member" },
];

const edgeSeeds: Array<{
  subject: string;
  object: string;
  type: string;
  confidence: number;
  note: string;
}> = [
  { subject: "10001", object: "10004", type: "好友", confidence: 0.95, note: "每天互道晚安" },
  { subject: "10001", object: "10002", type: "死党", confidence: 0.8, note: "老战友" },
  { subject: "10004", object: "10005", type: "情侣", confidence: 0.9, note: "公开 CP" },
  { subject: "10004", object: "10006", type: "好友", confidence: 0.7, note: "" },
  { subject: "10006", object: "10004", type: "搭子", confidence: 0.5, note: "游戏搭子" },
  { subject: "10002", object: "10006", type: "师徒", confidence: 0.85, note: "带飞上分" },
  { subject: "10007", object: "10008", type: "伴侣", confidence: 0.75, note: "" },
  { subject: "10007", object: "10009", type: "同学", confidence: 0.6, note: "同校" },
  { subject: "10008", object: "10010", type: "同事", confidence: 0.55, note: "" },
  { subject: "10009", object: "10011", type: "对立", confidence: 0.65, note: "常在话题里互怼" },
  { subject: "10010", object: "10012", type: "mentions", confidence: 0.4, note: "" },
  { subject: "10001", object: "10007", type: "mentions", confidence: 0.35, note: "" },
  { subject: "10003", object: "10004", type: "mentions", confidence: 0.45, note: "经常被 @ 复读" },
  { subject: "10002", object: "10008", type: "同事", confidence: 0.7, note: "" },
  { subject: "10006", object: "10012", type: "同学", confidence: 0.5, note: "" },
  { subject: "10009", object: "10012", type: "搭子", confidence: 0.6, note: "饭搭子" },
  { subject: "10005", object: "10008", type: "好友", confidence: 0.65, note: "" },
  { subject: "10001", object: "10010", type: "亲属", confidence: 0.8, note: "表兄妹" },
  { subject: "10005", object: "10011", type: "好友", confidence: 0.5, note: "" },
  { subject: "10002", object: "10012", type: "对立", confidence: 0.55, note: "" },
];

const degree = new Map<string, number>();
for (const seed of edgeSeeds) {
  degree.set(seed.subject, (degree.get(seed.subject) ?? 0) + 1);
  degree.set(seed.object, (degree.get(seed.object) ?? 0) + 1);
}

const nodes: AgentRelationGraphNode[] = members.map((member) => ({
  userId: member.userId,
  nickname: member.nickname,
  groupNickname: member.groupNickname,
  role: member.role,
  linked: degree.has(member.userId),
  degree: degree.get(member.userId) ?? 0,
}));

const edges: AgentRelationItem[] = edgeSeeds.map((seed, index) => ({
  id: `edge-${index + 1}`,
  groupId: "20000",
  subjectUserId: seed.subject,
  objectUserId: seed.object,
  type: seed.type,
  sourceKind: seed.type === "mentions" ? "mention" : "auto",
  note: seed.note,
  confidence: seed.confidence,
  evidenceCount: Math.round(seed.confidence * 20) + 1,
  lastSeenAt: "2026-08-20T12:30:00Z",
}));

const mockGraph: AgentRelationGraph = {
  nodes,
  edges,
  meta: { relationTruncated: false, memberTruncated: false },
};

function PreviewApp(): React.JSX.Element {
  return (
    <div style={{ padding: "24px", maxWidth: 1400, margin: "0 auto" }}>
      <h1
        style={{
          fontFamily: '"ZCOOL KuaiLe", "PingFang SC", "Microsoft YaHei", sans-serif',
          color: "#d63f71",
          fontSize: 24,
          margin: "0 0 16px",
        }}
      >
        🌸 关系图谱预览（开发页）
      </h1>
      <RelationGraphView
        graph={mockGraph}
        typeFilter=""
        onEditRelation={() => undefined}
        onDeleteRelation={() => undefined}
      />
    </div>
  );
}

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
          colorTextBase: "#53414c",
          colorBgLayout: "transparent",
          colorBorder: "#f3cdd9",
          colorBorderSecondary: "#fbe4ec",
          borderRadius: 14,
          fontFamily:
            '-apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
        },
        components: {
          Button: { borderRadius: 999, borderRadiusLG: 999, controlHeight: 36, fontWeight: 600 },
        },
      }}
    >
      <PreviewApp />
    </ConfigProvider>
  </React.StrictMode>,
);
