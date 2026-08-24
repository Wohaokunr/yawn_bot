import {
  App as AntApp,
  Button,
  Card,
  Drawer,
  Input,
  Select,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "./api";
import { AdminEmpty, formatTime, PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import type { FeatureState, GroupSummary, Member, UserSummary } from "./types";

const { Text } = Typography;

export function GroupsPage(): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const load = useCallback(
    () => api<GroupSummary[]>(`/groups?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`)
      .then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })),
    [page, search],
  );
  const query = useApiQuery(load, { resources: ["group_feature", "agent_config"] });
  const columns: ColumnsType<GroupSummary> = [
    {
      title: "群",
      render: (_, row) => <>
        <Link to={`/groups/${row.groupId}`}>{row.groupName || "未命名群"}</Link>
        <br />
        <Text type="secondary" copyable>{row.groupId}</Text>
      </>,
    },
    { title: "成员", dataIndex: "memberCount", width: 100 },
    {
      title: "Agent",
      dataIndex: "agentEnabled",
      width: 100,
      render: (value: boolean) => (
        <Tag color={value ? "green" : "default"}>{value ? "开启" : "关闭"}</Tag>
      ),
    },
    { title: "最近活跃", dataIndex: "lastActiveAt", render: formatTime },
    { title: "操作", width: 100, render: (_, row) => <Link to={`/agent/${row.groupId}`}>Agent</Link> },
  ];

  return <>
    <PageHeader
      title="群组与权限"
      subtitle="管理群级及成员级功能覆盖"
      onRefresh={query.reload}
      refreshing={query.refreshing}
      extra={(
        <Input.Search
          placeholder="搜索群名或群号"
          allowClear
          onSearch={(value) => {
            setSearch(value);
            setPage(1);
          }}
        />
      )}
    />
    <Card>
      {query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : (
          <Table
            rowKey="groupId"
            loading={query.loading}
            columns={columns}
            dataSource={query.data?.rows ?? []}
            locale={{ emptyText: <AdminEmpty description="暂无群组" /> }}
            pagination={{
              current: page,
              pageSize: 20,
              total: query.data?.total ?? 0,
              showSizeChanger: false,
              onChange: setPage,
            }}
          />
        )}
    </Card>
  </>;
}

function FeatureEditor({
  rows,
  onChange,
}: {
  rows: FeatureState[];
  onChange: (key: string, value: boolean | null) => Promise<void>;
}): React.JSX.Element {
  const [saving, setSaving] = useState("");
  return (
    <Table
      rowKey="key"
      pagination={false}
      dataSource={rows}
      columns={[
        {
          title: "功能",
          render: (_, row) => <>
            <Text strong>{row.name}</Text>
            <br />
            <Text type="secondary">{row.key}</Text>
          </>,
        },
        {
          title: "当前生效",
          render: (_, row) => (
            <Tag color={row.effective ? "green" : "red"}>
              {row.effective ? "开启" : "关闭"}
            </Tag>
          ),
        },
        {
          title: "来源",
          dataIndex: "source",
          render: (source: string) => ({
            default: "默认",
            group: "群设置",
            user: "用户覆盖",
            global_user: "全局用户",
          })[source] ?? source,
        },
        {
          title: "覆盖",
          width: 180,
          render: (_, row) => (
            <Select
              loading={saving === row.key}
              value={row.override === null ? "inherit" : row.override ? "on" : "off"}
              options={[
                { value: "inherit", label: "继承" },
                { value: "on", label: "显式开启" },
                { value: "off", label: "显式关闭" },
              ]}
              onChange={async (value) => {
                setSaving(row.key);
                try {
                  await onChange(row.key, value === "inherit" ? null : value === "on");
                } finally {
                  setSaving("");
                }
              }}
            />
          ),
        },
      ]}
    />
  );
}

interface GroupDetailData {
  groupId: string;
  groupName?: string;
  memberCount: number;
  features: FeatureState[];
}

