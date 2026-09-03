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
      onRefresh={query.reload}
      refreshing={query.refreshing}
      extra={<Input.Search placeholder="搜索群聊" allowClear onSearch={(value) => { setSearch(value); setPage(1); }} />}
    />
    <Card>
      {query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table
            rowKey="groupId"
            loading={query.loading}
            dataSource={query.data?.rows ?? []}
            locale={{ emptyText: <Empty description="暂无可查看群聊" /> }}
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
                render: (_, row: GuestGroupSummary) => <>
                  <Text strong>{row.groupName || "未命名群"}</Text><br />
                  <Text type="secondary">{row.groupId}</Text>
                </>,
              },
              { title: "成员", dataIndex: "memberCount", width: 120 },
              {
                title: "操作",
                width: 120,
                render: (_, row: GuestGroupSummary) => <Link to={`/guest/${row.groupId}`}>查看</Link>,
              },
            ]}
          />}
    </Card>
  </>;
}

export function GuestGroupPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") ?? "memories";
  const tab = GUEST_TABS.has(requestedTab) ? requestedTab : "memories";
  const detailQuery = useApiQuery(() => api<GuestGroupDetail>(`/guest/groups/${groupId}`).then((response) => response.data));

  useEffect(() => {
    if (requestedTab === tab) return;
    const next = new URLSearchParams(searchParams);
    next.delete("tab");
    setSearchParams(next, { replace: true });
  }, [requestedTab, searchParams, setSearchParams, tab]);

  if (detailQuery.error && !detailQuery.data) {
    const forbidden = detailQuery.error.includes("403") || detailQuery.error.includes("权限") || detailQuery.error.includes("访客");
    return <>
      <PageHeader title="群聊不可访问" subtitle="这个群聊当前不在你的访客可见范围内。" extra={<Link to="/guest">返回群聊列表</Link>} />
      <Alert
        type={forbidden ? "warning" : "error"}
        showIcon
        message={forbidden ? "访问权限已变化" : "无法加载群聊"}
        description={detailQuery.error}
        action={<Button onClick={detailQuery.reload}>重试</Button>}
      />
    </>;
  }

  const detail = detailQuery.data;
  const groupName = detail?.groupName || `群 ${groupId}`;
  const changeTab = (key: string) => {
    if (!GUEST_TABS.has(key)) return;
    const next = new URLSearchParams(searchParams);
    if (key === "memories") next.delete("tab"); else next.set("tab", key);
    setSearchParams(next, { replace: true });
  };

  return <>
    <PageHeader
      title={groupName}
      subtitle="访客视图只展示管理员开放的只读 Agent 数据。"
      status={<Tag color="blue" icon={<EyeOutlined />}>只读访客</Tag>}
      extra={<Link to="/guest">返回群聊列表</Link>}
    />
    <Space wrap className="section-row">
      <Tag icon={<ApartmentOutlined />}>群 {groupId}</Tag>
      {detail && <Tag>{detail.memberCount} 名成员</Tag>}
    </Space>
    <Tabs
      activeKey={tab}
      onChange={changeTab}
      items={[
        {
          key: "memories",
          label: <span><ReadOutlined /> 记忆</span>,
          children: <MemoriesPanel groupId={groupId} readOnly />,
        },
        {
          key: "profiles",
          label: <span><IdcardOutlined /> 成员画像</span>,
          children: <MemberProfilesPanel groupId={groupId} readOnly />,
        },
        {
          key: "relations",
          label: <span><ApartmentOutlined /> 关系</span>,
          children: <RelationsPanel groupId={groupId} readOnly />,
        },
      ]}
    />
  </>;
}
