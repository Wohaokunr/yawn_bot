import {
  ApartmentOutlined,
  EyeOutlined,
  IdcardOutlined,
  ReadOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useCallback, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { MemoriesPanel } from "./agent-panels/MemoriesPanel";
import { MemberProfilesPanel } from "./agent-panels/MemberProfilesPanel";
import { RelationsPanel } from "./agent-panels/RelationsPanel";
import { api, ApiError } from "./api";
import { PageHeader, QueryErrorAlert, useApiQuery } from "./shared";

const { Text } = Typography;

interface GuestGroupSummary {
  groupId: string;
  groupName: string | null;
  memberCount: number;
}

interface GuestGroupDetail extends GuestGroupSummary {}

const GUEST_TABS = new Set(["memories", "profiles", "relations"]);

export function GuestGroupsPage(): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const load = useCallback(
    () => api<GuestGroupSummary[]>(`/guest/groups?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`)
      .then((response) => ({ rows: response.data, total: response.meta.total ?? 0 })),
    [page, search],
  );
  const query = useApiQuery(load);

  return <>
    <PageHeader
      title="可查看群聊"
      subtitle="这里只展示管理员明确开放给访客的群聊；权限变化会在下一次请求时立即生效。"
      status={<Tag color="blue" icon={<EyeOutlined />}>访客 · 只读</Tag>}
      onRefresh={query.reload}
      refreshing={query.refreshing}
      extra={<Input.Search
        allowClear
        placeholder="搜索群名或群号"
        style={{ width: 240 }}
        onSearch={(value) => { setSearch(value.trim()); setPage(1); }}
      />}
    />
    <Alert
      type="info"
      showIcon
      className="section-alert"
      title="访客视图不会加载运维功能"
      description="进入群聊后仅提供记忆、成员画像和关系边三个只读页面，不提供 Agent 配置、人设、消息记录、调试、隐私治理或工具审计。"
    />
    <Card className="guest-groups-card">
      {query.error && !query.data ? (
        <QueryErrorAlert error={query.error} onRetry={query.reload} />
      ) : (
        <Table
          rowKey="groupId"
          loading={query.loading}
          dataSource={query.data?.rows ?? []}
          locale={{ emptyText: <Empty description="管理员暂未开放任何群聊" /> }}
          pagination={{
            current: page,
            pageSize: 20,
            total: query.data?.total ?? 0,
            showSizeChanger: false,
            onChange: setPage,
          }}
          columns={[
            {
              title: "群聊",
              render: (_, row: GuestGroupSummary) => <Space direction="vertical" size={0}>
                <Text strong>{row.groupName || "未命名群"}</Text>
                <Text type="secondary" copyable>{row.groupId}</Text>
              </Space>,
            },
            { title: "成员", dataIndex: "memberCount", width: 120, render: (value: number) => `${value} 人` },
            {
              title: "可查看内容",
              width: 300,
              render: () => <Space wrap>
                <Tag icon={<ReadOutlined />}>记忆</Tag>
                <Tag icon={<IdcardOutlined />}>成员画像</Tag>
                <Tag icon={<ApartmentOutlined />}>关系边</Tag>
              </Space>,
            },
            {
              title: "操作",
              width: 120,
              render: (_, row: GuestGroupSummary) => <Link to={`/guest/${row.groupId}?tab=memories`}>进入查看</Link>,
            },
          ]}
        />
      )}
    </Card>
  </>;
}

export function GuestGroupPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") ?? "memories";
  const tab = GUEST_TABS.has(requestedTab) ? requestedTab : "memories";

  useEffect(() => {
    if (GUEST_TABS.has(requestedTab)) return;
    const next = new URLSearchParams(searchParams);
    next.set("tab", "memories");
    setSearchParams(next, { replace: true });
  }, [requestedTab, searchParams, setSearchParams]);

  const groupLoad = useCallback(
    () => api<GuestGroupDetail>(`/groups/${groupId}`).then((response) => response.data),
    [groupId],
  );
  const groupQuery = useApiQuery(groupLoad);
  const forbidden = groupQuery.error && groupQuery.error.includes("未向访客开放");

  if (forbidden) {
    return <>
      <PageHeader title="群聊访问已收回" subtitle="管理员已经取消该群聊的访客授权。" />
      <Alert
        type="warning"
        showIcon
        title="当前访客会话不能再查看这个群聊"
        action={<Link to="/guest"><Button size="small">返回可查看群聊</Button></Link>}
      />
    </>;
  }

  const groupName = groupQuery.data?.groupName || `群 ${groupId}`;
  return <>
    <PageHeader
      title={groupName}
      subtitle={`群 ${groupId} · ${groupQuery.data ? `${groupQuery.data.memberCount} 名成员 · ` : ""}访客只读视图`}
      status={<Tag color="blue" icon={<EyeOutlined />}>访客 · 只读</Tag>}
      extra={<Link to="/guest">返回群聊列表</Link>}
    />
    {groupQuery.error && !groupQuery.data && <QueryErrorAlert error={groupQuery.error} onRetry={groupQuery.reload} />}
    <Tabs
      destroyOnHidden
      activeKey={tab}
      onChange={(key) => setSearchParams({ tab: key }, { replace: true })}
      items={[
        { key: "memories", label: <Space size={6}><ReadOutlined />记忆</Space>, children: <MemoriesPanel groupId={groupId} readOnly /> },
        { key: "profiles", label: <Space size={6}><IdcardOutlined />成员画像</Space>, children: <MemberProfilesPanel groupId={groupId} readOnly /> },
        { key: "relations", label: <Space size={6}><ApartmentOutlined />关系边</Space>, children: <RelationsPanel groupId={groupId} readOnly /> },
      ]}
    />
  </>;
}

export function isGuestTabAllowed(tab: string | null): boolean {
  return tab === null || GUEST_TABS.has(tab);
}

export function isGuestAccessError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}
