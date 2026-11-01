import { ApartmentOutlined, AppstoreOutlined, ArrowLeftOutlined, SafetyCertificateOutlined, WarningOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Skeleton, Tabs, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { RelationsPanel } from "../agent-panels/RelationsPanel";
import { ApiError } from "../api";
import { FeatureTable } from "../directory/components";
import { PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import type { FeatureState } from "../types";
import { meApi } from "./me-api";
import { groupRoleLabel } from "./me-meta";
import "./me.css";

const { Text } = Typography;
const TABS = new Set(["relations", "features"]);

export function MyGroupPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab") ?? "relations";
  const tab = TABS.has(requested) ? requested : "relations";
  const query = useApiQuery({ queryKey: ["me", "group", groupId], fetcher: () => meApi.group(groupId) });
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
        description="只有群主和管理员能在这里查看群关系、开关群功能。如果你刚被设为管理员，在群里说句话让 Bot 同步一下身份再来。"
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
        description="现在可以查看，但群功能开关要等 Bot 上线后才能修改。"
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
