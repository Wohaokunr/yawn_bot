import { CrownOutlined, FireOutlined, RightOutlined, SearchOutlined, TeamOutlined } from "@ant-design/icons";
import { Empty, Input, Segmented, Skeleton, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type JoinedGroup, spaceApi } from "./me-space-api";
import { formatQqTime, groupRoleLabel } from "./me-meta";
import { MeSection, StatTile, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";

const { Text } = Typography;

type Filter = "all" | "managed" | "unchecked";

function GroupRow({ group }: { group: JoinedGroup }): React.JSX.Element {
  const body = <>
    <span className="me-group-avatar" aria-hidden="true">{(group.groupName || "群").slice(0, 1)}</span>
    <span className="me-group-main">
      <span className="me-group-name">{group.groupName || `群 ${group.groupId}`}</span>
      <span className="me-group-meta">
        <Tag className={`me-role-tag is-${group.role}`}>{groupRoleLabel(group.role)}</Tag>
        <span>{group.memberCount} 名成员</span>
        {group.checkin && group.checkin.streakDays > 0 && <Tag icon={<FireOutlined />} color="orange">连签 {group.checkin.streakDays}</Tag>}
        {group.checkin?.checkedToday ? <Tag color="success">今天签过</Tag> : <Tag>今天没签</Tag>}
        {!group.agentEnabled && <Tag>群里的 AI 聊天关着</Tag>}
        {group.featuresOff.length > 0 && <Tooltip title={`对你关掉的：${group.featuresOff.map((item) => item.label).join("、")}`}><Tag color="default">{group.featuresOff.length} 项功能对你关着</Tag></Tooltip>}
      </span>
      <Text type="secondary" className="me-hint">最近活跃 {formatQqTime(group.lastActiveAt)}{group.checkin ? ` · 累计签到 ${group.checkin.totalDays} 天 · 群好感 ${group.checkin.groupAffinity}` : ""}</Text>
    </span>
  </>;
  return group.managed ? (
    <Link to={`/me/groups/${group.groupId}`} className="me-group-row">
      {body}
      <span className="me-group-actions" aria-hidden="true"><CrownOutlined /> 管理</span>
      <RightOutlined className="me-card-arrow" />
    </Link>
  ) : <div className="me-group-row is-static">{body}</div>;
}

export function MyGroupsPage(): React.JSX.Element {
  const { query, data } = useResource<JoinedGroup[]>(["me", "joined-groups"], () => spaceApi.joinedGroups());
  const [filter, setFilter] = useState<Filter>("all");
  const [keyword, setKeyword] = useState("");
  const rows = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return (data ?? []).filter((group) => {
      if (needle && !`${group.groupName ?? ""} ${group.groupId}`.toLowerCase().includes(needle)) return false;
      if (filter === "managed") return group.managed;
      if (filter === "unchecked") return !group.checkin?.checkedToday;
      return true;
    });
  }, [data, filter, keyword]);
  const managed = (data ?? []).filter((group) => group.managed).length;
  const unchecked = (data ?? []).filter((group) => !group.checkin?.checkedToday).length;

  return <div className="me-page">
    <PageHeader title="我在的群" subtitle="Bot 所在、你也在的群。你是群主或管理员的群可以点进去管理。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 5 }} /> : <>
      <div className="me-stat-row">
        <StatTile label="在的群" value={data.length} tone="brand" />
        <StatTile label="我管理的" value={managed} />
        <StatTile label="今天还没签到" value={unchecked} tone={unchecked ? "warning" : "success"} />
      </div>
      <MeSection icon={<TeamOutlined />} title="群列表" extra={<Segmented size="small" value={filter} onChange={setFilter} options={[{ value: "all", label: "全部" }, { value: "managed", label: `我管理的 ${managed}` }, { value: "unchecked", label: `没签到 ${unchecked}` }]} />}>
        <Input allowClear prefix={<SearchOutlined />} placeholder="按群名或群号找" value={keyword} onChange={(event) => setKeyword(event.target.value)} className="me-list-search" />
        {rows.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={data.length ? "没有符合条件的群" : "Bot 还没在你在的群里见过你。在群里说句话就会同步。"} /> : (
          <ul className="me-group-list">{rows.map((group) => <li key={group.groupId}><GroupRow group={group} /></li>)}</ul>
        )}
      </MeSection>
    </>}
  </div>;
}
