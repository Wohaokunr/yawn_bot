import { ApiOutlined, MoreOutlined, SafetyOutlined, SearchOutlined, TeamOutlined } from "@ant-design/icons";
import { App as AntApp, Badge, Button, Dropdown, Input, Segmented, Select, Skeleton, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { AdminEmpty, QueryErrorAlert, useApiQuery } from "../shared";
import {
  activeFilterCount,
  ACTIVITY_OPTIONS,
  featureLabel,
  GROUP_OVERRIDE_OPTIONS,
  GROUP_SORT_OPTIONS,
  groupDisplayName,
  groupFiltersToParams,
  groupListQuery,
  parseGroupFilters,
  percent,
  updateParams,
} from "./directory-meta";
import type { GroupFilters } from "./directory-meta";
import { directoryApi } from "./directory-api";
import type { Paged } from "./directory-api";
import {
  ActivityDot,
  BatchBar,
  clickableRow,
  CopyId,
  DirectoryStatusBar,
  FilterResetButton,
  RelativeTime,
  useDebouncedValue,
} from "./components";
import type { GroupRow, GroupsSummary } from "./types";

const { Text } = Typography;
const RESOURCES = ["group_feature", "agent_config", "user_feature"];

export function GroupsPage(): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const filters = parseGroupFilters(params);
  const navigate = useNavigate();
  const { message } = AntApp.useApp();
  const [searchText, setSearchText] = useState(filters.q);
  const debouncedSearch = useDebouncedValue(searchText.trim());
  const [selected, setSelected] = useState<string[]>([]);

  const setFilters = (patch: Partial<GroupFilters>, resetPage = true) => {
    const next = { ...filters, ...patch, page: resetPage ? 1 : patch.page ?? filters.page };
    setParams(updateParams(params, groupFiltersToParams(next)), { replace: true });
  };

  // 输入停顿 300ms 后再写 URL 查询，不用再按回车。
  useEffect(() => {
    if (debouncedSearch !== filters.q) setFilters({ q: debouncedSearch });
    // 只在输入停顿后同步一次，filters 每次渲染都是新对象，不放进依赖。
  }, [debouncedSearch]);
  // 浏览器后退改了 URL 时，把输入框同步回来。
  useEffect(() => {
    setSearchText((current) => (current.trim() === filters.q ? current : filters.q));
  }, [filters.q]);

  const query = useApiQuery<Paged<GroupRow>>({
    queryKey: ["groups", groupListQuery(filters)],
    fetcher: (signal) => directoryApi.groups(groupListQuery(filters), signal),
    invalidation: { resources: RESOURCES },
  });
  const summaryQuery = useApiQuery<GroupsSummary>({
    queryKey: ["groups-summary"],
    fetcher: (signal) => directoryApi.groupsSummary(signal),
    invalidation: { resources: RESOURCES },
  });
  const reloadAll = () => {
    query.reload();
    summaryQuery.reload();
  };
  const summary = summaryQuery.data;
  const filterCount = activeFilterCount(filters as unknown as Record<string, unknown>, ["q", "agent", "overrides", "active"]);
  const resetFilters = () => {
    setSearchText("");
    setParams(updateParams(params, groupFiltersToParams({ sort: filters.sort })), { replace: true });
  };

  const columns: ColumnsType<GroupRow> = [
    {
      title: "群",
      render: (_, row) => (
        <div className="dir-entity">
          <ActivityDot value={row.lastActiveAt} />
          <div className="dir-cell-stack">
            <Link to={`/groups/${row.groupId}`} className="dir-entity-name">
              {groupDisplayName(row)}
            </Link>
            <Space size={6} wrap>
              <CopyId value={row.groupId} label="群号" />
              {row.personaEnabled && <Tag className="dir-mini-tag">人设</Tag>}
              <Tag className="dir-mini-tag dir-xs-only" color={row.agentEnabled ? "green" : undefined}>Agent {row.agentEnabled ? "开" : "关"}</Tag>
            </Space>
          </div>
        </div>
      ),
    },
    {
      title: "成员",
      width: 120,
      responsive: ["md"],
      render: (_, row) => (
        <Tooltip title={`近 7 天出现过 ${row.activeMembers7d} 人（${percent(row.activeMembers7d, row.memberCount)}%）`}>
          <div className="dir-cell-stack">
            <span className="dir-num">{row.memberCount}</span>
            <Text type="secondary" className="dir-sub">7 天 {row.activeMembers7d} 人活跃</Text>
          </div>
        </Tooltip>
      ),
    },
    {
      title: "Agent",
      width: 140,
      responsive: ["sm"],
      render: (_, row) => (
        <div className="dir-cell-stack">
          <Tag color={row.agentEnabled ? "green" : "default"}>{row.agentEnabled ? "开启" : "关闭"}</Tag>
          {row.agentEnabled && (
            <Text type="secondary" className="dir-sub">
              发言 <RelativeTime value={row.lastAgentAt} empty="还没说过话" />
            </Text>
          )}
        </div>
      ),
    },
    {
      title: "今日签到",
      dataIndex: "checkinsToday",
      width: 96,
      responsive: ["lg"],
      render: (value: number) => <span className={value ? "dir-num" : "dir-num is-muted"}>{value}</span>,
    },
    {
      title: "单独设置",
      width: 150,
      responsive: ["md"],
      render: (_, row) => {
        if (!row.overrideCount && !row.memberOverrideCount) return <Text type="secondary">无</Text>;
        return (
          <Space size={4} wrap>
            {row.overrideCount > 0 && (
              <Tooltip title={row.disabledFeatures?.length ? `群里关掉了：${row.disabledFeatures.map((key) => featureLabel(key)).join("、")}` : "群功能有单独设置"}>
                <Tag color="orange">群 {row.overrideCount}</Tag>
              </Tooltip>
            )}
            {row.memberOverrideCount > 0 && (
              <Tooltip title="成员级覆盖条数，点开群看“覆盖一览”">
                <Tag color="purple">成员 {row.memberOverrideCount}</Tag>
              </Tooltip>
            )}
          </Space>
        );
      },
    },
    {
      title: "最近活跃",
      dataIndex: "lastActiveAt",
      width: 120,
      responsive: ["sm"],
      render: (value: string | null) => <RelativeTime value={value} />,
    },
    {
      title: "",
      width: 56,
      render: (_, row) => (
        <Dropdown
          trigger={["click"]}
          menu={{
            items: [
              { key: "overview", icon: <TeamOutlined />, label: "群概览" },
              { key: "agent", icon: <ApiOutlined />, label: "Agent 工作台" },
              { key: "members", icon: <SafetyOutlined />, label: "成员与权限" },
              { type: "divider" },
              { key: "copy", label: "复制群号" },
            ],
            onClick: ({ key, domEvent }) => {
              domEvent.stopPropagation();
              if (key === "agent") navigate(`/agent/${row.groupId}`);
              else if (key === "members") navigate(`/groups/${row.groupId}?group.tab=members`);
              else if (key === "copy") void navigator.clipboard?.writeText(row.groupId).then(() => message.success("已复制群号"));
              else navigate(`/groups/${row.groupId}`);
            },
          }}
        >
          <Button type="text" size="small" icon={<MoreOutlined />} aria-label={`${groupDisplayName(row)} 的更多操作`} onClick={(event) => event.stopPropagation()} />
        </Dropdown>
      ),
    },
  ];

  return (
    <div className="sbx-page dir-page">
      <div className="sbx-heading">
        <h2 className="sbx-title">群组</h2>
        <p>点开一个群看概览、成员和单独设置；Agent 的人设、记忆与调试在每个群的 Agent 工作台</p>
      </div>
      {summary ? (
        <DirectoryStatusBar
          label="群组状态"
          tone={summary.total === 0 ? "warn" : "ok"}
          title={`${summary.total} 个群 · ${summary.members} 个成员身份`}
          detail={summary.total === 0 ? "Bot 还没在任何群里收到消息" : `24 小时内 ${summary.active24h} 个群有动静，7 天内 ${summary.active7d} 个`}
          refreshing={summaryQuery.refreshing || query.refreshing}
          onRefresh={reloadAll}
          chips={[
            { key: "on", label: `Agent 开启 ${summary.agentEnabled}`, color: "green", active: filters.agent === "on", onClick: () => setFilters({ agent: filters.agent === "on" ? "" : "on" }) },
            { key: "off", label: `Agent 关闭 ${summary.total - summary.agentEnabled}`, active: filters.agent === "off", onClick: () => setFilters({ agent: filters.agent === "off" ? "" : "off" }) },
            { key: "active", label: `今日活跃 ${summary.active24h}`, color: "processing", active: filters.active === "24h", onClick: () => setFilters({ active: filters.active === "24h" ? "" : "24h" }) },
            { key: "overrides", label: `有单独设置 ${summary.withOverrides}`, color: summary.withOverrides ? "orange" : undefined, active: filters.overrides === "any", onClick: () => setFilters({ overrides: filters.overrides === "any" ? "" : "any" }) },
            { key: "checkins", label: `今日签到 ${summary.checkinsToday}`, hint: "所有群今天签到的人次" },
          ]}
        />
      ) : summaryQuery.error ? null : <Skeleton.Input active block style={{ height: 64 }} />}

      <div className="dir-toolbar" role="search">
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜群名或群号"
          value={searchText}
          onChange={(event) => setSearchText(event.target.value)}
          className="dir-search"
          aria-label="搜群名或群号"
        />
        <Segmented
          value={filters.agent}
          onChange={(value) => setFilters({ agent: value as GroupFilters["agent"] })}
          options={[
            { value: "", label: "全部" },
            { value: "on", label: "Agent 开" },
            { value: "off", label: "Agent 关" },
          ]}
        />
        <Select
          value={filters.overrides}
          onChange={(value) => setFilters({ overrides: value })}
          options={GROUP_OVERRIDE_OPTIONS.map((item) => ({ ...item }))}
          className="dir-select"
          aria-label="按单独设置筛选"
        />
        <Select
          value={filters.active}
          onChange={(value) => setFilters({ active: value })}
          options={ACTIVITY_OPTIONS}
          className="dir-select"
          aria-label="按活跃筛选"
        />
        <Select
          value={filters.sort}
          onChange={(value) => setFilters({ sort: value })}
          options={GROUP_SORT_OPTIONS.map((item) => ({ value: item.value, label: `排序：${item.label}` }))}
          className="dir-select"
          aria-label="排序"
        />
        <FilterResetButton count={filterCount} onReset={resetFilters} />
        <span className="dir-count">
          {query.data ? <>共 <Badge count={query.data.total} showZero overflowCount={9999} color="var(--accent, #e4558a)" /> 个</> : null}
        </span>
      </div>

      <BatchBar
        count={selected.length}
        noun="群"
        onClear={() => setSelected([])}
        onApply={async (feature, override) => {
          await directoryApi.batchGroups(selected, feature, override);
          reloadAll();
        }}
      />

      <div className="dir-card">
        {query.error && !query.data
          ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
          : (
            <Table
              rowKey="groupId"
              loading={query.initialLoading || query.transitioning}
              columns={columns}
              dataSource={query.data?.rows ?? []}
              rowSelection={{
                selectedRowKeys: selected,
                onChange: (keys) => setSelected(keys.map(String)),
                preserveSelectedRowKeys: true,
              }}
              rowClassName={() => "dir-row"}
              onRow={(row) => clickableRow(() => navigate(`/groups/${row.groupId}`), `打开 ${groupDisplayName(row)}`) as React.HTMLAttributes<HTMLTableRowElement>}
              locale={{
                emptyText: filterCount
                  ? <AdminEmpty description="没有符合筛选的群" action={<Button size="small" onClick={resetFilters}>清除筛选</Button>} />
                  : <AdminEmpty description="还没有群。把 Bot 拉进群并有人说话后，这里会出现" />,
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
      </div>
    </div>
  );
}
