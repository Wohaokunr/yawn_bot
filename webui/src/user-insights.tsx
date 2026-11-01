import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  InputNumber,
  Input,
  Popconfirm,
  Progress,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import { formatTime, QueryErrorAlert, useApiQuery } from "./shared";

const { Text } = Typography;

// ── 类型（与 webui/users.py 的返回一致） ───────────────────────

export interface AffinityEvent {
  id: number;
  group_id: number | null;
  source: string;
  source_label: string;
  delta: number;
  balance_after: number;
  note: string;
  day: string | null;
  created_at: string | null;
}

export interface AffinityGroupStatus {
  groupId: string;
  groupName: string;
  groupAffinity: number;
  checkedToday: boolean;
  streakDays: number;
  totalDays: number;
  points: number;
  exp: number;
  lastCheckinDate: string | null;
}

export interface UserAffinity {
  userId: string;
  points: number;
  level: string;
  levelIndex: number;
  nextLevel: string | null;
  toNext: number | null;
  progress: number;
  today: Record<string, number>;
  available: boolean;
  levels: { floor: number; name: string }[];
  sources: Record<string, { label: string; dailyCap: number | null }>;
  breakdown30d: Record<string, number>;
  events: AffinityEvent[];
  groups: AffinityGroupStatus[];
}

export interface PrivateFact {
  key: string;
  label: string;
  content: string;
  source: string;
  sourceLabel: string;
  updatedAt: string | null;
  expiresAt: string | null;
  expired: boolean;
}

export interface UserPrivateAgent {
  userId: string;
  enabled: boolean;
  agentLoaded: boolean;
  autoReply: boolean;
  friendsOnly: boolean;
  dailyTurns: number;
  autoPaused: boolean;
  turnsToday: number;
  memoryAvailable: boolean;
  facts: PrivateFact[];
  removed?: number;
  /** 后台小助手与私聊虚拟电脑（旧后端没有这些字段）。 */
  tasksEnabled?: boolean;
  taskLimits?: { perUser: number; daily: number; timeoutSeconds: number };
  tasks?: PrivateHelperTask[];
  tasksToday?: number;
  computer?: { workspace: string; runsToday: number; dailyLimit: number; inboxBytes: number } | null;
  cancelled?: boolean;
  /** 私聊人设摘要与本人提示词覆盖数（旧后端没有）。 */
  persona?: UserPrivatePersonaBrief | null;
}

export interface UserPrivatePersonaBrief {
  mode: "follow" | "custom" | "library";
  source: string;
  sourceLabel: string;
  personal: boolean;
  name: string | null;
  summary: string;
  templateName: string | null;
  locked: boolean;
  updatedAt: string | null;
  promptOverrides: number;
}

export type PrivateTaskState = "queued" | "running" | "blocked" | "done" | "failed" | "cancelled";

export interface PrivateHelperTask {
  id: string;
  title: string;
  goal?: string;
  state: PrivateTaskState;
  status: string;
  toolsets: string[];
  createdAt?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  progress: Array<{ at?: string; note: string; step?: number | null; total?: number | null }>;
  summary?: string | null;
  error?: string | null;
  question?: string | null;
  files: string[];
  reported: boolean;
  held: boolean;
}

export const TASK_STATE_COLORS: Record<PrivateTaskState, string> = {
  queued: "default",
  running: "processing",
  blocked: "gold",
  done: "green",
  failed: "red",
  cancelled: "default",
};

const TOOLSET_LABELS: Record<string, string> = { web: "联网", computer: "虚拟电脑", toolbox: "小工具箱" };

/** 任务还能不能取消（排队、进行中、等回答）。 */
export function taskCancellable(task: Pick<PrivateHelperTask, "state">): boolean {
  return task.state === "queued" || task.state === "running" || task.state === "blocked";
}

/** 一行任务的“结果/进度”文字。 */
export function taskOutcomeText(task: PrivateHelperTask): string {
  if (task.state === "done") return task.summary || "已完成";
  if (task.state === "failed") return task.error || "没做成";
  if (task.state === "blocked") return `等回答：${task.question ?? ""}`;
  const last = task.progress[task.progress.length - 1];
  if (!last) return task.state === "queued" ? "排队中" : "刚开始";
  const counter = last.step && last.total ? ` ${last.step}/${last.total}` : last.step ? ` 第${last.step}步` : "";
  return `${counter.trim() ? `${counter.trim()}：` : ""}${last.note}`;
}

// ── 纯函数（便于测试） ─────────────────────────────────────────

