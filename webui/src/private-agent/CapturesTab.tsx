import { DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Empty, Popconfirm, Skeleton, Space, Switch, Tag, Typography } from "antd";
import { PromptSnapshotView } from "../agent-prompts/PromptSnapshotView";
import { formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import { privateApi, type CacheStats } from "./private-api";
import { formatHitRate, pipelineLabel, PERSONA_SOURCE_LABELS, PREFIX_LAYER_LABELS, withPipelineTask } from "./private-meta";

const { Text, Paragraph } = Typography;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function UserCaptures({ userId, onOpenSection }: { userId: string; onOpenSection: (sectionId: string) => void }): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["private-captures", userId],
    fetcher: (signal) => privateApi.userCaptures(userId, signal),
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active paragraph={{ rows: 6 }} />;
  const prompts = query.data.prompts.map(withPipelineTask);
  const latest = prompts[0];
  return <section className="dbg-panel">
    <div className="pst-section-head">
      <Text strong>用户 {userId} 最近 {prompts.length} 次请求</Text>
      {latest?.persona && <Tag color={latest.persona.source.startsWith("user") ? "magenta" : "default"}>人设：{PERSONA_SOURCE_LABELS[latest.persona.source] ?? latest.persona.label}{latest.persona.name ? ` · ${latest.persona.name}` : ""}</Tag>}
      <span className="dbg-grow" />
      <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
    </div>
    <div className="pa-capture-tags">
      {prompts.map((item) => <Tag key={item.id} className="dbg-tag-soft">{pipelineLabel(item.pipeline)} #{item.round} · {formatTime(item.capturedAt)}</Tag>)}
    </div>
    <PromptSnapshotView snapshots={prompts} filenamePrefix={`private-${userId}`} emptyHint="这位用户最近还没有请求" onOpenSection={onOpenSection} />
  </section>;
}

function sumCounts(bucket: Record<string, number> | undefined): number {
  return Object.values(bucket ?? {}).reduce((total, value) => total + value, 0);
}

function topNames(bucket: Record<string, number> | undefined, limit = 4): string {
  return Object.entries(bucket ?? {}).slice(0, limit).map(([name, count]) => `${name}×${count}`).join("、");
}

function ToolDisclosureLine({ data }: { data: CacheStats }): React.JSX.Element | null {
  const info = data.disclosure;
  if (!info) return null;
  const saved = info.fullChars > 0 ? Math.round((1 - info.tableChars / info.fullChars) * 100) : 0;
  const tools = data.tools ?? {};
  const opened = topNames(tools.open);
  const used = topNames(tools.use);
  return <div className="pa-tool-disclosure" aria-label="工具披露">
    <Tag color={info.mode === "progressive" ? "green" : "default"}>{info.mode === "progressive" ? "工具渐进式披露" : "工具全量披露"}</Tag>
    <Text type="secondary" className="dbg-small">
      普通好友这一轮能用 {info.available} 个工具；工具表 {info.tableChars.toLocaleString()} 字
      {info.mode === "progressive" ? `（全量要 ${info.fullChars.toLocaleString()} 字，省 ${saved}%）` : ""}
      · 展开 {sumCounts(tools.open)} 次{opened ? `（${opened}）` : ""}
      · 族内调用 {sumCounts(tools.use)} 次{used ? `（${used}）` : ""}
      · 直达 {sumCounts(tools.direct)} 次 · 参数纠正 {sumCounts(tools.fixed)} 次
    </Text>
  </div>;
}

function CacheStatsPanel(): React.JSX.Element | null {
  const query = useApiQuery({
    queryKey: ["private-cache-stats"],
    fetcher: (signal) => privateApi.cacheStats(signal),
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return null;
  const data = query.data;
  const rows = Object.entries(data.pipelines);
  return <section className="dbg-panel" aria-label="前缀缓存命中">
    <div className="pst-section-head">
      <Text strong>前缀缓存命中</Text>
      <Tag color={data.hitRate !== null && data.hitRate >= 0.5 ? "green" : "default"}>总命中率 {formatHitRate(data.hitRate)}</Tag>
      <Text type="secondary" className="dbg-small">服务商报告的命中/未命中 token，按链路累计（进程内，重启清零）；锚定历史的用户 {data.anchoredUsers} 位。</Text>
      <span className="dbg-grow" />
      <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
    </div>
    <ToolDisclosureLine data={data} />
    {rows.length === 0
      ? <Empty description="还没有私聊请求" />
      : <ul className="pa-user-list">
        {rows.map(([name, row]) => {
          const last = row.recent[row.recent.length - 1];
          return <li key={name}>
            <Text strong>{pipelineLabel(name)}</Text>
            <Tag className="dbg-tag-soft">{formatHitRate(row.hitRate)}</Tag>
            <Text type="secondary" className="dbg-small">
              {row.calls} 次请求 · 命中 {row.cachedTokens} / 未命中 {row.missTokens} tokens
              {row.reported < row.calls ? ` · ${row.calls - row.reported} 次没报 usage` : ""}
              {last?.changed ? ` · 最近一次没命中是因为${PREFIX_LAYER_LABELS[last.changed] ?? last.changed}变了` : ""}
            </Text>
          </li>;
        })}
      </ul>}
  </section>;
}

const PART_BUCKETS = ["1", "2", "3", "4", "5+"] as const;
const PATTERN_LABELS: Record<string, string> = {
  same: "最近几次条数一模一样",
  always_split: "最近几次都分了条",
};

function percent(value: number | undefined): string {
  return `${Math.round((value ?? 0) * 100)}%`;
}

export function RhythmStatsPanel({ userId }: { userId?: string | null }): React.JSX.Element | null {
  const query = useApiQuery({
    queryKey: ["private-rhythm-stats", userId ?? ""],
    fetcher: (signal) => privateApi.rhythmStats(userId, signal),
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return null;
  const { stats, settings } = query.data;
  const recent = query.data.recent ?? [];
  return <section className="dbg-panel" aria-label="分条发送">
    <div className="pst-section-head">
      <Text strong>分条发送</Text>
      <Tag color={settings.interrupt ? "green" : "default"}>{settings.interrupt ? "可打断" : "不可打断"}</Tag>
      <Text type="secondary" className="dbg-small">实际发了几条的分布（进程内，重启清零）；分条上限 {settings.maxParts} 条，连发/蹦字另算。</Text>
      <span className="dbg-grow" />
      <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
    </div>
    {stats.deliveries === 0
      ? <Empty description="还没有私聊回复" />
      : <>
        <div className="pa-capture-tags" aria-label="条数分布">
          {PART_BUCKETS.map((key) => <Tag key={key} className="dbg-tag-soft">{key} 条 · {stats.byParts[key] ?? 0} 次（{percent(stats.partsShare[key])}）</Tag>)}
        </div>
        <div className="pa-capture-tags" aria-label="节奏分布">
          {Object.entries(stats.byRhythm).map(([label, count]) => <Tag key={label}>{label} {count}</Tag>)}
          {stats.withMedia > 0 && <Tag>带表情包/图片/文件 {stats.withMedia}</Tag>}
        </div>
        <Text type="secondary" className="dbg-small">
          被打断 {stats.interrupted} 次，没发出去 {stats.skippedItems} 条（不进历史）· 提醒“别套模板” {stats.mixHints} 次
        </Text>
      </>}
    {userId && <div className="pa-capture-tags" aria-label="这位用户最近几次">
      <Text type="secondary" className="dbg-small">用户 {userId} 最近：</Text>
      {recent.length === 0
        ? <Text type="secondary" className="dbg-small">还没有记录</Text>
        : recent.map((shape) => <Tag key={shape.at} color={shape.interrupted ? "orange" : undefined}>{shape.parts} 条 · {shape.rhythm}{shape.interrupted ? " · 被打断" : ""}</Tag>)}
      {query.data.pattern && <Tag color="gold">{PATTERN_LABELS[query.data.pattern] ?? query.data.pattern}</Tag>}
    </div>}
  </section>;
}

export function CapturesTab({ userId, onPickUser, onOpenSection }: { userId: string; onPickUser: (userId: string | null) => void; onOpenSection: (sectionId: string) => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery({
    queryKey: ["private-captures-index"],
    fetcher: (signal) => privateApi.captures(signal),
  });
  const run = async (work: () => Promise<unknown>, done: string): Promise<void> => {
    try { await work(); message.success(done); query.reload(); } catch (reason) { message.error(errorText(reason)); }
  };
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active paragraph={{ rows: 4 }} />;
  const data = query.data;
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <section className="dbg-panel">
      <div className="pst-section-head">
        <Text strong>私聊最近请求</Text>
        <Text type="secondary" className="dbg-small">每次真实发给模型的完整请求，按用户分开保存（每人最近 {data.perUser} 次，最多 {data.maxUsers} 位，只在内存里，重启清空）。</Text>
        <span className="dbg-grow" />
        <Space size={4}><Switch size="small" checked={!data.paused} onChange={(on) => void run(() => privateApi.setCapturePaused(!on), on ? "已恢复抓取" : "已暂停抓取")} aria-label="抓取私聊请求" /><Text type="secondary" className="dbg-small">抓取</Text></Space>
        <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
        <Popconfirm title="清空全部私聊抓取？" onConfirm={() => run(() => privateApi.clearCaptures(), "已清空")}><Button size="small" danger icon={<DeleteOutlined />}>全部清空</Button></Popconfirm>
      </div>
      {!data.captureEnabled && <Alert type="info" showIcon message="完整提示词抓取已关闭（AGENT_TRACE_PROMPT_CAPTURE=off）" />}
      <Paragraph type="secondary" className="pa-block">链接与本机路径会脱敏，图片和文件只留类型和大小。点请求里的分段可以跳到「提示词」页编辑。</Paragraph>
      {data.users.length === 0
        ? <Empty description="还没有私聊请求。有人私聊 Bot 后这里就会出现。" />
        : <ul className="pa-user-list">
          {data.users.map((row) => <li key={row.userId} className={row.userId === userId ? "is-active" : ""}>
            <button type="button" className="pa-user-btn" onClick={() => onPickUser(row.userId)}>{row.userId}</button>
            <Text type="secondary" className="dbg-small">{row.count} 次 · 最近{pipelineLabel(row.lastPipeline)} {row.lastAt ? formatTime(row.lastAt) : ""}</Text>
            <span className="dbg-grow" />
            <Button size="small" type="text" danger onClick={() => void run(() => privateApi.clearCaptures(row.userId), "已清空")} aria-label={`清空 ${row.userId} 的抓取`} icon={<DeleteOutlined />} />
          </li>)}
        </ul>}
    </section>
    <CacheStatsPanel />
    <RhythmStatsPanel userId={userId || null} />
    {userId && <UserCaptures key={userId} userId={userId} onOpenSection={onOpenSection} />}
  </Space>;
}
