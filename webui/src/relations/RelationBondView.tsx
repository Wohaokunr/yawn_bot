// Bot 视角：Bot 自己和每位成员的关系（熟悉度、阶段、好感、称呼、梗、边界）。
// 数据独立于成员关系图谱，走 /bonds 接口；点卡片打开详情抽屉，管理员可在抽屉里修改或重置。
import { LockOutlined } from "@ant-design/icons";
import { Alert, Button, Empty, Input, Segmented, Select, Spin, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { api } from "../api";
import { QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentBondItem, AgentBondSummary } from "../types";
import { BondDetailDrawer } from "./BondDetailDrawer";
import { Meter } from "./RelationCharts";
import { affinityWord, BOND_STAGE_ORDER, initiativeRatio, stageMeta } from "./bond-meta";
import { relativeTime } from "./relation-analysis";
import type { RelationsContext } from "./relations-context";

const { Text } = Typography;
const PAGE_SIZE = 48;

type BondSort = "familiarity" | "affinity" | "recent" | "days";

export function RelationBondView({ ctx }: { ctx: RelationsContext }): React.JSX.Element {
  const [stage, setStage] = useState("");
  const [sort, setSort] = useState<BondSort>("familiarity");
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState(1);
  const [openUserId, setOpenUserId] = useState<string | null>(null);
  const search = (keyword || ctx.filters.search).trim();

  const listQuery = useApiQuery({
    queryKey: ["agent-bonds", ctx.groupId, stage, sort, search, page],
    fetcher: (signal) => {
      const params = new URLSearchParams({ page: String(page), pageSize: String(PAGE_SIZE), sort, order: "desc", search });
      if (stage) params.set("stage", stage);
      return api<AgentBondItem[]>(`/agent/groups/${ctx.groupId}/bonds?${params.toString()}`, { signal })
        .then((response) => ({
          rows: response.data,
          total: response.meta.total ?? 0,
          tableReady: response.meta.tableReady !== false,
        }));
    },
    invalidation: { resources: ["agent_bond", "agent_member_data", "agent_group_data"], scope: { groupId: ctx.groupId } },
  });
  const summaryQuery = useApiQuery({
    queryKey: ["agent-bond-summary", ctx.groupId],
    fetcher: (signal) => api<AgentBondSummary>(`/agent/groups/${ctx.groupId}/bonds/summary`, { signal }).then((response) => response.data),
    invalidation: { resources: ["agent_bond", "agent_member_data", "agent_group_data"], scope: { groupId: ctx.groupId } },
  });

  const summary = summaryQuery.data;
  const counts = useMemo(
    () => new Map((summary?.stageCounts ?? []).map((item) => [item.stage, item.count])),
    [summary],
  );
  const rows = listQuery.data?.rows ?? [];
  const total = listQuery.data?.total ?? 0;
  const tableReady = (listQuery.data?.tableReady ?? true) && (summary?.tableReady ?? true);

  if (listQuery.error && !listQuery.data) return <QueryErrorAlert error={listQuery.error} onRetry={listQuery.reload} />;
  if (!tableReady) {
    return (
      <Alert
        type="warning"
        showIcon
        message="Bot 关系表还没有建好"
        description="这是新加的数据表，需要先在服务器上执行 nb orm upgrade；迁移前 Bot 照常聊天，只是不会沉淀和成员的关系。"
      />
    );
  }

  return (
    <div className="rel-bond-view">
      <div className="rel-bond-metrics" aria-label="Bot 关系概况">
        <div className="rel-bond-metric">
          <span className="rel-bond-metric-num">{summary?.total ?? "—"}</span>
          <span className="rel-small">有来往的成员</span>
        </div>
        <div className="rel-bond-metric">
          <span className="rel-bond-metric-num">{summary?.activeWeekCount ?? "—"}</span>
          <span className="rel-small">近 7 天聊过</span>
        </div>
        <div className="rel-bond-metric">
          <span className="rel-bond-metric-num">
            {summary?.avgFamiliarity === null || summary?.avgFamiliarity === undefined ? "—" : `${Math.round(summary.avgFamiliarity * 100)}%`}
          </span>
          <span className="rel-small">平均熟悉度</span>
        </div>
        <div className="rel-bond-metric">
          <span className="rel-bond-metric-num">{affinityWord(summary?.avgAffinity)}</span>
          <span className="rel-small">整体氛围</span>
        </div>
        <div className="rel-bond-stagebar" role="img" aria-label="关系阶段分布">
          {BOND_STAGE_ORDER.map((key) => {
            const count = counts.get(key) ?? 0;
            if (!count || !summary?.total) return null;
            const meta = stageMeta(key);
            return (
              <Tooltip key={key} title={`${meta.label} ${count} 人 · ${meta.hint}`}>
                <span style={{ flexGrow: count, background: meta.color }} />
              </Tooltip>
            );
          })}
        </div>
      </div>
      <div className="rel-subbar">
        <Input.Search
          allowClear
          className="rel-subbar-search"
          placeholder="找成员或关系标签"
          onChange={(event) => { setKeyword(event.target.value); setPage(1); }}
        />
        <Select
          className="rel-bond-stage-select"
          value={stage}
          onChange={(value) => { setStage(value); setPage(1); }}
          aria-label="按阶段筛选"
          options={[
            { value: "", label: "全部阶段" },
            ...BOND_STAGE_ORDER.map((key) => ({
              value: key,
              label: <span className="rel-select-type"><span className="rel-type-dot" style={{ background: stageMeta(key).color }} />{stageMeta(key).label}<span className="rel-legend-count">{counts.get(key) ?? 0}</span></span>,
            })),
          ]}
        />
        <Segmented
          size="small"
          value={sort}
          onChange={(value) => { setSort(value as BondSort); setPage(1); }}
          options={[
            { value: "familiarity", label: "最熟" },
            { value: "affinity", label: "最亲近" },
            { value: "recent", label: "最近聊过" },
            { value: "days", label: "来往最久" },
          ]}
        />
        <span className="rel-grow" />
        <Text type="secondary" className="rel-small">{total} 人</Text>
      </div>
      {listQuery.loading && !listQuery.data ? (
        <div className="rg-loading-wrap"><Spin /></div>
      ) : rows.length === 0 ? (
        <Empty description={search || stage ? "没有符合条件的成员" : "Bot 还没和成员有过来往；被 @、被回复或回复别人后，整理任务会沉淀关系"} />
      ) : (
        <div className="rel-member-grid">
          {rows.map((bond) => {
            const meta = stageMeta(bond.stage);
            const ratio = initiativeRatio(bond);
            const locked = (bond.lockedFields ?? []).length > 0;
            return (
              <button
                key={bond.id}
                type="button"
                className="rel-member-card rel-bond-card"
                style={{ "--rel-community": meta.color } as React.CSSProperties}
                onClick={() => setOpenUserId(bond.userId)}
                aria-label={`${bond.name}，${meta.label}`}
              >
                <div className="rel-member-card-head">
                  <span className="rel-avatar" aria-hidden="true" data-initial={bond.name.slice(0, 1)} />
                  <div className="rel-member-card-title">
                    <span className="rel-member-card-name">{bond.name}</span>
                    <span className="rel-mono rel-member-card-id">{bond.userId}</span>
                  </div>
                  <span className="rel-bond-stage" style={{ "--rel-type-color": meta.color } as React.CSSProperties}>{meta.label}</span>
                  {locked && <Tooltip title="有字段被管理员或本人锁定"><LockOutlined className="rel-bond-lock" /></Tooltip>}
                </div>
                <div className="rel-bond-label rel-ellipsis">{bond.label || <Text type="secondary">还没有关系标签</Text>}</div>
                <Meter value={bond.familiarity} color={meta.color} label={`熟悉度 ${Math.round(bond.familiarity * 100)}%`} />
                <div className="rel-member-card-stats">
                  <span><strong>{Math.round(bond.familiarity * 100)}%</strong> 熟悉</span>
                  {bond.affinity !== undefined && <span><strong>{affinityWord(bond.affinity)}</strong></span>}
                  <span><strong>{bond.activeDays}</strong> 天来往</span>
                  {ratio !== null && (
                    <Tooltip title="他主动找 Bot 的占比"><span><strong>{Math.round(ratio * 100)}%</strong> 主动</span></Tooltip>
                  )}
                </div>
                {(bond.memberCallsBot || bond.botCallsMember) && (
                  <div className="rel-member-card-types">
                    {bond.memberCallsBot && <span className="rel-type-chip">叫我「{bond.memberCallsBot}」</span>}
                    {bond.botCallsMember && <span className="rel-type-chip">我叫他「{bond.botCallsMember}」</span>}
                  </div>
                )}
                <div className="rel-member-card-foot">
                  <span className="rel-ellipsis">
                    {(bond.insideJokes ?? []).length ? `梗：${(bond.insideJokes ?? [])[0]}` : (bond.momentCount ? `${bond.momentCount} 段共同经历` : "")}
                  </span>
                  <span className="rel-member-card-time">{relativeTime(bond.lastInteractionAt)}</span>
                </div>
              </button>
            );
          })}
        </div>
      )}
      {total > PAGE_SIZE && (
        <div className="rel-center">
          <Button disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</Button>
          <Text type="secondary" className="rel-small">第 {page} / {Math.ceil(total / PAGE_SIZE)} 页</Text>
          <Button disabled={page * PAGE_SIZE >= total} onClick={() => setPage((value) => value + 1)}>下一页</Button>
        </div>
      )}
      <BondDetailDrawer
        groupId={ctx.groupId}
        userId={openUserId}
        readOnly={ctx.readOnly}
        onClose={() => setOpenUserId(null)}
        onChanged={() => { listQuery.reload(); summaryQuery.reload(); }}
        onOpenMember={(userId) => { setOpenUserId(null); ctx.openMember(userId); }}
      />
    </div>
  );
}
