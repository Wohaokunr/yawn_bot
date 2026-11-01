import { ApartmentOutlined, AppstoreOutlined, LockOutlined, RightOutlined, SafetyCertificateOutlined, SmileOutlined, TeamOutlined } from "@ant-design/icons";
import { Alert, Empty, Skeleton, Space, Tag, Typography } from "antd";
import { Link } from "react-router-dom";
import { PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import { meApi } from "./me-api";
import { formatQqTime, groupRoleLabel } from "./me-meta";
import "./me.css";

const { Text } = Typography;

export function MeHomePage(): React.JSX.Element {
  const profile = useApiQuery({ queryKey: ["me"], fetcher: () => meApi.profile() });
  const groups = useApiQuery({ queryKey: ["me", "groups"], fetcher: () => meApi.groups() });
  const me = profile.data;
  const title = me?.nickname ? `你好，${me.nickname}` : "我的空间";

  return <div className="me-page">
    <PageHeader
      title={title}
      subtitle={me ? `QQ ${me.userId} · 你在 ${me.groupCount} 个群里，其中 ${me.managedGroupCount} 个是群主/管理员` : "正在读取你的信息"}
      onRefresh={() => { profile.reload(); groups.reload(); }}
      refreshing={profile.refreshing || groups.refreshing}
    />
    {profile.error && !me && <QueryErrorAlert error={profile.error} onRetry={profile.reload} />}

    <div className="me-cards">
      <Link to="/me/persona" className="me-card me-card-link">
        <span className="me-card-icon"><SmileOutlined /></span>
        <span className="me-card-body">
          <strong>私聊人设</strong>
          {me?.persona ? <>
            <span className="me-card-main">{me.persona.name || "默认人设"}{me.persona.locked && <Tag className="me-inline-tag">已锁定</Tag>}</span>
            <Text type="secondary" className="me-card-sub" ellipsis>{me.persona.sourceLabel} · {me.persona.summary}</Text>
          </> : <Text type="secondary" className="me-card-sub">调整 Bot 和你私聊时的自称、称呼和说话方式</Text>}
        </span>
        <RightOutlined className="me-card-arrow" />
      </Link>
      <Link to="/me/account" className="me-card me-card-link">
        <span className="me-card-icon"><LockOutlined /></span>
        <span className="me-card-body">
          <strong>账号安全</strong>
          <span className="me-card-main">{me?.account?.passwordChangedAt ? `密码设于 ${formatQqTime(me.account.passwordChangedAt)}` : "—"}</span>
          <Text type="secondary" className="me-card-sub">上次登录 {formatQqTime(me?.account?.lastLoginAt)} · 改密码、退出所有设备</Text>
        </span>
        <RightOutlined className="me-card-arrow" />
      </Link>
    </div>

    <section className="me-section">
      <div className="me-section-head">
        <Space size={8}><TeamOutlined /><strong>我管理的群</strong></Space>
        <Text type="secondary" className="me-hint">只列出你是群主或管理员的群；打开时会再向 QQ 确认一次身份</Text>
      </div>
      {groups.error && !groups.data && <QueryErrorAlert error={groups.error} onRetry={groups.reload} />}
      {groups.loading && !groups.data && <Skeleton active paragraph={{ rows: 3 }} />}
      {groups.data && groups.data.length === 0 && (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={<span>你目前不是任何 Bot 所在群的群主或管理员。<br />成为管理员后，在群里说句话 Bot 就会同步身份。</span>}
        />
      )}
      {groups.data && groups.data.length > 0 && (
        <ul className="me-group-list">
          {groups.data.map((group) => (
            <li key={group.groupId}>
              <Link to={`/me/groups/${group.groupId}`} className="me-group-row">
                <span className="me-group-avatar" aria-hidden="true">{(group.groupName || "群").slice(0, 1)}</span>
                <span className="me-group-main">
                  <span className="me-group-name">{group.groupName || `群 ${group.groupId}`}</span>
                  <span className="me-group-meta">
                    <Tag className={`me-role-tag is-${group.role}`}>{groupRoleLabel(group.role)}</Tag>
                    <span>{group.memberCount} 名成员</span>
                    <span>·</span>
                    <span>{group.enabledFeatures}/{group.featureCount} 项功能开启</span>
                    {!group.agentEnabled && <Tag className="me-inline-tag">Agent 已关</Tag>}
                  </span>
                </span>
                <span className="me-group-actions" aria-hidden="true">
                  <span><ApartmentOutlined /> 关系</span>
                  <span><AppstoreOutlined /> 功能</span>
                </span>
                <RightOutlined className="me-card-arrow" />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>

    <Alert
      className="me-safety-note"
      type="info"
      showIcon
      icon={<SafetyCertificateOutlined />}
      message="你在这里只能看到自己的东西"
      description="群关系只读、精简显示；群功能开关和群里的 /面板 是同一份设置；私聊人设只影响 Bot 和你一个人的私聊。忘记密码就私聊 Bot 发 /网页密码。"
    />
  </div>;
}
