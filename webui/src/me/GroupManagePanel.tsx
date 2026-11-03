// 群管理员在网页上让 Bot 代办群管理（后端 webui/group_manage_routes.py，和私聊工具 group_manage 共用一套权限与记录）。
// 每个写操作都先请后端“预览”出一句话，弹窗确认后再执行；后端会再实时核对两边身份。
import {
  AuditOutlined,
  BellOutlined,
  CheckSquareOutlined,
  ExclamationCircleOutlined,
  LockOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
  SettingOutlined,
  StopOutlined,
  TeamOutlined,
  UnlockOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  Popconfirm,
  Popover,
  Segmented,
  Select,
  Skeleton,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError } from "../api";
import { QueryErrorAlert, useApiQuery } from "../shared";
import {
  type ManageActionBody,
  type ManageActionMeta,
  type ManageAuditRow,
  type ManageMember,
  type ManagePolicy,
  type ManageStatus,
  meApi,
} from "./me-api";
import { MUTE_PRESETS, groupRoleLabel, manageMinutesLabel, manageResultMeta, manageSourceLabel } from "./me-meta";

const { Text, Paragraph } = Typography;
const VIEWS = ["members", "notices", "group", "audit", "policy"] as const;
type View = (typeof VIEWS)[number];

function useManageRunner(groupId: string, status: ManageStatus | null, onDone: () => void) {
  const { message, modal } = AntApp.useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const blocked = useCallback(
    (action: string): string | null => {
      if (!status) return "正在读取权限";
      if (!status.available) return status.problem ?? "现在不能代办";
      return status.actions.find((row) => row.key === action)?.blockedReason ?? null;
    },
    [status],
  );
  const run = useCallback(
    async (body: ManageActionBody, key = body.action): Promise<boolean> => {
      setBusy(key);
      try {
        const preview = await meApi.managePreview(groupId, body);
        const confirmed = await new Promise<boolean>((resolve) => {
          modal.confirm({
            title: preview.risk === "high" ? "这个操作不可撤回" : `确认${preview.label}？`,
            icon: <ExclamationCircleOutlined />,
            content: <div className="me-manage-confirm">
              <p>{preview.summary}</p>
              <Text type="secondary" className="me-hint">执行前会再向 QQ 核对一次你和 Bot 的身份；操作会记进本群的操作记录。</Text>
            </div>,
            okText: preview.risk === "high" ? "确认执行" : "确认",
            okButtonProps: { danger: preview.risk !== "low" },
            cancelText: "取消",
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!confirmed) return false;
        const result = await meApi.manageAction(groupId, { ...body, confirm: true });
        message.success(`已完成：${result.summary}`);
        onDone();
        return true;
      } catch (reason) {
        message.error(reason instanceof ApiError ? reason.message : (reason as Error).message);
        return false;
      } finally {
        setBusy(null);
      }
    },
    [groupId, message, modal, onDone],
  );
  return { run, busy, blocked };
}

type Runner = ReturnType<typeof useManageRunner>;

function GuardedButton({ runner, action, children, ...rest }: { runner: Runner; action: string } & React.ComponentProps<typeof Button>): React.JSX.Element {
  const reason = runner.blocked(action);
  const button = <Button {...rest} disabled={Boolean(reason) || rest.disabled} loading={runner.busy === action || rest.loading}>{children}</Button>;
  return reason ? <Tooltip title={reason}><span className="me-manage-guard">{button}</span></Tooltip> : button;
}

// ── 状态条 ────────────────────────────────────────────────────────────

function StatusStrip({ status }: { status: ManageStatus }): React.JSX.Element {
  const percent = status.dailyLimit ? Math.min(100, Math.round((status.usedToday / status.dailyLimit) * 100)) : 0;
  return <div className="me-manage-strip">
    <div className="me-manage-stat">
      <span className="me-manage-stat-label">你的身份</span>
      <strong><SafetyCertificateOutlined /> {status.yourRole ? groupRoleLabel(status.yourRole) : "未确认"}</strong>
    </div>
    <div className="me-manage-stat">
      <span className="me-manage-stat-label">Bot 的身份</span>
      <strong>{status.botRole ? groupRoleLabel(status.botRole) : "未确认"}</strong>
    </div>
    <div className="me-manage-stat">
      <span className="me-manage-stat-label">今天已代办</span>
      <strong>{status.usedToday}{status.dailyLimit ? ` / ${status.dailyLimit}` : ""}</strong>
      {status.dailyLimit > 0 && <span className="me-manage-meter" aria-hidden="true"><span style={{ width: `${percent}%` }} /></span>}
    </div>
    <div className="me-manage-stat">
      <span className="me-manage-stat-label">现在能做</span>
      <strong>{status.allowedActions.length} 项操作</strong>
    </div>
  </div>;
}

// ── 成员 ──────────────────────────────────────────────────────────────

function MuteButton({ runner, member, maxMinutes }: { runner: Runner; member: ManageMember; maxMinutes: number }): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [minutes, setMinutes] = useState(10);
  const [reason, setReason] = useState("");
  const blocked = runner.blocked("mute");
  const presets = MUTE_PRESETS.filter((item) => item <= maxMinutes);
  const content = <div className="me-manage-pop">
    <div className="me-manage-presets">
      {presets.map((value) => (
        <Button key={value} size="small" type={minutes === value ? "primary" : "default"} onClick={() => setMinutes(value)}>{manageMinutesLabel(value)}</Button>
      ))}
    </div>
    <Space.Compact block>
      <InputNumber min={1} max={maxMinutes} value={minutes} onChange={(value) => setMinutes(Number(value) || 1)} style={{ width: "100%" }} aria-label="禁言分钟数" />
      <Button disabled>分钟</Button>
    </Space.Compact>
    <Input value={reason} onChange={(event) => setReason(event.target.value)} maxLength={80} placeholder="理由（可选，记进操作记录）" />
    <Text type="secondary" className="me-hint">本群一次最多 {manageMinutesLabel(maxMinutes)}</Text>
    <Button
      type="primary"
      danger
      block
      loading={runner.busy === `mute-${member.userId}`}
      onClick={() => {
        setOpen(false);
        void runner.run({ action: "mute", member: member.userId, minutes, reason: reason.trim() || undefined }, `mute-${member.userId}`);
      }}
    >
      禁言 {manageMinutesLabel(minutes)}
    </Button>
  </div>;
  if (blocked) return <Tooltip title={blocked}><span className="me-manage-guard"><Button size="small" icon={<StopOutlined />} disabled>禁言</Button></span></Tooltip>;
  return <Popover open={open} onOpenChange={setOpen} trigger="click" title={`禁言 ${member.name}`} content={content} destroyOnHidden>
    <Button size="small" icon={<StopOutlined />} loading={runner.busy === `mute-${member.userId}`}>禁言</Button>
  </Popover>;
}

function TextActionButton({
  runner, action, member, label, placeholder, maxLength,
}: { runner: Runner; action: "card" | "title"; member: ManageMember; label: string; placeholder: string; maxLength: number }): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(action === "card" ? member.card : member.title ?? "");
  const blocked = runner.blocked(action);
  if (blocked) return <Tooltip title={blocked}><span className="me-manage-guard"><Button size="small" disabled>{label}</Button></span></Tooltip>;
  return <Popover
    open={open}
    onOpenChange={setOpen}
    trigger="click"
    title={`${label} · ${member.name}`}
    destroyOnHidden
    content={<div className="me-manage-pop">
      <Input value={text} onChange={(event) => setText(event.target.value)} maxLength={maxLength} placeholder={placeholder} allowClear />
      <Button type="primary" block onClick={() => { setOpen(false); void runner.run({ action, member: member.userId, text }, `${action}-${member.userId}`); }}>
        {text.trim() ? "保存" : "清空"}
      </Button>
    </div>}
  >
    <Button size="small" loading={runner.busy === `${action}-${member.userId}`}>{label}</Button>
  </Popover>;
}

function MembersView({ groupId, runner, status }: { groupId: string; runner: Runner; status: ManageStatus }): React.JSX.Element {
  const [keyword, setKeyword] = useState("");
  const [mutedOnly, setMutedOnly] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const query = useApiQuery({
    queryKey: ["me", "manage", groupId, "members", refreshKey],
    fetcher: () => meApi.manageMembers(groupId, "", refreshKey > 0),
  });
  const rows = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return (query.data?.rows ?? []).filter((row) =>
      (!mutedOnly || row.mutedUntil)
      && (!needle || `${row.name} ${row.nickname} ${row.card} ${row.userId}`.toLowerCase().includes(needle)));
  }, [query.data, keyword, mutedOnly]);
  const reload = () => setRefreshKey((value) => value + 1);
  const max = status.policy.maxMuteMinutes;
  return <div className="me-manage-view">
    <div className="me-manage-toolbar">
      <Input.Search allowClear value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜群名片、昵称或 QQ 号" className="me-manage-search" />
      <Checkbox checked={mutedOnly} onChange={(event) => setMutedOnly(event.target.checked)}>只看正在禁言的</Checkbox>
      <span className="me-manage-toolbar-spacer" />
      {query.data && <Text type="secondary" className="me-hint">共 {query.data.total} 人 · {query.data.muted} 人禁言中</Text>}
      <Button icon={<ReloadOutlined />} onClick={reload} loading={query.refreshing}>刷新</Button>
    </div>
    {query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Table<ManageMember>
        rowKey="userId"
        size="middle"
        className="me-manage-table"
        loading={query.loading}
        dataSource={rows}
        pagination={{ pageSize: 20, showSizeChanger: false, hideOnSinglePage: true }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={mutedOnly ? "现在没人被禁言" : "没有匹配的成员"} /> }}
        scroll={{ x: 680 }}
        columns={[
          {
            title: "成员",
            key: "name",
            render: (_, row) => <div className="me-manage-member">
              <span className="me-group-avatar" aria-hidden="true">{row.name.slice(0, 1)}</span>
              <span className="me-manage-member-main">
                <strong>{row.name}</strong>
                <Text type="secondary" className="me-hint">{row.userId}{row.card && row.nickname && row.card !== row.nickname ? ` · ${row.nickname}` : ""}</Text>
              </span>
            </div>,
          },
          {
            title: "身份",
            key: "role",
            width: 110,
            render: (_, row) => <Space size={4} wrap>
              <Tag className={`me-role-tag is-${row.role}`}>{groupRoleLabel(row.role)}</Tag>
              {row.title && <Tag>{row.title}</Tag>}
            </Space>,
          },
          {
            title: "状态",
            key: "muted",
            width: 150,
            render: (_, row) => (row.mutedUntil ? <Tag color="warning" icon={<LockOutlined />}>禁言到 {row.mutedUntil}</Tag> : <Text type="secondary">正常</Text>),
          },
          {
            title: "操作",
            key: "actions",
            width: 280,
            render: (_, row) => (row.role === "owner" ? <Text type="secondary" className="me-hint">群主不能被处置</Text> : <Space size={4} wrap>
              {row.mutedUntil
                ? <GuardedButton runner={runner} action="unmute" size="small" icon={<UnlockOutlined />} onClick={() => void runner.run({ action: "unmute", member: row.userId })}>解禁</GuardedButton>
                : <MuteButton runner={runner} member={row} maxMinutes={max} />}
              <TextActionButton runner={runner} action="card" member={row} label="名片" placeholder="新的群名片，留空清空" maxLength={60} />
              {status.botRole === "owner" && <TextActionButton runner={runner} action="title" member={row} label="头衔" placeholder="专属头衔，最多 6 个字" maxLength={18} />}
              <GuardedButton
                runner={runner}
                action="kick"
                size="small"
                danger
                onClick={() => void runner.run({ action: "kick", member: row.userId }, `kick-${row.userId}`)}
              >
                移出
              </GuardedButton>
            </Space>),
          },
        ]}
      />}
  </div>;
}

