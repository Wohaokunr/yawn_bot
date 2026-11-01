import { SearchOutlined } from "@ant-design/icons";
import { Badge, Button, Input, Select, Skeleton, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AdminEmpty, QueryErrorAlert, useApiQuery } from "../shared";
import {
  activeFilterCount,
  ACTIVITY_OPTIONS,
  parseUserFilters,
  updateParams,
  USER_OVERRIDE_OPTIONS,
  USER_SORT_OPTIONS,
  userFiltersToParams,
  userListQuery,
} from "./directory-meta";
import type { UserFilters } from "./directory-meta";
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
import { UserProfileDrawer } from "./UserProfileDrawer";
import type { UserRow, UsersSummary } from "./types";

const { Text } = Typography;
const RESOURCES = ["global_user_feature", "user_affinity", "user_feature", "user_private_agent"];

export function UsersPage(): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const filters = parseUserFilters(params);
  const openUser = params.get("user");
  const [searchText, setSearchText] = useState(filters.q);
  const debounced = useDebouncedValue(searchText.trim());
  const [selected, setSelected] = useState<string[]>([]);

  const setFilters = (patch: Partial<UserFilters>, resetPage = true) => {
    const next = { ...filters, ...patch, page: resetPage ? 1 : patch.page ?? filters.page };
    setParams(updateParams(params, userFiltersToParams(next)), { replace: true });
  };
  useEffect(() => {
    if (debounced !== filters.q) setFilters({ q: debounced });
  }, [debounced]);
  useEffect(() => {
    setSearchText((current) => (current.trim() === filters.q ? current : filters.q));
  }, [filters.q]);
  const setUser = (userId: string | null) => setParams(updateParams(params, { user: userId, "user.tab": null }));

  const query = useApiQuery<Paged<UserRow>>({
    queryKey: ["users", userListQuery(filters)],
    fetcher: (signal) => directoryApi.users(userListQuery(filters), signal),
    invalidation: { resources: RESOURCES },
  });
  const summaryQuery = useApiQuery<UsersSummary>({
    queryKey: ["users-summary"],
    fetcher: (signal) => directoryApi.usersSummary(signal),
    invalidation: { resources: RESOURCES },
  });
  const summary = summaryQuery.data;
  const filterCount = activeFilterCount(filters as unknown as Record<string, unknown>, ["q", "overrides", "activity"]);
  const resetFilters = () => {
    setSearchText("");
    setParams(updateParams(params, userFiltersToParams({ sort: filters.sort })), { replace: true });
  };
  const reloadAll = () => {
    query.reload();
    summaryQuery.reload();
  };

  const columns: ColumnsType<UserRow> = [
    {
      title: "用户",
      render: (_, row) => (
        <div className="dir-entity">
          <ActivityDot value={row.lastInteractionAt} />
          <div className="dir-cell-stack">
            <button type="button" className="dir-link dir-entity-name" onClick={() => setUser(row.userId)}>{row.nickname || `QQ ${row.userId}`}</button>
            <CopyId value={row.userId} label="QQ" />
          </div>
        </div>
      ),
    },
    {
      title: "好感",
      width: 130,
      render: (_, row) => (
        <Space size={6}>
          {row.affinityLevel && <Tag color={row.affinity > 0 ? "magenta" : undefined}>{row.affinityLevel}</Tag>}
          <span className="dir-num">{row.affinity}</span>
        </Space>
      ),
    },
    {
      title: "所在群",
      dataIndex: "groupCount",
      width: 90,
      responsive: ["md"],
      render: (value: number) => (value ? <span className="dir-num">{value}</span> : <Tooltip title="只私聊过，或还没在群里说过话"><Text type="secondary">0</Text></Tooltip>),
    },
    {
      title: "单独设置",
      width: 150,
      responsive: ["md"],
      render: (_, row) => {
        if (!row.overrideCount && !row.memberOverrideCount) return <Text type="secondary">无</Text>;
        return (
          <Space size={4} wrap>
            {row.overrideCount > 0 && <Tooltip title="私聊功能的单独设置"><Tag color="orange">私聊 {row.overrideCount}</Tag></Tooltip>}
            {row.memberOverrideCount > 0 && <Tooltip title="在各群被单独设置的条数"><Tag color="purple">群内 {row.memberOverrideCount}</Tag></Tooltip>}
          </Space>
        );
      },
    },
    { title: "最近互动", dataIndex: "lastInteractionAt", width: 120, responsive: ["sm"], render: (value: string | null) => <RelativeTime value={value} /> },
    { title: "首次认识", dataIndex: "firstInteractionAt", width: 120, responsive: ["lg"], render: (value: string | null) => <RelativeTime value={value} empty="—" /> },
  ];

  return (
    <div className="sbx-page dir-page">
      <div className="sbx-heading">
        <h2 className="sbx-title">用户</h2>
        <p>Bot 认识的每个人：好感与签到、所在的群、私聊功能和私聊 Agent。私聊功能开关只管私聊，群里的限制在各群成员里设</p>
      </div>
      {summary ? (
        <DirectoryStatusBar
          label="用户状态"
          tone={summary.total === 0 ? "warn" : "ok"}
          title={`${summary.total} 位用户`}
          detail={`24 小时内 ${summary.active24h} 人互动过，7 天内 ${summary.active7d} 人`}
          refreshing={summaryQuery.refreshing || query.refreshing}
          onRefresh={reloadAll}
          chips={[
            { key: "active", label: `今日活跃 ${summary.active24h}`, color: "processing", active: filters.activity === "24h", onClick: () => setFilters({ activity: filters.activity === "24h" ? "" : "24h" }) },
            { key: "private", label: `私聊有单独设置 ${summary.withOverrides}`, color: summary.withOverrides ? "orange" : undefined, active: filters.overrides === "private", onClick: () => setFilters({ overrides: filters.overrides === "private" ? "" : "private" }) },
            { key: "member", label: `群内有单独设置 ${summary.withMemberOverrides}`, color: summary.withMemberOverrides ? "purple" : undefined, active: filters.overrides === "member", onClick: () => setFilters({ overrides: filters.overrides === "member" ? "" : "member" }) },
            { key: "checkins", label: `今日签到 ${summary.checkinsToday} 人` },
            ...(summary.privateAgentPaused ? [{ key: "paused", label: `私聊 Agent 暂停 ${summary.privateAgentPaused}`, color: "gold", hint: "这些人发过 /退出，或被管理员暂停了自动回复" }] : []),
          ]}
        />
      ) : summaryQuery.error ? null : <Skeleton.Input active block style={{ height: 64 }} />}

      <div className="dir-toolbar" role="search">
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜昵称或 QQ"
          value={searchText}
          onChange={(event) => setSearchText(event.target.value)}
          className="dir-search"
          aria-label="搜昵称或 QQ"
        />
        <Select value={filters.overrides} onChange={(value) => setFilters({ overrides: value })} options={USER_OVERRIDE_OPTIONS.map((item) => ({ ...item }))} className="dir-select" aria-label="按单独设置筛选" />
        <Select value={filters.activity} onChange={(value) => setFilters({ activity: value })} options={ACTIVITY_OPTIONS} className="dir-select" aria-label="按活跃筛选" />
        <Select
          value={filters.sort}
          onChange={(value) => setFilters({ sort: value })}
          options={USER_SORT_OPTIONS.map((item) => ({ value: item.value, label: `排序：${item.label}` }))}
          className="dir-select"
          aria-label="排序"
        />
        <FilterResetButton count={filterCount} onReset={resetFilters} />
        <span className="dir-count">
          {query.data ? <>共 <Badge count={query.data.total} showZero overflowCount={99999} color="var(--accent, #e4558a)" /> 人</> : null}
        </span>
      </div>

      <BatchBar
        count={selected.length}
        noun="用户的私聊功能"
        excludeFeatures={["group_agent"]}
        onClear={() => setSelected([])}
        onApply={async (feature, override) => {
          await directoryApi.batchUsers(selected, feature, override);
          reloadAll();
        }}
      />

      <div className="dir-card">
        {query.error && !query.data
          ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
          : (
            <Table
              rowKey="userId"
              loading={query.initialLoading || query.transitioning}
              dataSource={query.data?.rows ?? []}
              columns={columns}
              rowSelection={{ selectedRowKeys: selected, onChange: (keys) => setSelected(keys.map(String)), preserveSelectedRowKeys: true }}
              rowClassName={(row) => (row.userId === openUser ? "dir-row is-open" : "dir-row")}
              onRow={(row) => clickableRow(() => setUser(row.userId), `打开 ${row.nickname || row.userId} 的档案`) as React.HTMLAttributes<HTMLTableRowElement>}
              locale={{
                emptyText: filterCount
                  ? <AdminEmpty description="没有符合筛选的用户" action={<Button size="small" onClick={resetFilters}>清除筛选</Button>} />
                  : <AdminEmpty description="还没有用户。有人和 Bot 说话后会出现在这里" />,
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
      <UserProfileDrawer userId={openUser} onClose={() => setUser(null)} />
    </div>
  );
}