export function GroupDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const { message } = AntApp.useApp();
  const groupLoad = useCallback(
    () => api<GroupDetailData>(`/groups/${groupId}`).then((r) => r.data),
    [groupId],
  );
  const groupQuery = useApiQuery(groupLoad, { resources: ["group_feature"] });
  const [memberPage, setMemberPage] = useState(1);
  const [memberSearch, setMemberSearch] = useState("");
  const membersLoad = useCallback(
    () => api<Member[]>(`/groups/${groupId}/members?page=${memberPage}&pageSize=20&search=${encodeURIComponent(memberSearch)}`)
      .then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })),
    [groupId, memberPage, memberSearch],
  );
  const membersQuery = useApiQuery(membersLoad);
  const [selectedMember, setSelectedMember] = useState<Member | null>(null);
  const [memberFeatures, setMemberFeatures] = useState<FeatureState[]>([]);
  const [featuresLoading, setFeaturesLoading] = useState(false);

  const openMember = async (member: Member) => {
    setSelectedMember(member);
    setFeaturesLoading(true);
    try {
      setMemberFeatures((await api<FeatureState[]>(`/groups/${groupId}/members/${member.userId}/features`)).data);
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "加载失败");
    } finally {
      setFeaturesLoading(false);
    }
  };

  const group = groupQuery.data;
  if (!group) {
    return groupQuery.error
      ? <QueryErrorAlert error={groupQuery.error} onRetry={groupQuery.reload} />
      : <Spin />;
  }

  return <>
    <PageHeader
      title={group.groupName || "未命名群"}
      subtitle={`群号 ${group.groupId} · ${group.memberCount} 名成员`}
      extra={<Link to="/groups">返回列表</Link>}
    />
    <Tabs items={[
      {
        key: "features",
        label: "群功能",
        children: (
          <Card>
            <FeatureEditor
              rows={group.features}
              onChange={async (feature, override) => {
                await api(`/groups/${groupId}/features/${feature}`, {
                  method: "PATCH",
                  body: JSON.stringify({ override }),
                });
                message.success("群功能已更新");
                groupQuery.reload();
              }}
            />
          </Card>
        ),
      },
      {
        key: "members",
        label: "成员",
        children: (
          <Card>
            <Input.Search
              className="table-search"
              placeholder="搜索成员昵称或 QQ"
              allowClear
              onSearch={(value) => {
                setMemberSearch(value);
                setMemberPage(1);
              }}
            />
            {membersQuery.error && !membersQuery.data
              ? <QueryErrorAlert error={membersQuery.error} onRetry={membersQuery.reload} />
              : (
                <Table
                  rowKey="userId"
                  loading={membersQuery.loading}
                  dataSource={membersQuery.data?.rows ?? []}
                  pagination={{
                    current: memberPage,
                    pageSize: 20,
                    total: membersQuery.data?.total ?? 0,
                    showSizeChanger: false,
                    onChange: setMemberPage,
                  }}
                  columns={[
                    {
                      title: "成员",
                      render: (_, row: Member) => <>
                        {row.groupNickname || row.nickname || "未知成员"}
                        <br />
                        <Text type="secondary" copyable>{row.userId}</Text>
                      </>,
                    },
                    { title: "角色", dataIndex: "role" },
                    { title: "最近出现", dataIndex: "lastSeenAt", render: formatTime },
                    {
                      title: "操作",
                      render: (_, row: Member) => (
                        <Button type="link" onClick={() => openMember(row)}>功能权限</Button>
                      ),
                    },
                  ]}
                />
              )}
          </Card>
        ),
      },
    ]} />
    <Drawer
      open={!!selectedMember}
      width={680}
      title={`${selectedMember?.groupNickname || selectedMember?.nickname || selectedMember?.userId} · 功能覆盖`}
      onClose={() => setSelectedMember(null)}
    >
      {featuresLoading
        ? <Spin />
        : selectedMember && (
          <FeatureEditor
            rows={memberFeatures}
            onChange={async (feature, override) => {
              const result = await api<FeatureState>(
                `/groups/${groupId}/members/${selectedMember.userId}/features/${feature}`,
                { method: "PATCH", body: JSON.stringify({ override }) },
              );
              setMemberFeatures((current) => current.map((row) => (
                row.key === feature ? result.data : row
              )));
              message.success("成员功能已更新");
            }}
          />
        )}
    </Drawer>
  </>;
}

export function UsersPage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const load = useCallback(
    () => api<UserSummary[]>(`/users?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`)
      .then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })),
    [page, search],
  );
  const query = useApiQuery(load, { resources: ["global_user_feature"] });
  const [selected, setSelected] = useState<UserSummary | null>(null);
  const [features, setFeatures] = useState<FeatureState[]>([]);
  const [featuresLoading, setFeaturesLoading] = useState(false);

  const open = async (user: UserSummary) => {
    setSelected(user);
    setFeaturesLoading(true);
    try {
      setFeatures((await api<FeatureState[]>(`/users/${user.userId}/features`)).data);
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "加载失败");
    } finally {
      setFeaturesLoading(false);
    }
  };

  return <>
    <PageHeader
      title="全局用户"
      subtitle="管理私聊及跨群全局功能覆盖"
      onRefresh={query.reload}
      refreshing={query.refreshing}
      extra={(
        <Input.Search
          placeholder="搜索昵称或 QQ"
          allowClear
          onSearch={(value) => {
            setSearch(value);
            setPage(1);
          }}
        />
      )}
    />
    <Card>
      {query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : (
          <Table
            rowKey="userId"
            loading={query.loading}
            dataSource={query.data?.rows ?? []}
            locale={{ emptyText: <AdminEmpty description="暂无用户" /> }}
            pagination={{
              current: page,
              pageSize: 20,
              total: query.data?.total ?? 0,
              showSizeChanger: false,
              onChange: setPage,
            }}
            columns={[
              {
                title: "用户",
                render: (_, row: UserSummary) => <>
                  {row.nickname || "未知用户"}
                  <br />
                  <Text type="secondary" copyable>{row.userId}</Text>
                </>,
              },
              { title: "好感", dataIndex: "affinity" },
              { title: "最近互动", dataIndex: "lastInteractionAt", render: formatTime },
              {
                title: "操作",
                render: (_, row: UserSummary) => (
                  <Button type="link" onClick={() => open(row)}>全局功能</Button>
                ),
              },
            ]}
          />
        )}
    </Card>
    <Drawer
      open={!!selected}
      width={680}
      title={`${selected?.nickname || selected?.userId} · 全局功能`}
      onClose={() => setSelected(null)}
    >
      {featuresLoading
        ? <Spin />
        : selected && (
          <FeatureEditor
            rows={features}
            onChange={async (feature, override) => {
              const result = await api<FeatureState>(`/users/${selected.userId}/features/${feature}`, {
                method: "PATCH",
                body: JSON.stringify({ override }),
              });
              setFeatures((current) => current.map((row) => (
                row.key === feature ? result.data : row
              )));
              message.success("全局用户功能已更新");
            }}
          />
        )}
    </Drawer>
  </>;
}