// ── 公告与待办 ────────────────────────────────────────────────────────

function NoticesView({ groupId, runner, status, onChanged }: { groupId: string; runner: Runner; status: ManageStatus; onChanged: () => void }): React.JSX.Element {
  const [content, setContent] = useState("");
  const [pinned, setPinned] = useState(false);
  const [confirmRequired, setConfirmRequired] = useState(false);
  const [todo, setTodo] = useState("");
  const [say, setSay] = useState("");
  const [noticeKey, setNoticeKey] = useState(0);
  const notices = useApiQuery({ queryKey: ["me", "manage", groupId, "notices", noticeKey], fetcher: () => meApi.manageNotices(groupId) });
  const refreshNotices = () => setNoticeKey((value) => value + 1);
  return <div className="me-manage-grid">
    <section className="me-section me-manage-card">
      <div className="me-section-head"><strong><BellOutlined /> 发群公告</strong><Text type="secondary" className="me-hint">{content.length} / 600</Text></div>
      <Input.TextArea value={content} onChange={(event) => setContent(event.target.value)} maxLength={600} autoSize={{ minRows: 4, maxRows: 10 }} placeholder="公告正文；和在 QQ 里发的公告一样，群员都能看到" />
      <div className="me-manage-row">
        <Checkbox checked={pinned} onChange={(event) => setPinned(event.target.checked)}>置顶</Checkbox>
        <Checkbox checked={confirmRequired} onChange={(event) => setConfirmRequired(event.target.checked)}>需要群员确认</Checkbox>
        <span className="me-manage-toolbar-spacer" />
        <GuardedButton
          runner={runner}
          action="announce"
          type="primary"
          icon={<BellOutlined />}
          disabled={!content.trim()}
          onClick={() => void runner.run({ action: "announce", content, pinned, confirmRequired }).then((done) => { if (done) { setContent(""); refreshNotices(); } })}
        >
          发布
        </GuardedButton>
      </div>
    </section>

    <section className="me-section me-manage-card">
      <div className="me-section-head"><strong><CheckSquareOutlined /> 设群待办</strong><Text type="secondary" className="me-hint">Bot 会先发这条消息，再把它设成群待办</Text></div>
      <Input.TextArea value={todo} onChange={(event) => setTodo(event.target.value)} maxLength={500} autoSize={{ minRows: 2, maxRows: 6 }} placeholder="例如：周五 20:00 前交活动报名表" />
      <div className="me-manage-row">
        <span className="me-manage-toolbar-spacer" />
        <GuardedButton runner={runner} action="todo" type="primary" icon={<CheckSquareOutlined />} disabled={!todo.trim()} onClick={() => void runner.run({ action: "todo", text: todo }).then((done) => { if (done) setTodo(""); })}>
          发出并设为待办
        </GuardedButton>
      </div>
      <div className="me-section-head me-manage-subhead"><strong><SendOutlined /> 以 Bot 身份说一句</strong></div>
      <Space.Compact block>
        <Input value={say} onChange={(event) => setSay(event.target.value)} maxLength={500} placeholder="直接以 Bot 的身份发到群里" onPressEnter={() => say.trim() && void runner.run({ action: "send", text: say }).then((done) => { if (done) setSay(""); })} />
        <GuardedButton runner={runner} action="send" icon={<SendOutlined />} disabled={!say.trim()} onClick={() => void runner.run({ action: "send", text: say }).then((done) => { if (done) setSay(""); })}>发送</GuardedButton>
      </Space.Compact>
    </section>

    <section className="me-section me-manage-card">
      <div className="me-section-head"><strong>现有群公告</strong><Button size="small" icon={<ReloadOutlined />} onClick={refreshNotices} loading={notices.refreshing}>刷新</Button></div>
      {notices.error && !notices.data
        ? <QueryErrorAlert error={notices.error} onRetry={notices.reload} />
        : !notices.data
          ? <Skeleton active paragraph={{ rows: 3 }} />
          : notices.data.length === 0
            ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有群公告" />
            : <ul className="me-manage-list">
              {notices.data.map((item) => <li key={item.noticeId} className="me-manage-list-item">
                <div className="me-manage-list-main">
                  <Paragraph ellipsis={{ rows: 3, expandable: true, symbol: "展开" }} className="me-manage-notice-text">{item.content || "（图片公告）"}</Paragraph>
                  <Text type="secondary" className="me-hint">{item.publishTime || "时间未知"} · 发布人 {item.senderId || "未知"}</Text>
                </div>
                <GuardedButton runner={runner} action="delete_notice" size="small" danger onClick={() => void runner.run({ action: "delete_notice", noticeId: item.noticeId }, `del-${item.noticeId}`).then((done) => { if (done) refreshNotices(); })}>删除</GuardedButton>
              </li>)}
            </ul>}
    </section>

    <section className="me-section me-manage-card">
      <div className="me-section-head"><strong>Bot 最近代发的消息</strong><Text type="secondary" className="me-hint">只有这些消息能撤回、设精华、完成或取消待办</Text></div>
      {status.sent.length === 0
        ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="最近 7 天没有代发过消息" />
        : <ul className="me-manage-list">
          {status.sent.map((item) => <li key={item.messageId} className="me-manage-list-item">
            <div className="me-manage-list-main">
              <span><Tag>{item.kind === "todo" ? "待办" : "消息"}</Tag>{item.preview}</span>
              <Text type="secondary" className="me-hint">{item.at}</Text>
            </div>
            <Space size={4} wrap>
              {item.kind === "todo" && <>
                <GuardedButton runner={runner} action="todo_done" size="small" onClick={() => void runner.run({ action: "todo_done", messageId: item.messageId }, `done-${item.messageId}`).then((ok) => ok && onChanged())}>完成</GuardedButton>
                <GuardedButton runner={runner} action="todo_cancel" size="small" onClick={() => void runner.run({ action: "todo_cancel", messageId: item.messageId }, `cancel-${item.messageId}`).then((ok) => ok && onChanged())}>取消待办</GuardedButton>
              </>}
              <GuardedButton runner={runner} action="essence" size="small" onClick={() => void runner.run({ action: "essence", messageId: item.messageId }, `ess-${item.messageId}`)}>设精华</GuardedButton>
              <GuardedButton runner={runner} action="recall" size="small" danger onClick={() => void runner.run({ action: "recall", messageId: item.messageId }, `recall-${item.messageId}`).then((ok) => ok && onChanged())}>撤回</GuardedButton>
            </Space>
          </li>)}
        </ul>}
    </section>
  </div>;
}