export function formatDelta(delta: number): string {
  return delta > 0 ? `+${delta}` : String(delta);
}

export function sourceRows(data: Pick<UserAffinity, "sources" | "today" | "breakdown30d">): {
  key: string;
  label: string;
  today: number;
  cap: number | null;
  last30: number;
}[] {
  return Object.entries(data.sources).map(([key, rule]) => ({
    key,
    label: rule.label,
    today: data.today[key] ?? 0,
    cap: rule.dailyCap,
    last30: data.breakdown30d[key] ?? 0,
  }));
}

export type PrivateAgentState = "off" | "not_loaded" | "paused" | "manual_only" | "auto";

// 私聊 Agent 对这位用户实际处于什么状态，按优先级判断。
export function privateAgentState(data: Pick<UserPrivateAgent, "enabled" | "agentLoaded" | "autoReply" | "autoPaused">): PrivateAgentState {
  if (!data.enabled) return "off";
  if (!data.agentLoaded) return "not_loaded";
  if (!data.autoReply) return "manual_only";
  if (data.autoPaused) return "paused";
  return "auto";
}

export const PRIVATE_AGENT_STATE_META: Record<PrivateAgentState, { label: string; color: string; hint: string }> = {
  off: { label: "已关闭", color: "default", hint: "PRIVATE_AGENT_ENABLED 关着，私聊只有纯对话" },
  not_loaded: { label: "未加载", color: "warning", hint: "Agent 子插件没有加载，私聊退回纯对话" },
  manual_only: { label: "需 /对话", color: "processing", hint: "自动接管关着，用户发 /对话 后才由 Agent 回复" },
  paused: { label: "已暂停自动回复", color: "warning", hint: "用户发过 /退出，发 /对话 或在这里恢复" },
  auto: { label: "自动回复", color: "success", hint: "好友私聊直接由 Agent 回复" },
};

export function turnsText(turnsToday: number, dailyTurns: number): string {
  return dailyTurns > 0 ? `${turnsToday} / ${dailyTurns}` : `${turnsToday}（不限）`;
}

// ── 好感与签到 ────────────────────────────────────────────────

export function AffinityPanel({ userId }: { userId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [delta, setDelta] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const query = useApiQuery<UserAffinity>({
    queryKey: ["user-affinity", userId],
    fetcher: (signal) => api<UserAffinity>(`/users/${userId}/affinity`, { signal }).then((r) => r.data),
    invalidation: { resources: ["user_affinity"] },
  });

  const adjust = useCallback(async () => {
    if (!delta) return;
    setSaving(true);
    try {
      await api<UserAffinity>(`/users/${userId}/affinity/adjust`, {
        method: "POST",
        body: JSON.stringify({ delta, note }),
      });
      setDelta(null);
      setNote("");
      message.success("好感已调整");
      query.reload();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "调整失败");
    } finally {
      setSaving(false);
    }
  }, [delta, note, userId, message, query]);

  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  const data = query.data;
  if (!data) return <Spin />;

  return <Space direction="vertical" size="middle" style={{ width: "100%" }}>
    {!data.available && (
      <Alert type="warning" showIcon message="好感流水表还没迁移" description="当前只显示余额，流水与手动调整需要先执行数据库迁移。" />
    )}
    <Card size="small" title="全局好感">
      <Space direction="vertical" style={{ width: "100%" }}>
        <Space wrap>
          <Tag color="magenta">{data.level}</Tag>
          <Text strong>{data.points}</Text>
          <Text type="secondary">
            {data.nextLevel ? `距「${data.nextLevel}」还差 ${data.toNext ?? 0}` : "已是最高等级"}
          </Text>
        </Space>
        <Progress percent={Math.round(data.progress * 100)} size="small" showInfo={false} />
      </Space>
    </Card>
    <Card size="small" title="来源">
      <Table
        size="small"
        rowKey="key"
        pagination={false}
        dataSource={sourceRows(data)}
        columns={[
          { title: "来源", dataIndex: "label" },
          {
            title: "今日",
            render: (_, row) => row.cap ? `${row.today} / ${row.cap}` : row.today,
          },
          { title: "近 30 天", dataIndex: "last30", render: (value: number) => formatDelta(value) },
        ]}
      />
    </Card>
    <Card size="small" title="各群签到">
      {data.groups.length === 0
        ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没在任何群签到" />
        : (
          <Table
            size="small"
            rowKey="groupId"
            pagination={false}
            dataSource={data.groups}
            columns={[
              { title: "群", render: (_, row) => row.groupName || row.groupId },
              { title: "今日", render: (_, row) => row.checkedToday ? <Tag color="success">已签</Tag> : <Tag>未签</Tag> },
              { title: "连签", dataIndex: "streakDays" },
              { title: "累计", dataIndex: "totalDays" },
              { title: "积分", dataIndex: "points" },
              { title: "群好感", dataIndex: "groupAffinity" },
            ]}
          />
        )}
    </Card>
    <Card size="small" title="手动调整">
      <Form layout="inline" onFinish={() => void adjust()}>
        <Form.Item label="增减">
          <InputNumber min={-500} max={500} value={delta} onChange={(value) => setDelta(value)} placeholder="如 10 或 -5" />
        </Form.Item>
        <Form.Item label="原因">
          <Input maxLength={100} value={note} onChange={(event) => setNote(event.target.value)} placeholder="选填" />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit" loading={saving} disabled={!delta || !data.available}>调整</Button>
        </Form.Item>
      </Form>
    </Card>
    <Card size="small" title="最近流水">
      <Table
        size="small"
        rowKey="id"
        pagination={{ pageSize: 10, showSizeChanger: false }}
        dataSource={data.events}
        locale={{ emptyText: "暂无流水" }}
        columns={[
          { title: "时间", dataIndex: "created_at", render: formatTime },
          { title: "来源", dataIndex: "source_label" },
          {
            title: "变化",
            dataIndex: "delta",
            render: (value: number) => <Text type={value < 0 ? "danger" : "success"}>{formatDelta(value)}</Text>,
          },
          { title: "余额", dataIndex: "balance_after" },
          { title: "备注", dataIndex: "note" },
        ]}
      />
    </Card>
  </Space>;
}

