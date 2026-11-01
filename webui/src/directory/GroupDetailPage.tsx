import {
  ApiOutlined,
  AppstoreOutlined,
  ClearOutlined,
  DashboardOutlined,
  SafetyOutlined,
  SearchOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Badge,
  Button,
  Card,
  Empty,
  Input,
  Popconfirm,
  Progress,
  Segmented,
  Select,
  Skeleton,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { GroupWorkspaceSwitch } from "../group-workspace";
import { AdminEmpty, PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import {
  activeFilterCount,
  ACTIVITY_OPTIONS,
  groupDisplayName,
  groupOverridesByMember,
  memberDisplayName,
  memberFiltersToParams,
  memberListQuery,
  MEMBER_SORT_OPTIONS,
  parseGroupTab,
  parseMemberFilters,
  percent,
  redundantOverrides,
  roleMeta,
  updateParams,
} from "./directory-meta";
import type { GroupTab, MemberFilters } from "./directory-meta";
import { directoryApi } from "./directory-api";
import type { Paged } from "./directory-api";
import {
  ActivityDot,
  BatchBar,
  clickableRow,
  CopyId,
  FeatureTable,
  FilterResetButton,
  Metric,
  RelativeTime,
  RoleTag,
  useDebouncedValue,
} from "./components";
import { UserProfileDrawer } from "./UserProfileDrawer";
import type { GroupDetail, GroupOverview, MemberOverride, MemberRow } from "./types";

const { Text } = Typography;

const AGENT_OFF_WARNING = "关掉后这个群的 Agent 不再回复，正在进行的对话会被结束。和 Agent 工作台里的总开关是同一个开关。";

export function GroupDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = parseGroupTab(params.get("group.tab"));
  const openUser = params.get("user");
  const groupQuery = useApiQuery<GroupDetail>({
    queryKey: ["group", groupId],
    fetcher: (signal) => directoryApi.group(groupId, signal),
    invalidation: { resources: ["group_feature", "agent_config"], scope: { groupId } },
  });
  const overridesQuery = useApiQuery<MemberOverride[]>({
    queryKey: ["group-overrides", groupId],
    fetcher: (signal) => directoryApi.groupOverrides(groupId, signal),
    invalidation: { resources: ["user_feature", "group_feature", "agent_config"], scope: { groupId } },
  });

  const setTab = (next: GroupTab) => setParams(updateParams(params, { "group.tab": next === "overview" ? null : next }), { replace: true });
  const setUser = (userId: string | null) => setParams(updateParams(params, { user: userId, "user.tab": null }));

  const group = groupQuery.data;
  if (!group) {
    return groupQuery.error
      ? <QueryErrorAlert error={groupQuery.error} onRetry={groupQuery.reload} />
      : <Skeleton active />;
  }
  const overrideTotal = overridesQuery.data?.length ?? 0;
  const groupOverrideTotal = group.features.filter((row) => row.override !== null).length;

  return (
    <div className="dir-page">
      <PageHeader
        title={groupDisplayName(group)}
        subtitle=""
        extra={<GroupWorkspaceSwitch groupId={group.groupId} active="access" />}
      />
      <div className="dir-group-meta">
        <CopyId value={group.groupId} label="群号" />
        <span>{group.memberCount} 名成员</span>
        <span>最近活跃 <RelativeTime value={group.lastActiveAt} /></span>
        {group.firstSeenAt && <span>首次见到 <RelativeTime value={group.firstSeenAt} /></span>}
      </div>
      <Tabs
        activeKey={tab}
        onChange={(key) => setTab(key as GroupTab)}
        destroyOnHidden
        items={[
          { key: "overview", label: <span><DashboardOutlined /> 概览</span>, children: <OverviewTab group={group} onOpenUser={setUser} onTab={setTab} /> },
          {
            key: "features",
            label: <span><AppstoreOutlined /> 群功能 {groupOverrideTotal > 0 && <Badge count={groupOverrideTotal} size="small" color="orange" />}</span>,
            children: <FeaturesTab group={group} onSaved={() => { groupQuery.reload(); overridesQuery.reload(); }} />,
          },
          { key: "members", label: <span><TeamOutlined /> 成员</span>, children: <MembersTab groupId={groupId} onOpenUser={setUser} /> },
          {
            key: "overrides",
            label: <span><SafetyOutlined /> 覆盖一览 {overrideTotal > 0 && <Badge count={overrideTotal} size="small" color="purple" />}</span>,
            children: <OverridesTab groupId={groupId} query={overridesQuery} onOpenUser={setUser} />,
          },
        ]}
      />
      <UserProfileDrawer
        userId={openUser}
        groupId={groupId}
        groupFeatures={group.features}
        onChanged={overridesQuery.reload}
        onClose={() => setUser(null)}
      />
    </div>
  );
}

// ── 概览 ──────────────────────────────────────────────────────

function OverviewTab({ group, onOpenUser, onTab }: { group: GroupDetail; onOpenUser: (userId: string) => void; onTab: (tab: GroupTab) => void }): React.JSX.Element {
  const query = useApiQuery<GroupOverview>({
    queryKey: ["group-overview", group.groupId],
    fetcher: (signal) => directoryApi.groupOverview(group.groupId, signal),
    invalidation: { resources: ["group_feature", "agent_config", "user_feature"], scope: { groupId: group.groupId } },
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  const data = query.data;
  if (!data) return <Skeleton active />;
  const checkinDelta = data.checkins.today - data.checkins.yesterday;
  const roles = Object.entries(data.roles).sort(([a], [b]) => roleMeta(a).order - roleMeta(b).order);

  return (
    <div className="dir-overview">
      <div className="sbx-metrics">
        <Metric label="成员" value={group.memberCount} hint={`7 天 ${data.activeMembers["7d"]} 人出现（${percent(data.activeMembers["7d"], group.memberCount)}%）`} />
        <Metric label="24 小时活跃" value={data.activeMembers["24h"]} hint={`30 天 ${data.activeMembers["30d"]} 人`} />
        <Metric
          label="今日签到"
          value={data.checkins.today}
          hint={checkinDelta === 0 ? `与昨天持平（${data.checkins.yesterday}）` : `比昨天${checkinDelta > 0 ? "多" : "少"} ${Math.abs(checkinDelta)}（昨天 ${data.checkins.yesterday}）`}
        />
        <Metric
          label="单独设置"
          value={data.overrides.group + data.overrides.member}
          hint={<button type="button" className="dir-link" onClick={() => onTab("overrides")}>群 {data.overrides.group} · {data.overrides.membersWithOverrides} 位成员 {data.overrides.member} 条</button>}
        />
      </div>
      <div className="sbx-grid dir-overview-grid">
        <Card size="small" title="Agent" extra={<Link to={`/agent/${group.groupId}`}><ApiOutlined /> 去工作台</Link>}>
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <Space wrap>
              <Tag color={data.agent.enabled ? "green" : "default"}>{data.agent.enabled ? "开启" : "关闭"}</Tag>
              {data.agent.personaEnabled && <Tag color="magenta">人设已启用</Tag>}
              {data.agent.enabled && <Tag>{data.agent.proactiveEnabled ? "会主动接话" : "只在被叫时回复"}</Tag>}
            </Space>
            <Text type="secondary">最近发言：<RelativeTime value={data.agent.lastAgentAt} empty="还没说过话" /></Text>
            {data.agent.dailyLimit !== null && <Text type="secondary">每日主动上限 {data.agent.dailyLimit} 次</Text>}
            {!data.agent.configured && <Text type="secondary">还没有保存过 Agent 配置，使用默认值</Text>}
          </Space>
        </Card>
        <Card size="small" title="角色分布">
          {roles.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有成员" /> : (
            <div className="dir-bars">
              {roles.map(([role, count]) => (
                <div key={role} className="dir-bar-row">
                  <span>{roleMeta(role).label}</span>
                  <Progress percent={percent(count, group.memberCount)} size="small" strokeColor="var(--accent, #e4558a)" format={() => count} />
                </div>
              ))}
            </div>
          )}
        </Card>
        <Card size="small" title="最近出现" extra={<button type="button" className="dir-link" onClick={() => onTab("members")}>全部成员</button>}>
          <RankList
            empty="还没有人说过话"
            rows={data.recentMembers.map((row) => ({ key: row.userId, name: row.name, extra: <RelativeTime value={row.lastSeenAt} />, tag: row.role !== "member" ? <RoleTag role={row.role} /> : null }))}
            onOpen={onOpenUser}
          />
        </Card>
        <Card size="small" title="连签榜">
          <RankList
            empty="还没有人在连签"
            rows={data.checkins.streakTop.map((row) => ({ key: row.userId, name: row.name, extra: `${row.streakDays} 天`, tag: row.checkedToday ? <Tag color="success">今日已签</Tag> : <Tag>今日未签</Tag> }))}
            onOpen={onOpenUser}
          />
        </Card>
        <Card size="small" title="群好感榜">
          <RankList
            empty="还没有人有群好感"
            rows={data.affinityTop.map((row) => ({ key: row.userId, name: row.name, extra: row.groupAffinity }))}
            onOpen={onOpenUser}
          />
        </Card>
      </div>
    </div>
  );
}

function RankList({ rows, empty, onOpen }: { rows: { key: string; name: string; extra: React.ReactNode; tag?: React.ReactNode }[]; empty: string; onOpen: (key: string) => void }): React.JSX.Element {
  if (rows.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={empty} />;
  return (
    <ol className="dir-rank">
      {rows.map((row, index) => (
        <li key={row.key}>
          <span className="dir-rank-index">{index + 1}</span>
          <button type="button" className="dir-link dir-rank-name" onClick={() => onOpen(row.key)}>{row.name}</button>
          {row.tag}
          <span className="dir-rank-extra">{row.extra}</span>
        </li>
      ))}
    </ol>
  );
}

// ── 群功能 ────────────────────────────────────────────────────

function FeaturesTab({ group, onSaved }: { group: GroupDetail; onSaved: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  return (
    <Card>
      <Alert
        className="dir-hint"
        type="info"
        showIcon
        message="群里所有人默认跟随这里；某个成员要例外，到“成员”里单独设置"
        description="群聊 Agent 一项与 Agent 工作台的总开关是同一个开关。私聊里的功能在“用户”页管理，和这里互不影响。"
      />
      <FeatureTable
        rows={group.features}
        confirmOff={{ group_agent: AGENT_OFF_WARNING }}
        onChange={async (feature, override) => {
          await directoryApi.setGroupFeature(group.groupId, feature, override);
          message.success("群功能已更新");
          onSaved();
        }}
        resetHint="除群聊 Agent 总开关外，其余功能回到默认开启。"
        onReset={async () => {
          const result = await directoryApi.resetGroupFeatures(group.groupId);
          message.success(result.removed.length ? `已恢复 ${result.removed.length} 项` : "没有需要恢复的项");
          onSaved();
        }}
      />
    </Card>
  );
}

// ── 成员 ──────────────────────────────────────────────────────

function MembersTab({ groupId, onOpenUser }: { groupId: string; onOpenUser: (userId: string) => void }): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const filters = parseMemberFilters(params);
  const [searchText, setSearchText] = useState(filters.q);
  const debounced = useDebouncedValue(searchText.trim());
  const [selected, setSelected] = useState<string[]>([]);
  const setFilters = (patch: Partial<MemberFilters>, resetPage = true) => {
    const next = { ...filters, ...patch, page: resetPage ? 1 : patch.page ?? filters.page };
    setParams(updateParams(params, memberFiltersToParams(next)), { replace: true });
  };
  useEffect(() => {
    if (debounced !== filters.q) setFilters({ q: debounced });
    // 只在输入停顿后同步一次，filters 每次渲染都是新对象，不放进依赖。
  }, [debounced]);

  const query = useApiQuery<Paged<MemberRow>>({
    queryKey: ["group-members", groupId, memberListQuery(filters)],
    fetcher: (signal) => directoryApi.members(groupId, memberListQuery(filters), signal),
    invalidation: { resources: ["user_feature"], scope: { groupId } },
  });
  const filterCount = activeFilterCount(filters as unknown as Record<string, unknown>, ["q", "role", "activity", "overrides"]);
  const resetFilters = () => {
    setSearchText("");
    setParams(updateParams(params, memberFiltersToParams({ sort: filters.sort })), { replace: true });
  };

  const columns: ColumnsType<MemberRow> = [
    {
      title: "成员",
      render: (_, row) => (
        <div className="dir-entity">
          <ActivityDot value={row.lastSeenAt} />
          <div className="dir-cell-stack">
            <Space size={6} wrap>
              <button type="button" className="dir-link dir-entity-name" onClick={() => onOpenUser(row.userId)}>{memberDisplayName(row)}</button>
              {row.title && <Tag className="dir-mini-tag" color="magenta">{row.title}</Tag>}
              {!row.active && <Tag className="dir-mini-tag">已不在群</Tag>}
            </Space>
            <Space size={6}>
              <CopyId value={row.userId} label="QQ" />
              {row.groupNickname && row.nickname && row.groupNickname !== row.nickname && <Text type="secondary" className="dir-sub">QQ 昵称 {row.nickname}</Text>}
            </Space>
          </div>
        </div>
      ),
    },
    { title: "角色", dataIndex: "role", width: 90, render: (role: string) => <RoleTag role={role} /> },
    {
      title: "签到",
      width: 120,
      responsive: ["md"],
      render: (_, row) => (
        <div className="dir-cell-stack">
          <span>{row.streakDays > 0 ? `连签 ${row.streakDays} 天` : <Text type="secondary">没在连签</Text>}</span>
          <Text type="secondary" className="dir-sub">{row.checkedToday ? "今日已签" : "今日未签"} · 累计 {row.totalDays}</Text>
        </div>
      ),
    },
    { title: "群好感", dataIndex: "groupAffinity", width: 90, responsive: ["lg"], render: (value: number) => <span className={value ? "dir-num" : "dir-num is-muted"}>{value}</span> },
    {
      title: "单独设置",
      dataIndex: "overrideCount",
      width: 96,
      responsive: ["sm"],
      render: (value: number) => (value ? <Tag color="purple">{value} 项</Tag> : <Text type="secondary">无</Text>),
    },
    { title: "最近出现", dataIndex: "lastSeenAt", width: 110, responsive: ["sm"], render: (value: string | null) => <RelativeTime value={value} /> },
  ];

  return (
    <Card>
      <div className="dir-toolbar" role="search">
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜昵称、群名片或 QQ"
          value={searchText}
          onChange={(event) => setSearchText(event.target.value)}
          className="dir-search"
          aria-label="搜成员"
        />
        <Segmented
          value={filters.role}
          onChange={(value) => setFilters({ role: value as MemberFilters["role"] })}
          options={[
            { value: "", label: "全部" },
            { value: "owner", label: "群主" },
            { value: "admin", label: "管理员" },
            { value: "member", label: "成员" },
          ]}
        />
        <Select value={filters.activity} onChange={(value) => setFilters({ activity: value })} options={ACTIVITY_OPTIONS} className="dir-select" aria-label="按活跃筛选" />
        <Select
          value={filters.sort}
          onChange={(value) => setFilters({ sort: value })}
          options={MEMBER_SORT_OPTIONS.map((item) => ({ value: item.value, label: `排序：${item.label}` }))}
          className="dir-select"
          aria-label="排序"
        />
        <label className="dir-switch">
          <Switch size="small" checked={filters.overrides} onChange={(checked) => setFilters({ overrides: checked })} />
          只看有单独设置的
        </label>
        <FilterResetButton count={filterCount} onReset={resetFilters} />
        <span className="dir-count">{query.data ? `共 ${query.data.total} 人` : null}</span>
      </div>
      <BatchBar
        count={selected.length}
        noun="成员"
        onClear={() => setSelected([])}
        onApply={async (feature, override) => {
          await directoryApi.batchMembers(groupId, selected, feature, override);
          query.reload();
        }}
      />
      {query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : (
          <Table
            rowKey="userId"
            loading={query.initialLoading || query.transitioning}
            dataSource={query.data?.rows ?? []}
            columns={columns}
            rowSelection={{ selectedRowKeys: selected, onChange: (keys) => setSelected(keys.map(String)), preserveSelectedRowKeys: true }}
            rowClassName={() => "dir-row"}
            onRow={(row) => clickableRow(() => onOpenUser(row.userId), `打开 ${memberDisplayName(row)} 的档案`) as React.HTMLAttributes<HTMLTableRowElement>}
            locale={{
              emptyText: filterCount
                ? <AdminEmpty description="没有符合筛选的成员" action={<Button size="small" onClick={resetFilters}>清除筛选</Button>} />
                : <AdminEmpty description="还没有记录到成员" />,
            }}
            pagination={{
              current: filters.page,
              pageSize: 20,
              total: query.data?.total ?? 0,
              showSizeChanger: false,
              hideOnSinglePage: true,
              showTotal: (total, range) => `${range[0]}-${range[1]} / ${total}`,
              onChange: (page) => setFilters({ page }, false),
            }}
          />
        )}
    </Card>
  );
}

// ── 覆盖一览 ──────────────────────────────────────────────────

function OverridesTab({
  groupId,
  query,
  onOpenUser,
}: {
  groupId: string;
  query: ReturnType<typeof useApiQuery<MemberOverride[]>>;
  onOpenUser: (userId: string) => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const [busy, setBusy] = useState("");
  const [view, setView] = useState<"feature" | "member">("feature");
  const rows = useMemo(() => query.data ?? [], [query.data]);
  const redundant = redundantOverrides(rows);
  const byMember = useMemo(() => groupOverridesByMember(rows), [rows]);

  const revoke = async (items: MemberOverride[], done: string) => {
    setBusy(items.map((row) => `${row.userId}:${row.feature}`).join(","));
    try {
      for (const row of items) {
        await directoryApi.setMemberFeature(groupId, row.userId, row.feature, null);
      }
      message.success(done);
      query.reload();
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setBusy("");
    }
  };

  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active />;
  if (rows.length === 0) {
    return (
      <Card>
        <AdminEmpty
          description="本群没有成员级单独设置，所有人都跟随群功能"
          action={<Button size="small" onClick={() => navigate(`/groups/${groupId}?group.tab=members`)}>去成员列表</Button>}
        />
      </Card>
    );
  }

  const columns: ColumnsType<MemberOverride> = [
    {
      title: "成员",
      render: (_, row) => (
        <Space size={6}>
          <button type="button" className="dir-link" onClick={() => onOpenUser(row.userId)}>{row.name}</button>
          {row.role && row.role !== "member" && <RoleTag role={row.role} />}
        </Space>
      ),
    },
    { title: "功能", dataIndex: "featureName", width: 120 },
    { title: "单独设为", width: 96, render: (_, row) => <Tag color={row.enabled ? "green" : "red"}>{row.enabled ? "开" : "关"}</Tag> },
    { title: "群里是", width: 84, responsive: ["sm"], render: (_, row) => <Text type="secondary">{row.groupEffective ? "开" : "关"}</Text> },
    {
      title: "说明",
      responsive: ["md"],
      render: (_, row) => {
        if (row.blockedByGroup) return <Tag color="warning">群里 Agent 总开关关着，单独开启不生效</Tag>;
        if (row.redundant) return <Tag>和群里一样，可以清掉</Tag>;
        return <Text type="secondary">{row.enabled ? "群里关着，这位成员例外开启" : "群里开着，这位成员被单独关闭"}</Text>;
      },
    },
    {
      title: "",
      width: 80,
      render: (_, row) => (
        <Popconfirm title={`撤销 ${row.name} 的「${row.featureName}」单独设置？`} description="撤销后跟随群设置" onConfirm={() => revoke([row], "已撤销")}>
          <Button type="link" size="small" loading={busy.includes(`${row.userId}:${row.feature}`)}>撤销</Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <Card>
      <div className="dir-toolbar">
        <Segmented value={view} onChange={(value) => setView(value as "feature" | "member")} options={[{ value: "feature", label: "逐条" }, { value: "member", label: "按成员" }]} />
        <Text type="secondary">{byMember.length} 位成员共 {rows.length} 条单独设置</Text>
        <span className="dir-count">
          <Popconfirm
            title={`清掉 ${redundant.length} 条多余的设置？`}
            description="这些设置和群里的开关一样，清掉不会改变任何人的实际权限。"
            disabled={redundant.length === 0}
            onConfirm={() => revoke(redundant, `已清理 ${redundant.length} 条`)}
          >
            <Button size="small" icon={<ClearOutlined />} disabled={redundant.length === 0} loading={busy !== "" && redundant.length > 1}>
              清理多余设置{redundant.length ? `（${redundant.length}）` : ""}
            </Button>
          </Popconfirm>
        </span>
      </div>
      {view === "feature" ? (
        <Table rowKey={(row) => `${row.userId}:${row.feature}`} size="middle" pagination={rows.length > 20 ? { pageSize: 20, showSizeChanger: false } : false} dataSource={rows} columns={columns} />
      ) : (
        <div className="dir-override-members">
          {byMember.map((member) => (
            <div key={member.userId} className="dir-override-member">
              <div className="dir-override-member-head">
                <button type="button" className="dir-link dir-entity-name" onClick={() => onOpenUser(member.userId)}>{member.name}</button>
                {member.role !== "member" && <RoleTag role={member.role} />}
                <Popconfirm title={`撤销 ${member.name} 在本群的全部单独设置？`} onConfirm={() => revoke(member.items, "已全部撤销")}>
                  <Button type="link" size="small">全部撤销</Button>
                </Popconfirm>
              </div>
              <Space size={[6, 6]} wrap>
                {member.items.map((item) => (
                  <Tag key={item.feature} color={item.blockedByGroup ? "warning" : item.enabled ? "green" : "red"}>
                    {item.featureName} {item.enabled ? "开" : "关"}{item.redundant ? "（多余）" : ""}
                  </Tag>
                ))}
              </Space>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