// ── 全群 ──────────────────────────────────────────────────────────────

function GroupView({ runner, groupName }: { runner: Runner; groupName: string }): React.JSX.Element {
  const [name, setName] = useState("");
  return <div className="me-manage-grid">
    <section className="me-section me-manage-card">
      <div className="me-section-head"><strong><LockOutlined /> 全员禁言</strong></div>
      <Text type="secondary" className="me-hint">开启后除群主和管理员外所有人都不能发言。QQ 不返回当前状态，按需开或关。</Text>
      <div className="me-manage-row">
        <GuardedButton runner={runner} action="whole_mute" danger icon={<LockOutlined />} onClick={() => void runner.run({ action: "whole_mute", enable: true }, "whole-on")}>开启全员禁言</GuardedButton>
        <GuardedButton runner={runner} action="whole_mute" icon={<UnlockOutlined />} onClick={() => void runner.run({ action: "whole_mute", enable: false }, "whole-off")}>关闭全员禁言</GuardedButton>
      </div>
    </section>
    <section className="me-section me-manage-card">
      <div className="me-section-head"><strong>改群名</strong><Text type="secondary" className="me-hint">现在叫「{groupName}」</Text></div>
      <Space.Compact block>
        <Input value={name} onChange={(event) => setName(event.target.value)} maxLength={60} placeholder="新的群名" />
        <GuardedButton runner={runner} action="rename" danger disabled={!name.trim()} onClick={() => void runner.run({ action: "rename", text: name }).then((ok) => { if (ok) setName(""); })}>改名</GuardedButton>
      </Space.Compact>
    </section>
  </div>;
}