// ── 私聊 Agent ────────────────────────────────────────────────

export function PrivateAgentPanel({ userId }: { userId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [busy, setBusy] = useState(false);
  const query = useApiQuery<UserPrivateAgent>({
    queryKey: ["user-private-agent", userId],
    fetcher: (signal) => api<UserPrivateAgent>(`/users/${userId}/private-agent`, { signal }).then((r) => r.data),
    invalidation: { resources: ["user_private_agent"] },
  });

  const run = useCallback(async (path: string, init: RequestInit, done: string) => {
    setBusy(true);
    try {
      await api<UserPrivateAgent>(path, init);
      message.success(done);
      query.reload();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }, [message, query]);

  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  const data = query.data;
  if (!data) return <Spin />;
  const state = privateAgentState(data);
  const meta = PRIVATE_AGENT_STATE_META[state];
  const base = `/users/${userId}/private-agent`;

  return <Space direction="vertical" size="middle" style={{ width: "100%" }}>
    <Card size="small" title="状态">
      <Descriptions size="small" column={1}>
        <Descriptions.Item label="当前">
          <Space wrap>
            <Tag color={meta.color}>{meta.label}</Tag>
            <Text type="secondary">{meta.hint}</Text>
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="今日轮数">{turnsText(data.turnsToday, data.dailyTurns)}</Descriptions.Item>
        <Descriptions.Item label="自动回复这位用户">
          <Switch
            checked={!data.autoPaused}
            loading={busy}
            disabled={!data.enabled || !data.autoReply}
            aria-label="自动回复这位用户"
            onChange={(checked) => void run(
              base,
              { method: "PATCH", body: JSON.stringify({ autoPaused: !checked }) },
              checked ? "已恢复自动回复" : "已暂停自动回复",
            )}
          />
        </Descriptions.Item>
      </Descriptions>
    </Card>
    {data.persona && <Card
      size="small"
      title="私聊人设与提示词"
      extra={<Link to={`/private-agent?pa.tab=persona&pa.user=${encodeURIComponent(userId)}`}>去「私聊 Agent」页管理</Link>}
    >
      <Descriptions size="small" column={1}>
        <Descriptions.Item label="人设">
          <Space wrap>
            <Tag color={data.persona.personal ? "magenta" : "default"}>{data.persona.sourceLabel}</Tag>
            {data.persona.locked && <Tag color="gold">已锁定</Tag>}
            <Text type="secondary">{data.persona.summary}</Text>
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="本人提示词覆盖">
          {data.persona.promptOverrides > 0
            ? <Link to={`/private-agent?pa.tab=prompts&pa.user=${encodeURIComponent(userId)}`}>{data.persona.promptOverrides} 处</Link>
            : <Text type="secondary">没有，跟随全局</Text>}
        </Descriptions.Item>
      </Descriptions>
    </Card>}
    {data.tasks && <Card
      size="small"
      title="后台小助手"
      extra={data.taskLimits && <Text type="secondary">
        今日 {data.tasksToday ?? 0} / {data.taskLimits.daily} 个 · 同时 {data.taskLimits.perUser} 个 · 每个 {Math.round(data.taskLimits.timeoutSeconds / 60)} 分钟
      </Text>}
    >
      {data.tasksEnabled === false && <Alert type="info" showIcon message="后台小助手已关闭（PRIVATE_AGENT_TASKS_ENABLED）" style={{ marginBottom: 8 }} />}
      <Table
        size="small"
        rowKey="id"
        pagination={false}
        dataSource={data.tasks}
        locale={{ emptyText: "还没有派过任务" }}
        expandable={{
          rowExpandable: (row) => row.progress.length > 0 || row.files.length > 0,
          expandedRowRender: (row) => <Space direction="vertical" size={2}>
            {row.goal && <Text type="secondary">目标：{row.goal}</Text>}
            {row.progress.map((item, index) => <Text key={index}>
              {item.step ? `${item.step}${item.total ? `/${item.total}` : ""} ` : "· "}{item.note}
              {item.at && <Text type="secondary"> （{item.at}）</Text>}
            </Text>)}
            {row.files.map((ref) => <Text key={ref} code>{ref}</Text>)}
          </Space>,
        }}
        columns={[
          { title: "编号", dataIndex: "id", width: 64 },
          { title: "任务", dataIndex: "title" },
          {
            title: "状态",
            width: 120,
            render: (_, row) => <Space size={4} wrap>
              <Tag color={TASK_STATE_COLORS[row.state]}>{row.status}</Tag>
              {row.held && <Tag>免打扰中待汇报</Tag>}
            </Space>,
          },
          { title: "工具", dataIndex: "toolsets", render: (value: string[]) => value.map((key) => TOOLSET_LABELS[key] ?? key).join("、") },
          { title: "进度 / 结果", render: (_, row) => taskOutcomeText(row) },
          { title: "派出", dataIndex: "createdAt", width: 150 },
          {
            title: "",
            width: 64,
            render: (_, row) => taskCancellable(row) && (
              <Popconfirm
                title={`取消「${row.title}」？`}
                description="小助手会立刻停下，不会再汇报。"
                onConfirm={() => run(`${base}/tasks/${encodeURIComponent(row.id)}`, { method: "DELETE" }, "已取消")}
              >
                <Button type="link" danger size="small">取消</Button>
              </Popconfirm>
            ),
          },
        ]}
      />
      {data.computer && <Text type="secondary" style={{ display: "block", marginTop: 8 }}>
        虚拟电脑（工作区 {data.computer.workspace}）：今日运行 {data.computer.runsToday} / {data.computer.dailyLimit} 次，
        附件入箱 {(data.computer.inboxBytes / 1024 / 1024).toFixed(1)} MB
      </Text>}
    </Card>}
    <Card
      size="small"
      title="私聊记忆"
      extra={data.facts.length > 0 && (
        <Popconfirm
          title="清空这位用户的全部私聊记忆？"
          description="只删私聊里记的，群里的记忆不受影响。"
          onConfirm={() => run(`${base}/memories`, { method: "DELETE" }, "私聊记忆已清空")}
        >
          <Button danger size="small" loading={busy}>全部清空</Button>
        </Popconfirm>
      )}
    >
      {!data.memoryAvailable && <Alert type="warning" showIcon message="记忆表不可用，暂时无法读取" />}
      <Table
        size="small"
        rowKey="key"
        pagination={false}
        dataSource={data.facts}
        locale={{ emptyText: "还没有私聊记忆" }}
        columns={[
          { title: "类别", dataIndex: "label" },
          {
            title: "内容",
            render: (_, row) => <>
              {row.content}
              {row.expired && <Tag style={{ marginLeft: 8 }}>已过期</Tag>}
            </>,
          },
          { title: "来源", dataIndex: "sourceLabel" },
          { title: "更新", dataIndex: "updatedAt", render: formatTime },
          {
            title: "",
            render: (_, row) => (
              <Popconfirm
                title={`删除「${row.label}」？`}
                onConfirm={() => run(`${base}/memories?key=${encodeURIComponent(row.key)}`, { method: "DELETE" }, "已删除")}
              >
                <Button type="link" danger size="small">删除</Button>
              </Popconfirm>
            ),
          },
        ]}
      />
    </Card>
  </Space>;
}
