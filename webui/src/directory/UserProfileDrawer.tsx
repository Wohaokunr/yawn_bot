import { CommentOutlined, HeartOutlined, LockOutlined, TeamOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Drawer, Empty, Grid, Progress, Skeleton, Space, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link, useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import type { FeatureState } from "../types";
import { AffinityPanel, PrivateAgentPanel } from "../user-insights";
import { groupDisplayName, parseProfileTab, updateParams } from "./directory-meta";
import type { ProfileTab } from "./directory-meta";
import { directoryApi } from "./directory-api";
import { CopyId, FeatureTable, RelativeTime, RoleTag } from "./components";
import type { Membership, UserProfile } from "./types";

const { Text } = Typography;

/**
 * 用户档案抽屉：群成员表和用户页共用。
 * 状态在 URL：`user=<QQ>` 打开，`user.tab=` 记住标签，可直接分享链接。
 * 在群里打开时多一个“本群”标签，直接改这位成员在本群的单独设置。
 */
export function UserProfileDrawer({
  userId,
  groupId,
  groupFeatures,
  onChanged,
  onClose,
}: {
  userId: string | null;
  groupId?: string;
  groupFeatures?: FeatureState[];
  /** 档案里改了覆盖后通知外层（如群详情的覆盖一览）刷新。 */
  onChanged?: () => void;
  onClose: () => void;
}): React.JSX.Element {
  const screens = Grid.useBreakpoint();
  return (
    <Drawer
      open={!!userId}
      width={screens.md ? 760 : "100%"}
      onClose={onClose}
      destroyOnHidden
      title={null}
      closable={false}
      styles={{ body: { padding: 0 } }}
    >
      {userId && <ProfileBody userId={userId} groupId={groupId} groupFeatures={groupFeatures} onChanged={onChanged} onClose={onClose} />}
    </Drawer>
  );
}

function ProfileBody({
  userId,
  groupId,
  groupFeatures,
  onChanged,
  onClose,
}: {
  userId: string;
  groupId?: string;
  groupFeatures?: FeatureState[];
  onChanged?: () => void;
  onClose: () => void;
}): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const tab = parseProfileTab(params.get("user.tab"), Boolean(groupId));
  const query = useApiQuery<UserProfile>({
    queryKey: ["user-profile", userId],
    fetcher: (signal) => directoryApi.user(userId, signal),
    invalidation: { resources: ["user_feature", "global_user_feature", "user_affinity"] },
  });
  const setTab = (next: ProfileTab) => setParams(updateParams(params, { "user.tab": next }), { replace: true });
  const changed = () => {
    query.reload();
    onChanged?.();
  };

  if (query.error && !query.data) {
    return <div className="dir-profile-pad"><QueryErrorAlert error={query.error} onRetry={query.reload} /></div>;
  }
  const profile = query.data;
  if (!profile) return <div className="dir-profile-pad"><Skeleton active avatar /></div>;
  const membership = groupId ? profile.memberships.find((item) => item.groupId === groupId) : undefined;
  const name = membership?.groupNickname || profile.nickname || `QQ ${profile.userId}`;

  return (
    <div className="dir-profile">
      <header className="dir-profile-head">
        <div className="dir-avatar" aria-hidden="true">{name.slice(0, 1)}</div>
        <div className="dir-profile-title">
          <Space size={8} wrap>
            <strong>{name}</strong>
            {membership && <RoleTag role={membership.role} />}
            {membership?.title && <Tag color="magenta">{membership.title}</Tag>}
          </Space>
          <Space size={10} wrap className="dir-profile-sub">
            <CopyId value={profile.userId} label="QQ" />
            {membership?.groupNickname && profile.nickname && <Text type="secondary">QQ 昵称 {profile.nickname}</Text>}
            <Text type="secondary">最近互动 <RelativeTime value={profile.lastInteractionAt} /></Text>
          </Space>
        </div>
        <Button type="text" onClick={onClose} aria-label="关闭档案">✕</Button>
      </header>
      <div className="dir-profile-stats">
        <div>
          <span className="sbx-metric-label">好感</span>
          <Space size={6}>
            <Tag color="magenta">{profile.level.name}</Tag>
            <strong>{profile.affinity}</strong>
          </Space>
          <Tooltip title={profile.level.nextLevel ? `距「${profile.level.nextLevel}」还差 ${profile.level.toNext ?? 0}` : "已是最高等级"}>
            <Progress percent={Math.round(profile.level.progress * 100)} size="small" showInfo={false} strokeColor="var(--accent, #e4558a)" />
          </Tooltip>
        </div>
        <div>
          <span className="sbx-metric-label">所在群</span>
          <strong>{profile.groupCount}</strong>
          <Text type="secondary" className="dir-sub">今日签到 {profile.checkedTodayCount} 个群</Text>
        </div>
        <div>
          <span className="sbx-metric-label">单独设置</span>
          <strong>{profile.privateOverrides + profile.memberOverrides}</strong>
          <Text type="secondary" className="dir-sub">私聊 {profile.privateOverrides} · 群内 {profile.memberOverrides}</Text>
        </div>
        <div>
          <span className="sbx-metric-label">首次认识</span>
          <strong><RelativeTime value={profile.firstInteractionAt} empty="—" /></strong>
        </div>
      </div>
      <Tabs
        className="dir-profile-tabs"
        activeKey={tab}
        onChange={(key) => setTab(key as ProfileTab)}
        destroyOnHidden
        items={[
          ...(groupId ? [{
            key: "group",
            label: <span><TeamOutlined /> 本群</span>,
            children: membership
              ? <GroupMemberFeatures groupId={groupId} userId={userId} membership={membership} groupFeatures={groupFeatures} onChanged={changed} />
              : <Empty description="这位用户不在本群的成员记录里" />,
          }] : []),
          {
            key: "groups",
            label: <span><UserOutlined /> 所在群 {profile.groupCount}</span>,
            children: <MembershipTable userId={userId} memberships={profile.memberships} onChanged={changed} />,
          },
          { key: "private", label: <span><LockOutlined /> 私聊功能</span>, children: <PrivateFeatures userId={userId} onChanged={changed} /> },
          { key: "affinity", label: <span><HeartOutlined /> 好感与签到</span>, children: <AffinityPanel userId={userId} /> },
          { key: "agent", label: <span><CommentOutlined /> 私聊 Agent</span>, children: <PrivateAgentPanel userId={userId} /> },
        ]}
      />
    </div>
  );
}