// ── 操作记录 ──────────────────────────────────────────────────────────

function AuditView({ groupId }: { groupId: string }): React.JSX.Element {
  const [result, setResult] = useState("");
  const [mine, setMine] = useState(false);
  const query = useApiQuery({
    queryKey: ["me", "manage", groupId, "audit", result, mine],
    fetcher: () => meApi.manageAudit(groupId, { result: result || undefined, mine }),
    invalidation: { resources: ["group_manage"], scope: { groupId } },
  });
  return <div className="me-manage-view">
    <div className="me-manage-toolbar">
      <Select
        value={result}
        onChange={setResult}
        style={{ width: 130 }}
        options={[{ value: "", label: "全部结果" }, { value: "success", label: "成功" }, { value: "failed", label: "失败" }, { value: "denied", label: "被拒" }]}
        aria-label="按结果筛选"
      />
      <Checkbox checked={mine} onChange={(event) => setMine(event.target.checked)}>只看我的</Checkbox>
      <span className="me-manage-toolbar-spacer" />
      <Button icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
    </div>
    {query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Table<ManageAuditRow>
        rowKey="id"
        size="middle"
        className="me-manage-table"
        loading={query.loading}
        dataSource={query.data ?? []}
        pagination={{ pageSize: 20, showSizeChanger: false, hideOnSinglePage: true }}
        scroll={{ x: 720 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有操作记录" /> }}
        columns={[
          { title: "时间", dataIndex: "at", width: 160, render: (value: string) => <Text className="me-mono">{value}</Text> },
          {
            title: "谁",
            key: "actor",
            width: 150,
            render: (_, row) => <span>{row.actorId}<Tag className="me-inline-tag">{manageSourceLabel(row.source)}</Tag></span>,
          },
          {
            title: "做了什么",
            key: "what",
            render: (_, row) => <div className="me-manage-list-main">
              <span>{row.label}{row.targetName ? ` · ${row.targetName}` : ""}</span>
              {(row.detail || row.reason) && <Text type="secondary" className="me-hint">{[row.detail, row.reason && `理由：${row.reason}`].filter(Boolean).join(" · ")}</Text>}
              {row.error && <Text type="danger" className="me-hint">{row.error}</Text>}
            </div>,
          },
          {
            title: "结果",
            dataIndex: "result",
            width: 90,
            render: (value: string) => {
              const meta = manageResultMeta(value);
              return <Tag color={meta.color}>{meta.label}</Tag>;
            },
          },
        ]}
      />}
  </div>;
}

// ── 策略 ──────────────────────────────────────────────────────────────

function PolicyView({ groupId, status, onSaved }: { groupId: string; status: ManageStatus; onSaved: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [saving, setSaving] = useState<string | null>(null);
  const policy = status.policy;
  const editable = status.isOwner;
  const save = async (key: string, changes: Partial<ManagePolicy>) => {
    setSaving(key);
    try {
      await meApi.managePolicy(groupId, changes);
      message.success("策略已保存");
      onSaved();
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setSaving(null);
    }
  };
  const toggleList = (field: "disabledActions" | "adminActions" | "confirmActions", key: string, on: boolean) => {
    const current = new Set(policy[field]);
    if (on) current.add(key);
    else current.delete(key);
    void save(`${field}-${key}`, { [field]: [...current] });
  };
  const switches: Array<{ key: keyof ManagePolicy; label: string; help: string }> = [
    { key: "enabled", label: "允许群管代办", help: "关掉后私聊和网页都不能让 Bot 代办本群的管理操作" },
    { key: "privateEnabled", label: "允许私聊代办", help: "群主/管理员私聊 Bot 说“把谁禁言”等，确认后执行" },
    { key: "webuiEnabled", label: "允许网页代办", help: "在本页直接点按钮执行" },
    { key: "allowRelay", label: "允许私聊转告到本群", help: "群友私聊 Bot 说“帮我在群里问一下/转告一句”，确认后 Bot 以“〔某某 让我转告〕”发到本群；群友引用回复会转回给他" },
    { key: "notifyGroup", label: "在群里说明是谁让 Bot 做的", help: "禁言、踢人、全员禁言、改群名、设头衔后，Bot 会在群里发一句“〔群管代办〕某某让我…”" },
  ];
  return <div className="me-manage-view">
    {!editable && <Alert type="info" showIcon message="只有群主能改这里的策略" description="你可以看到本群对管理员开放了哪些操作；需要调整请联系群主。" />}
    <div className="me-manage-grid">
      <section className="me-section me-manage-card me-manage-wide">
        <div className="me-section-head"><strong><SettingOutlined /> 总开关</strong>{policy.updatedAt && <Text type="secondary" className="me-hint">上次修改 {policy.updatedAt}</Text>}</div>
        <ul className="me-manage-list">
          {switches.map((item) => <li key={item.key} className="me-manage-list-item">
            <div className="me-manage-list-main"><strong>{item.label}</strong><Text type="secondary" className="me-hint">{item.help}</Text></div>
            <Switch checked={Boolean(policy[item.key])} disabled={!editable} loading={saving === item.key} onChange={(checked) => void save(item.key, { [item.key]: checked })} aria-label={item.label} />
          </li>)}
          <li className="me-manage-list-item">
            <div className="me-manage-list-main"><strong>一次最多禁言</strong><Text type="secondary" className="me-hint">上限 {manageMinutesLabel(policy.maxMuteCeiling)}</Text></div>
            <Select
              value={policy.maxMuteMinutes}
              disabled={!editable}
              loading={saving === "maxMuteMinutes"}
              style={{ width: 120 }}
              onChange={(value) => void save("maxMuteMinutes", { maxMuteMinutes: value })}
              options={[10, 60, 360, 1440, 4320, 10080, 43200].filter((value) => value <= policy.maxMuteCeiling).map((value) => ({ value, label: manageMinutesLabel(value) }))}
              aria-label="一次最多禁言"
            />
          </li>
        </ul>
      </section>
      <section className="me-section me-manage-card me-manage-wide">
        <div className="me-section-head"><strong><SafetyCertificateOutlined /> 每项操作</strong><Text type="secondary" className="me-hint">“私聊需确认”指私聊里要等他回一句同意才执行；网页总会弹窗确认</Text></div>
        <Table<ManageActionMeta>
          rowKey="key"
          size="small"
          pagination={false}
          className="me-manage-table"
          dataSource={status.actions}
          scroll={{ x: 560 }}
          columns={[
            {
              title: "操作",
              key: "label",
              render: (_, row) => <Space size={4} wrap>
                <span>{row.label}</span>
                {row.risk === "high" && <Tag color="error">不可撤回</Tag>}
                {row.botOwner && <Tag>需 Bot 是群主</Tag>}
              </Space>,
            },
            { title: "开启", key: "on", width: 70, render: (_, row) => <Switch size="small" checked={!row.disabled} disabled={!editable} loading={saving === `disabledActions-${row.key}`} onChange={(checked) => toggleList("disabledActions", row.key, !checked)} aria-label={`开启${row.label}`} /> },
            { title: "管理员可用", key: "admin", width: 100, render: (_, row) => <Switch size="small" checked={row.adminAllowed} disabled={!editable || row.disabled} loading={saving === `adminActions-${row.key}`} onChange={(checked) => toggleList("adminActions", row.key, checked)} aria-label={`管理员可用${row.label}`} /> },
            { title: "私聊需确认", key: "confirm", width: 100, render: (_, row) => <Switch size="small" checked={row.confirm} disabled={!editable || row.disabled} loading={saving === `confirmActions-${row.key}`} onChange={(checked) => toggleList("confirmActions", row.key, checked)} aria-label={`私聊需确认${row.label}`} /> },
          ]}
        />
      </section>
    </div>
  </div>;
}

// ── 入口 ──────────────────────────────────────────────────────────────

export function GroupManagePanel({ groupId, groupName }: { groupId: string; groupName: string }): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("manage.view") as View | null;
  const view: View = requested && VIEWS.includes(requested) ? requested : "members";
  const status = useApiQuery({
    queryKey: ["me", "manage", groupId],
    fetcher: () => meApi.manage(groupId),
    invalidation: { resources: ["group_manage"], scope: { groupId } },
  });
  const runner = useManageRunner(groupId, status.data, status.reload);
  const setView = (next: string | number) => {
    const params = new URLSearchParams(searchParams);
    params.set("manage.view", String(next));
    setSearchParams(params, { replace: true });
  };
  if (!status.data) return status.error ? <QueryErrorAlert error={status.error} onRetry={status.reload} /> : <Skeleton active paragraph={{ rows: 8 }} />;
  const data = status.data;
  return <div className="me-manage">
    <Text type="secondary" className="me-hint me-tab-hint">
      让 Bot 替你做群管理：每次执行前都会向 QQ 实时核对你和 Bot 都是群主或管理员；管理员不能处置群主和其他管理员。也可以直接私聊 Bot 说“把某某禁言 10 分钟”。
    </Text>
    {!data.available && <Alert className="me-banner" type="warning" showIcon message="现在不能代办" description={data.problem} action={<Button size="small" onClick={status.reload}>重试</Button>} />}
    <StatusStrip status={data} />
    <Segmented
      className="me-manage-tabs"
      value={view}
      onChange={setView}
      options={[
        { value: "members", label: <span><TeamOutlined /> 成员</span> },
        { value: "notices", label: <span><BellOutlined /> 公告与待办</span> },
        { value: "group", label: <span><LockOutlined /> 全群</span> },
        { value: "audit", label: <span><AuditOutlined /> 操作记录</span> },
        { value: "policy", label: <span><SettingOutlined /> 策略</span> },
      ]}
    />
    {view === "members" && <MembersView groupId={groupId} runner={runner} status={data} />}
    {view === "notices" && <NoticesView groupId={groupId} runner={runner} status={data} onChanged={status.reload} />}
    {view === "group" && <GroupView runner={runner} groupName={groupName} />}
    {view === "audit" && <AuditView groupId={groupId} />}
    {view === "policy" && <PolicyView groupId={groupId} status={data} onSaved={status.reload} />}
  </div>;
}
