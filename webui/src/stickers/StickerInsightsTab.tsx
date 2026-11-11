// 统计：用量走势、最常用/从没用过、各群用量、情绪覆盖、模型写了但库里没有的名字。
import { CloseOutlined, LinkOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Empty, Popconfirm, Segmented, Select, Skeleton, Statistic, Tag, Tooltip } from "antd";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { stickerImageUrl, stickersApi } from "./stickers-api";
import { formatBytes, historySeries, sparkPoints, trendText } from "./stickers-meta";
import type { StickerInsights, StickerMiss, StickerOverview } from "./types";

const RANGES = [7, 30, 90] as const;

function Spark({ values, label }: { values: number[]; label: string }): React.JSX.Element {
  return (
    <svg className="stk-spark" viewBox="0 0 100 32" preserveAspectRatio="none" role="img" aria-label={label}>
      <polyline points={sparkPoints(values)} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Bars({ rows, label }: { rows: { key: string; label: string; value: number; hint?: string }[]; label: string }): React.JSX.Element {
  const top = Math.max(...rows.map((row) => row.value), 1);
  return (
    <ul className="stk-bars" aria-label={label}>
      {rows.map((row) => (
        <li key={row.key} title={row.hint}>
          <span className="stk-bars-label">{row.label}</span>
          <span className="stk-bars-track"><span className="stk-bars-fill" style={{ width: `${(row.value / top) * 100}%` }} /></span>
          <span className="stk-bars-value">{row.value}</span>
        </li>
      ))}
    </ul>
  );
}

function MissRow({ miss, overview, onDone }: { miss: StickerMiss; overview: StickerOverview; onDone: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [target, setTarget] = useState<string | undefined>(miss.suggestions[0]?.id);
  const [busy, setBusy] = useState(false);
  const run = async (action: "alias" | "dismiss") => {
    setBusy(true);
    try {
      await stickersApi.resolveMiss({ name: miss.name, action, stickerId: action === "alias" ? target : undefined });
      message.success(action === "alias" ? `以后模型写“${miss.name}”就发这张` : "已忽略");
      onDone();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };
  const suggested = new Set(miss.suggestions.map((item) => item.id));
  return (
    <li className="stk-miss">
      <div>
        <strong>{miss.name}</strong>
        <span className="sbx-muted"> 写了 {miss.count} 次{miss.last ? ` · 最近 ${miss.last}` : ""}{miss.groups.length ? ` · ${miss.groups.slice(0, 2).map((group) => overview.groupNames[group] ?? group).join("、")}` : ""}</span>
      </div>
      <div className="stk-actions">
        <Select
          showSearch
          size="small"
          style={{ minWidth: 170 }}
          value={target}
          placeholder="当成哪一张的别名"
          optionFilterProp="label"
          onChange={(value: string) => setTarget(value)}
          aria-label={`把 ${miss.name} 当成哪一张的别名`}
          options={[
            ...miss.suggestions.map((item) => ({ value: item.id, label: `${item.name}（可能是它）` })),
            ...overview.stickers.filter((item) => !suggested.has(item.id)).map((item) => ({ value: item.id, label: item.name })),
          ]}
        />
        <Button size="small" type="primary" icon={<LinkOutlined />} disabled={!target} loading={busy} onClick={() => void run("alias")}>加成别名</Button>
        <Tooltip title="忽略这一条"><Button size="small" type="text" icon={<CloseOutlined />} disabled={busy} onClick={() => void run("dismiss")} aria-label={`忽略 ${miss.name}`} /></Tooltip>
      </div>
    </li>
  );
}

export function InsightsTab({ overview, onChanged }: { overview: StickerOverview; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = Number(searchParams.get("stk.days"));
  const days = (RANGES as readonly number[]).includes(requested) ? requested : 30;
  const query = useApiQuery<StickerInsights>({
    queryKey: ["stickers-insights", days],
    fetcher: (signal) => stickersApi.insights(days, signal),
    invalidation: { resources: ["stickers"] },
  });
  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Skeleton active />;
  const done = () => {
    query.reload();
    onChanged();
  };
  const byId = new Map(overview.stickers.map((item) => [item.id, item]));
  const open = (id: string) => setSearchParams(updateSearchParams(searchParams, { "stk.tab": null, "stk.edit": id, "stk.f": "all" }), { replace: true });
  const sent = historySeries(data.history, "sent");
  const privateSent = historySeries(data.history, "private_sent");
  const total = (values: number[]) => values.reduce((sum, value) => sum + value, 0);
  return (
    <div className="stk-insights">
      <div className="web-toolbar">
        <Segmented value={days} onChange={(value) => setSearchParams(updateSearchParams(searchParams, { "stk.days": Number(value) === 30 ? null : String(value) }), { replace: true })} options={RANGES.map((value) => ({ value, label: `近 ${value} 天` }))} />
        <span className="sbx-muted">只统计发了哪张、发了几次，不记聊天内容；私聊只记总数，不记是谁。</span>
      </div>
      <div className="stk-stats">
        <Card size="small">
          <Statistic title={`近 ${days} 天群里发出`} value={total(sent)} suffix="张" />
          <Spark values={sent} label="群里每天发出的表情包" />
          <span className="sbx-muted">{trendText(data.trend.sent)}</span>
        </Card>
        <Card size="small">
          <Statistic title="私聊里发出" value={total(privateSent)} suffix="张" />
          <Spark values={privateSent} label="私聊每天发出的表情包" />
        </Card>
        <Card size="small">
          <Statistic title="被拦下" value={total(historySeries(data.history, "dropped"))} suffix="次" />
          <Spark values={historySeries(data.history, "dropped")} label="每天被拦下的次数" />
          <span className="sbx-muted">{trendText(data.trend.dropped)}</span>
        </Card>
        <Card size="small">
          <Statistic title="Bot 偷图" value={total(historySeries(data.history, "stolen"))} suffix="张" />
          <Spark values={historySeries(data.history, "stolen")} label="每天偷的图" />
          <span className="sbx-muted">{trendText(data.trend.stolen)}</span>
        </Card>
        <Card size="small">
          <Statistic title="从没发过" value={data.totals.neverUsed} suffix={`/ ${data.totals.usable} 张`} />
          <span className="sbx-muted">库一共 {formatBytes(data.totals.bytes)}，{data.totals.animated} 张动图</span>
        </Card>
        <Card size="small">
          <Tooltip title="用得最多的前 10% 的图占了多少次发送；越高说明老发那几张，可以把菜单策略换成“没怎么用过的优先”">
            <Statistic title="集中度" value={data.totals.concentration} suffix="%" />
          </Tooltip>
          <span className="sbx-muted">{data.totals.concentration >= 70 ? "老发那几张" : "用得比较均匀"}</span>
        </Card>
      </div>
      <div className="sbx-grid">
        <Card
          size="small"
          className="sbx-card-wide"
          title={`模型想发但库里没有的名字（${data.missCount}）`}
          extra={data.misses.length > 0 && (
            <Popconfirm title="清空这个列表？" onConfirm={() => void stickersApi.resolveMiss({ action: "clear" }).then(done).catch(() => message.error("清空失败"))}>
              <Button size="small" type="text">清空</Button>
            </Popconfirm>
          )}
        >
          {data.misses.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="模型写的名字都对得上" />
          ) : (
            <>
              <p className="sbx-muted">模型写了 [表情:名字]，但本群可用的库里没有这个名字，那张图就没发出去。把它加成某一张的别名，下次就能发了。发送时从不猜“差不多是哪张”，只认这里登记过的名字、别名和标签。</p>
              <ul className="stk-miss-list">{data.misses.map((miss) => <MissRow key={miss.name} miss={miss} overview={overview} onDone={done} />)}</ul>
            </>
          )}
        </Card>
        <Card size="small" title="用得最多">
          {data.top.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没发过表情包" /> : (
            <ol className="stk-rank">
              {data.top.map((row) => (
                <li key={row.id}>
                  <button type="button" className="stk-rank-item" onClick={() => open(row.id)} aria-label={`打开 ${row.name}`}>
                    {byId.get(row.id) && !byId.get(row.id)?.fileMissing ? <img src={stickerImageUrl(byId.get(row.id)!)} alt="" loading="lazy" /> : <span className="stk-rank-blank" />}
                    <span>{row.name}</span>
                  </button>
                  <span className="sbx-muted">{row.uses} 次{row.sentToday ? ` · 今天 ${row.sentToday}` : ""}</span>
                </li>
              ))}
            </ol>
          )}
        </Card>
        <Card size="small" title="入库后一次没发过">
          {data.neverUsed.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="每张都发过" /> : (
            <>
              <div className="stk-chiprow">
                {data.neverUsed.map((row) => <Tag key={row.id} className="stk-chip" onClick={() => open(row.id)}>{row.name}</Tag>)}
              </div>
              <p className="sbx-muted">可能名字起得让模型不知道什么时候发。点开改个名字、补一句“什么时候发”，或者置顶几天。</p>
            </>
          )}
        </Card>
        <Card size="small" title="各群用量">
          {data.groups.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有数据" /> : (
            <Bars
              label="各群发出的表情包"
              rows={data.groups.map((row) => ({
                key: row.groupId ?? "private",
                label: row.private ? "私聊（合计）" : row.groupName ?? overview.groupNames[row.groupId ?? ""] ?? String(row.groupId),
                value: row.uses,
                hint: `今天 ${row.today} 张${row.favorite ? `，最爱发“${row.favorite}”` : ""}`,
              }))}
            />
          )}
        </Card>
        <Card size="small" title="情绪覆盖">
          <Bars label="各种情绪有几张" rows={data.moods.filter((row) => row.count > 0).map((row) => ({ key: row.mood, label: row.mood, value: row.count }))} />
          {data.moodGaps.length > 0 && <Alert type="info" showIcon message={`这些情绪一张都没有：${data.moodGaps.join("、")}`} description="遇到这些场合 Bot 没图可发。可以上传几张，或者等它自己偷。" />}
          {data.moodUnknown > 0 && <p className="sbx-muted">{data.moodUnknown} 张还没让 AI 判断情绪（到“AI 标注与偷图”里批量重标）。</p>}
        </Card>
        <Card size="small" title="为什么被拦下（今天）">
          {data.drops.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="今天没有被拦下的" /> : <Bars label="被拦下的原因" rows={data.drops.map((row) => ({ key: row.reason, label: row.reasonText, value: row.count }))} />}
        </Card>
        <Card size="small" title="最常用的标签">
          {data.tags.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有标签" /> : (
            <div className="stk-chiprow">
              {data.tags.map((row) => <Tooltip key={row.tag} title={`${row.count} 张，一共发过 ${row.uses} 次`}><Tag>{row.tag} · {row.uses}</Tag></Tooltip>)}
            </div>
          )}
        </Card>
        <Card size="small" title="每周入库">
          <Bars label="每周入库的张数" rows={data.growth.map((row) => ({ key: row.week, label: `${row.week} 那周`, value: row.total, hint: data.sources.map((source) => `${source.label} ${Number(row[source.source] ?? 0)}`).join("，") }))} />
          <div className="stk-chiprow">{data.sources.filter((row) => row.count > 0).map((row) => <Tag key={row.source}>{row.label} {row.count}</Tag>)}</div>
        </Card>
        <Card size="small" title="刚刚发出去的（本次启动以来）">
          {data.recent.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="启动以来还没发过" /> : (
            <ul className="stk-mini-list">
              {data.recent.slice(0, 12).map((row, index) => (
                <li key={`${row.at}-${index}`}><span className="sbx-muted">{row.at?.slice(5, 16)}</span> {row.name} <span className="sbx-muted">→ {row.groupId ? overview.groupNames[row.groupId] ?? row.groupId : "私聊"}</span></li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