function GroupMemberFeatures({
  groupId,
  userId,
  membership,
  groupFeatures,
  onChanged,
}: {
  groupId: string;
  userId: string;
  membership: Membership;
  groupFeatures?: FeatureState[];
  onChanged: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<FeatureState[]>({
    queryKey: ["member-features", groupId, userId],
    fetcher: (signal) => directoryApi.memberFeatures(groupId, userId, signal),
    invalidation: { resources: ["user_feature", "group_feature", "agent_config"], scope: { groupId } },
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active />;
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div className="dir-membership-facts">
        <span>入群 <RelativeTime value={membership.firstSeenAt} empty="—" /></span>
        <span>最近出现 <RelativeTime value={membership.lastSeenAt} /></span>
        <span>{membership.streakDays > 0 ? `连签 ${membership.streakDays} 天` : "没在连签"} · {membership.checkedToday ? "今日已签" : "今日未签"}</span>
        <span>群好感 {membership.groupAffinity}</span>
      </div>
      <FeatureTable
        compact
        rows={query.data}
        parent={groupFeatures}
        emptyHint="全部跟随群设置"
        resetLabel="全部跟随群设置"
        resetHint="清掉这位成员在本群的所有单独设置。"
        onChange={async (feature, override) => {
          const row = await directoryApi.setMemberFeature(groupId, userId, feature, override);
          query.reload();
          onChanged();
          message.success(`「${row.name}」已更新`);
        }}
        onReset={async () => {
          const result = await directoryApi.resetMemberFeatures(groupId, userId);
          message.success(result.removed ? `已清掉 ${result.removed} 项` : "没有单独设置");
          query.reload();
          onChanged();
        }}
      />
    </Space>
  );
}

function MembershipTable({ userId, memberships, onChanged }: { userId: string; memberships: Membership[]; onChanged: () => void }): React.JSX.Element {
  if (memberships.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没在任何群里出现过，可能只私聊过" />;
  }
  const columns: ColumnsType<Membership> = [
    {
      title: "群",
      render: (_, row) => (
        <div className="dir-cell-stack">
          <Space size={6} wrap>
            <Link to={`/groups/${row.groupId}?group.tab=members&user=${userId}`}>{groupDisplayName(row)}</Link>
            {row.role !== "member" && <RoleTag role={row.role} />}
            {!row.agentEnabled && <Tooltip title="这个群的 Agent 关着"><Tag className="dir-mini-tag">Agent 关</Tag></Tooltip>}
          </Space>
          <Text type="secondary" className="dir-sub">{row.groupNickname ? `群名片 ${row.groupNickname} · ` : ""}最近 <RelativeTime value={row.lastSeenAt} /></Text>
        </div>
      ),
    },
    {
      title: "签到",
      width: 120,
      render: (_, row) => (
        <div className="dir-cell-stack">
          <span>{row.streakDays > 0 ? `连签 ${row.streakDays}` : <Text type="secondary">—</Text>}</span>
          <Text type="secondary" className="dir-sub">{row.checkedToday ? "今日已签" : "今日未签"}</Text>
        </div>
      ),
    },
    { title: "群好感", dataIndex: "groupAffinity", width: 72, responsive: ["sm"] },
    { title: "单独设置", dataIndex: "overrideCount", width: 90, render: (value: number) => (value ? <Tag color="purple">{value} 项</Tag> : <Text type="secondary">无</Text>) },
  ];
  return (
    <Table
      rowKey="groupId"
      size="small"
      pagination={memberships.length > 10 ? { pageSize: 10, showSizeChanger: false } : false}
      dataSource={memberships}
      columns={columns}
      expandable={{
        expandedRowRender: (row) => <InlineMemberFeatures groupId={row.groupId} userId={userId} onChanged={onChanged} />,
        rowExpandable: () => true,
        expandRowByClick: false,
      }}
    />
  );
}

// 在“所在群”里展开某个群，直接改这位成员在那个群的单独设置。
function InlineMemberFeatures({ groupId, userId, onChanged }: { groupId: string; userId: string; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<FeatureState[]>({
    queryKey: ["member-features", groupId, userId],
    fetcher: (signal) => directoryApi.memberFeatures(groupId, userId, signal),
    invalidation: { resources: ["user_feature", "group_feature", "agent_config"], scope: { groupId } },
  });
  const groupQuery = useApiQuery<FeatureState[]>({
    queryKey: ["group-features", groupId],
    fetcher: (signal) => directoryApi.group(groupId, signal).then((group) => group.features),
    invalidation: { resources: ["group_feature", "agent_config"], scope: { groupId } },
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active paragraph={{ rows: 3 }} />;
  return (
    <FeatureTable
      compact
      rows={query.data}
      parent={groupQuery.data ?? undefined}
      emptyHint="全部跟随群设置"
      onChange={async (feature, override) => {
        await directoryApi.setMemberFeature(groupId, userId, feature, override);
        message.success("已更新");
        query.reload();
        onChanged();
      }}
    />
  );
}

function PrivateFeatures({ userId, onChanged }: { userId: string; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<FeatureState[]>({
    queryKey: ["user-features", userId],
    fetcher: (signal) => directoryApi.userFeatures(userId, signal),
    invalidation: { resources: ["global_user_feature"] },
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active />;
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="这里的开关只在私聊里生效"
        description="想在某个群里限制这位用户，到“所在群”展开那个群单独设置，或去群的成员列表。"
      />
      <FeatureTable
        compact
        rows={query.data}
        onChange={async (feature, override) => {
          await directoryApi.setUserFeature(userId, feature, override);
          message.success("私聊功能已更新");
          query.reload();
          onChanged();
        }}
        onReset={async () => {
          const changed = query.data!.filter((row) => row.override !== null);
          for (const row of changed) await directoryApi.setUserFeature(userId, row.key, null);
          message.success(`已恢复 ${changed.length} 项`);
          query.reload();
          onChanged();
        }}
        resetHint="私聊里的所有功能回到默认开启。"
      />
    </Space>
  );
}
