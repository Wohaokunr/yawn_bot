import {
  ApartmentOutlined,
  AppstoreOutlined,
  ArrowLeftOutlined,
  ControlOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SmileOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Skeleton, Tabs, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { AgentConfigPanel } from "../agent-panels/AgentConfigPanel";
import { PersonaPanel } from "../agent-panels/PersonaPanel";
import { RelationsPanel } from "../agent-panels/RelationsPanel";
import { ApiError } from "../api";
import { FeatureTable } from "../directory/components";
import { PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import type { FeatureState } from "../types";
import { GroupManagePanel } from "./GroupManagePanel";
import { meApi } from "./me-api";
import { groupRoleLabel } from "./me-meta";
import "./me.css";

const { Text } = Typography;
const TABS = new Set(["manage", "agent", "persona", "features", "relations"]);
const MANAGER_TABS = new Set(["manage", "agent", "persona"]);

// “从其它群复制运行配置”只列自己管理的群
const myGroupsForCopy = (search: string) => meApi.groups().then((rows) => {
  const needle = search.trim().toLowerCase();
  return rows
    .filter((row) => !needle || `${row.groupName ?? ""} ${row.groupId}`.toLowerCase().includes(needle))
    .map((row) => ({ groupId: row.groupId, groupName: row.groupName }));
});

export function MyGroupPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const query = useApiQuery({ queryKey: ["me", "group", groupId], fetcher: () => meApi.group(groupId) });
  // 网页群管理关着时（GROUP_MANAGE_WEBUI=false）只剩原来的两个标签
  const managerTabs = query.data?.groupManage?.webui ?? true;
  const fallback = managerTabs ? "manage" : "relations";
  const requested = searchParams.get("tab") ?? fallback;
  const tab = TABS.has(requested) && (managerTabs || !MANAGER_TABS.has(requested)) ? requested : fallback;
  const [features, setFeatures] = useState<FeatureState[] | null>(null);

  useEffect(() => setFeatures(query.data?.features ?? null), [query.data]);

  // 关系标签默认打开“洞察”视图（群管理员最关心的全局结构）
  useEffect(() => {
    if (tab !== "relations" || searchParams.has("relations.view")) return;
    const next = new URLSearchParams(searchParams);
    next.set("relations.view", "insights");
    setSearchParams(next, { replace: true });
  }, [tab, searchParams, setSearchParams]);

  const forbidden = query.error && !query.data && query.error.includes("不是这个群的群主或管理员");
  if (forbidden) {
    return <div className="me-page">
      <PageHeader title="没有这个群的管理权限" subtitle={`群 ${groupId}`} />
      <Alert
        type="warning"
        showIcon
        icon={<WarningOutlined />}
        message="你不是这个群的群主或管理员"
        description="只有群主和管理员能在这里管理群、调本群 Agent 和人设。如果你刚被设为管理员，在群里说句话让 Bot 同步一下身份再来。"
        action={<Link to="/me"><Button size="small">返回我的主页</Button></Link>}
      />
    </div>;
  }

  const group = query.data;
  const name = group?.groupName || `群 ${groupId}`;

  const change = async (key: string, override: boolean | null): Promise<void> => {
    try {
      const row = await meApi.setFeature(groupId, key, override);
      setFeatures((current) => current?.map((item) => (item.key === key ? row : item)) ?? null);
      message.success(`「${row.name}」已${row.effective ? "开启" : "关闭"}${override === null ? "（跟随默认）" : ""}`);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 403) query.reload();
      throw reason;
    }
  };

  return <div className="me-page">
    <PageHeader
      title={name}
      subtitle={group ? `群 ${groupId} · ${group.memberCount} 名成员` : `群 ${groupId}`}
      status={group && <Tag className={`me-role-tag is-${group.role}`} icon={<SafetyCertificateOutlined />}>你是{groupRoleLabel(group.role)}</Tag>}
      extra={<Link to="/me"><Button icon={<ArrowLeftOutlined />}>我管理的群</Button></Link>}
      onRefresh={query.reload}
      refreshing={query.refreshing}
    />
    {query.error && !group && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {group && !group.verified && (
      <Alert
        className="me-banner"
        type="warning"
        showIcon
        message="Bot 暂时不在线，没法向 QQ 实时确认你的管理员身份"
        description="现在可以查看，但群管理、Agent 设置、人设和群功能开关要等 Bot 上线后才能修改。"
      />
    )}
    <Tabs
      destroyOnHidden
      activeKey={tab}
      onChange={(key) => {
        const next = new URLSearchParams();
        next.set("tab", key);
        setSearchParams(next, { replace: true });
      }}
      items={[
        ...(managerTabs ? [
          {
            key: "manage",
            label: <span><ControlOutlined /> 群管理</span>,
            children: <GroupManagePanel groupId={groupId} groupName={name} />,
          },
          {
            key: "agent",
            label: <span><RobotOutlined /> Agent 设置</span>,
            children: <div className="me-agent-embed">
              <Text type="secondary" className="me-hint me-tab-hint">
                本群 Agent 怎么回话、主动参与多少、能用哪些管理工具。和运维在管理台看到的是同一份设置，你的修改会记进「群管理 → 操作记录」。
              </Text>
              <AgentConfigPanel groupId={groupId} manager loadGroups={myGroupsForCopy} />
            </div>,
          },
          {
            key: "persona",
            label: <span><SmileOutlined /> 群人设</span>,
            children: <div className="me-agent-embed">
              <Text type="secondary" className="me-hint me-tab-hint">
                Bot 在本群的名字、性格和说话方式，只影响这个群。人设里不能写让 Bot 无视规则的话；人设库里的模板可以一键套用。
              </Text>
              <PersonaPanel groupId={groupId} manager />
            </div>,
          },
        ] : []),
        {
          key: "relations",
          label: <span><ApartmentOutlined /> 群关系洞悉</span>,
          children: <>
            <Text type="secondary" className="me-hint me-tab-hint">
              Bot 从群聊里观察到的成员关系，只读；不显示来源与证据细节，退出记录的成员不会出现。
            </Text>
            <RelationsPanel groupId={groupId} readOnly />
          </>,
        },
        {
          key: "features",
          label: <span><AppstoreOutlined /> 群功能设置</span>,
          children: !features ? <Skeleton active paragraph={{ rows: 6 }} /> : <div className="me-feature-card">
            <Text type="secondary" className="me-hint">
              和群里的 /面板、管理台写的是同一份设置。“继承”表示跟随默认（开启）；单个成员的单独设置不受影响。
            </Text>
            <FeatureTable
              rows={features}
              onChange={change}
              confirmOff={group?.featureHints}
              emptyHint="全部跟随默认（开启）"
            />
          </div>,
        },
      ]}
    />
  </div>;
}
