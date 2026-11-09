// 管理台「好感度」页：好感玩法的全站策略（每级权益、礼物、商店、任务、衰减、问候）与统计。
// 标签在 URL aff.tab；策略整份编辑、整份保存（后端会补齐/校验），离开前有未保存提示。
import { DashboardOutlined, GiftOutlined, HeartOutlined, ReloadOutlined, SaveOutlined, SettingOutlined, UndoOutlined } from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  Popconfirm,
  Select,
  Skeleton,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHeader, QueryErrorAlert, SaveStatus, useApiQuery, useUnsavedChanges } from "../shared";
import { affinityAdminApi } from "./affinity-api";
import { ITEM_NAMES, itemsText, LEVEL_NUMBER_FIELDS, policyEqual, toggleUnlock, validatePolicy } from "./affinity-meta";
import type { AffinityPolicy, AffinityStats, GiftPolicy, LevelPerksPolicy, PolicyMeta, ShopPolicy } from "./types";
import "./affinity.css";

const { Text } = Typography;
type AffTab = "overview" | "levels" | "economy" | "rules";
const TABS: AffTab[] = ["overview", "levels", "economy", "rules"];

function pickTab(value: string | null): AffTab {
  return TABS.includes(value as AffTab) ? (value as AffTab) : "overview";
}

export function StatsView({ stats, onDecay, decaying }: { stats: AffinityStats; onDecay: () => void; decaying: boolean }): React.JSX.Element {
  const max = Math.max(1, ...stats.distribution.map((row) => row.count));
  return <div>
    {!stats.available && <Alert type="warning" showIcon message="好感流水表还没迁移" description="执行 nb orm upgrade 之后才有来源统计和衰减。" style={{ marginBottom: 12 }} />}
    <div className="aff-admin-stats">
      <Card size="small"><Statistic title="有好感的人" value={stats.withAffinity} suffix={`/ ${stats.users}`} /></Card>
      <Card size="small"><Statistic title="今天完成的任务" value={stats.questsDoneToday} /></Card>
      <Card size="small"><Statistic title="设了专属称呼" value={stats.nicknames} /></Card>
      <Card size="small"><Statistic title="护盾生效中" value={stats.activeShields} /></Card>
      <Card size="small"><Statistic title="今日心仪礼物" value={stats.favoriteToday ?? "—"} /></Card>
    </div>
    <div className="aff-grid">
      <Card size="small" title="等级分布">
        <ul className="aff-dist">
          {stats.distribution.map((row) => <li key={row.level}><span>{row.level}</span><span className="aff-dist-track"><span className="aff-dist-fill" style={{ width: `${Math.round((row.count / max) * 100)}%` }} /></span><strong>{row.count}</strong></li>)}
        </ul>
      </Card>
      <Card size="small" title="近 30 天好感从哪来">
        {stats.sources30d.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有记录" /> : (
          <ul className="aff-dist">
            {stats.sources30d.map((row) => <li key={row.source}><span>{row.label}</span><span className="aff-dist-track"><span className="aff-dist-fill" style={{ width: `${Math.round((Math.abs(row.total) / Math.max(1, Math.abs(stats.sources30d[0].total))) * 100)}%` }} /></span><strong>{row.total > 0 ? "+" : ""}{row.total}</strong></li>)}
          </ul>
        )}
      </Card>
      <Card size="small" title="和 Bot 最熟的人">
        <Table size="small" rowKey="userId" pagination={false} dataSource={stats.top} locale={{ emptyText: "还没有人" }}
          columns={[{ title: "QQ", dataIndex: "userId" }, { title: "昵称", dataIndex: "nickname", render: (v: string | null) => v ?? "—" }, { title: "等级", dataIndex: "level", render: (v: string) => <Tag>{v}</Tag> }, { title: "好感", dataIndex: "points", width: 72 }]} />
      </Card>
      <Card size="small" title="送礼与道具" extra={<Popconfirm title="现在结算一次衰减？" description="平时每天 4:10 自动跑；只扣长期没有来往的人" onConfirm={onDecay}><Button size="small" loading={decaying}>立刻结算衰减</Button></Popconfirm>}>
        <Text type="secondary">礼物累计</Text>
        <div className="aff-tags">{stats.gifts.length ? stats.gifts.map((g) => <Tag key={g.key}>{g.name} ×{g.count}</Tag>) : <Text type="secondary">还没人送过</Text>}</div>
        <Text type="secondary">大家背包里的道具</Text>
        <div className="aff-tags">{stats.items.length ? stats.items.map((g) => <Tag key={g.key}>{g.name} ×{g.count}</Tag>) : <Text type="secondary">—</Text>}</div>
      </Card>
      {stats.daily.length > 0 && <Card size="small" title="近 30 天每天涨了多少" className="aff-span">
        <Table size="small" rowKey="day" pagination={{ pageSize: 10 }} dataSource={[...stats.daily].reverse()} columns={[{ title: "日期", dataIndex: "day" }, { title: "好感合计", dataIndex: "gained" }, { title: "人数", dataIndex: "users" }]} />
      </Card>}
    </div>
  </div>;
}

export function LevelEditor({ level, floor, meta, onChange }: { level: LevelPerksPolicy; floor: number; meta: PolicyMeta; onChange: (next: LevelPerksPolicy) => void }): React.JSX.Element {
  return <Card size="small" className="aff-level-card" title={<span><HeartOutlined /> {level.level} <Text type="secondary">（{floor}+）</Text></span>}>
    <label>相处提示（给模型）<Input maxLength={80} value={level.tone} onChange={(e) => onChange({ ...level, tone: e.target.value })} aria-label={`${level.level} 相处提示`} /></label>
    <label>这一级 Bot 能做到什么程度<Input maxLength={120} value={level.behavior} onChange={(e) => onChange({ ...level, behavior: e.target.value })} aria-label={`${level.level} 分寸`} /></label>
    <div className="aff-number-grid">
      {LEVEL_NUMBER_FIELDS.map((field) => <label key={field.key}>{field.label}（{field.suffix}）
        <InputNumber min={0} max={field.max} value={Number(level[field.key])} onChange={(value) => onChange({ ...level, [field.key]: Number(value) || 0 })} aria-label={`${level.level} ${field.label}`} />
      </label>)}
    </div>
    <div>
      <Text type="secondary">解锁</Text>
      <div className="aff-tags">
        {meta.unlocks.map((unlock) => <Checkbox key={unlock.key} checked={level.unlocks.includes(unlock.key)} onChange={(e) => onChange(toggleUnlock(level, unlock.key, e.target.checked))}>{unlock.label}</Checkbox>)}
      </div>
    </div>
    <div>
      <Text type="secondary">升到这一级送</Text>
      <div className="aff-number-grid">
        {meta.items.map((item) => <label key={item.key}>{ITEM_NAMES[item.key] ?? item.key}
          <InputNumber min={0} max={99} value={level.levelup_items[item.key] ?? 0} onChange={(value) => {
            const next = { ...level.levelup_items, [item.key]: Number(value) || 0 };
            if (!next[item.key]) delete next[item.key];
            onChange({ ...level, levelup_items: next });
          }} aria-label={`${level.level} 升级送 ${ITEM_NAMES[item.key] ?? item.key}`} />
        </label>)}
      </div>
      <Text type="secondary" className="me-hint">要勾选“升级礼包”才会送：{itemsText(level.levelup_items, ITEM_NAMES)}</Text>
    </div>
  </Card>;
}

function EconomyEditor({ policy, meta, onChange }: { policy: AffinityPolicy; meta: PolicyMeta; onChange: (next: AffinityPolicy) => void }): React.JSX.Element {
  const setGift = (index: number, patch: Partial<GiftPolicy>) => onChange({ ...policy, gifts: policy.gifts.map((g, i) => (i === index ? { ...g, ...patch } : g)) });
  const setItem = (index: number, patch: Partial<ShopPolicy>) => onChange({ ...policy, shop: policy.shop.map((g, i) => (i === index ? { ...g, ...patch } : g)) });
  const usedItems = new Set(policy.shop.map((row) => row.key));
  const missing = meta.items.filter((item) => !usedItems.has(item.key));
  return <div className="aff-grid">
    <Card size="small" title={<span><GiftOutlined /> 礼物</span>} className="aff-span" extra={
      <Button size="small" disabled={policy.gifts.length >= 20} onClick={() => onChange({ ...policy, gifts: [...policy.gifts, { key: `gift_${policy.gifts.length + 1}`, name: "新礼物", emoji: "🎁", price: 50, affinity: 2, enabled: true }] })}>加一种</Button>
    }>
      <div className="aff-form-row" style={{ marginBottom: 8 }}>
        <label>礼物每天最多加好感 <InputNumber min={0} max={500} value={policy.gift_affinity_daily_cap} onChange={(v) => onChange({ ...policy, gift_affinity_daily_cap: Number(v) || 0 })} aria-label="礼物好感日上限" /></label>
        <label>今日心仪倍数 <InputNumber min={1} max={5} value={policy.favorite_multiplier} onChange={(v) => onChange({ ...policy, favorite_multiplier: Number(v) || 1 })} aria-label="心仪倍数" /></label>
      </div>
      <Table size="small" rowKey={(_row, index) => String(index)} pagination={false} dataSource={policy.gifts} scroll={{ x: 620 }} columns={[
        { title: "开", width: 56, render: (_: unknown, row: GiftPolicy, i: number) => <Switch size="small" checked={row.enabled} onChange={(v) => setGift(i, { enabled: v })} aria-label="启用" /> },
        { title: "图标", width: 72, render: (_: unknown, row: GiftPolicy, i: number) => <Input maxLength={8} value={row.emoji} onChange={(e) => setGift(i, { emoji: e.target.value })} aria-label="图标" /> },
        { title: "名字", render: (_: unknown, row: GiftPolicy, i: number) => <Input maxLength={12} value={row.name} onChange={(e) => setGift(i, { name: e.target.value })} aria-label="礼物名" /> },
        { title: "标识", render: (_: unknown, row: GiftPolicy, i: number) => <Input maxLength={24} value={row.key} onChange={(e) => setGift(i, { key: e.target.value.toLowerCase() })} aria-label="礼物标识" /> },
        { title: "价格（积分）", width: 120, render: (_: unknown, row: GiftPolicy, i: number) => <InputNumber min={0} max={100000} value={row.price} onChange={(v) => setGift(i, { price: Number(v) || 0 })} aria-label="价格" /> },
        { title: "好感", width: 90, render: (_: unknown, row: GiftPolicy, i: number) => <InputNumber min={1} max={100} value={row.affinity} onChange={(v) => setGift(i, { affinity: Number(v) || 1 })} aria-label="好感" /> },
        { title: "", width: 64, render: (_: unknown, _row: GiftPolicy, i: number) => <Button size="small" type="link" danger onClick={() => onChange({ ...policy, gifts: policy.gifts.filter((_g, j) => j !== i) })}>删</Button> },
      ]} />
    </Card>
    <Card size="small" title="商店道具" className="aff-span" extra={missing.length > 0 && <Select size="small" placeholder="上架道具" style={{ minWidth: 140 }} value={null}
      options={missing.map((item) => ({ value: item.key, label: ITEM_NAMES[item.key] ?? item.key }))}
      onChange={(key: string) => onChange({ ...policy, shop: [...policy.shop, { key, name: ITEM_NAMES[key] ?? key, price: 50, description: meta.items.find((m) => m.key === key)?.description ?? "", min_level: "陌生", max_hold: 5, enabled: true }] })} />}>
      <Table size="small" rowKey="key" pagination={false} dataSource={policy.shop} scroll={{ x: 720 }} columns={[
        { title: "开", width: 56, render: (_: unknown, row: ShopPolicy, i: number) => <Switch size="small" checked={row.enabled} onChange={(v) => setItem(i, { enabled: v })} aria-label="上架" /> },
        { title: "名字", render: (_: unknown, row: ShopPolicy, i: number) => <Input maxLength={12} value={row.name} onChange={(e) => setItem(i, { name: e.target.value })} aria-label="道具名" /> },
        { title: "价格", width: 110, render: (_: unknown, row: ShopPolicy, i: number) => <InputNumber min={0} max={100000} value={row.price} onChange={(v) => setItem(i, { price: Number(v) || 0 })} aria-label="道具价格" /> },
        { title: "最低等级", width: 120, render: (_: unknown, row: ShopPolicy, i: number) => <Select size="small" value={row.min_level} style={{ width: 100 }} onChange={(v: string) => setItem(i, { min_level: v })} options={meta.levels.map((l) => ({ value: l.name, label: l.name }))} aria-label="最低等级" /> },
        { title: "最多持有", width: 100, render: (_: unknown, row: ShopPolicy, i: number) => <InputNumber min={1} max={99} value={row.max_hold} onChange={(v) => setItem(i, { max_hold: Number(v) || 1 })} aria-label="最多持有" /> },
        { title: "说明", render: (_: unknown, row: ShopPolicy, i: number) => <Input maxLength={80} value={row.description} onChange={(e) => setItem(i, { description: e.target.value })} aria-label="说明" /> },
        { title: "", width: 64, render: (_: unknown, _row: ShopPolicy, i: number) => <Button size="small" type="link" danger onClick={() => onChange({ ...policy, shop: policy.shop.filter((_g, j) => j !== i) })}>下架</Button> },
      ]} />
    </Card>
  </div>;
}

function RulesEditor({ policy, meta, onChange }: { policy: AffinityPolicy; meta: PolicyMeta; onChange: (next: AffinityPolicy) => void }): React.JSX.Element {
  return <div className="aff-grid">
    <Card size="small" title="每日任务">
      <div className="aff-form-row">
        <label><Switch checked={policy.quests.enabled} onChange={(v) => onChange({ ...policy, quests: { ...policy.quests, enabled: v } })} aria-label="每日任务" />开启</label>
        <label>每天几个 <InputNumber min={1} max={5} value={policy.quests.per_day} onChange={(v) => onChange({ ...policy, quests: { ...policy.quests, per_day: Number(v) || 1 } })} aria-label="每天任务数" /></label>
        <label>每个奖励好感 <InputNumber min={0} max={20} value={policy.quests.reward_affinity} onChange={(v) => onChange({ ...policy, quests: { ...policy.quests, reward_affinity: Number(v) || 0 } })} aria-label="任务奖励" /></label>
        <label>全做完送 <Select size="small" style={{ minWidth: 130 }} value={policy.quests.all_done_item} onChange={(v: string) => onChange({ ...policy, quests: { ...policy.quests, all_done_item: v } })}
          options={[{ value: "", label: "不送" }, ...meta.items.map((item) => ({ value: item.key, label: ITEM_NAMES[item.key] ?? item.key }))]} aria-label="全做完送" /></label>
      </div>
      <Text type="secondary" className="me-hint">任务池：{meta.quests.map((q) => q.title).join("、")}</Text>
    </Card>
    <Card size="small" title="长期不来往的衰减">
      <div className="aff-form-row">
        <label><Switch checked={policy.decay.enabled} onChange={(v) => onChange({ ...policy, decay: { ...policy.decay, enabled: v } })} aria-label="衰减" />开启</label>
        <label>几天没来往开始 <InputNumber min={3} max={365} value={policy.decay.idle_days} onChange={(v) => onChange({ ...policy, decay: { ...policy.decay, idle_days: Number(v) || 14 } })} aria-label="衰减天数" /></label>
        <label>每天掉 <InputNumber min={1} max={50} value={policy.decay.per_day} onChange={(v) => onChange({ ...policy, decay: { ...policy.decay, per_day: Number(v) || 1 } })} aria-label="每天掉多少" /></label>
        <label><Checkbox checked={policy.decay.keep_level} onChange={(e) => onChange({ ...policy, decay: { ...policy.decay, keep_level: e.target.checked } })}>只掉到本级下限（不掉级）</Checkbox></label>
      </div>
    </Card>
    <Card size="small" title="久别问候（私聊）">
      <div className="aff-form-row">
        <label>几天没私聊算久别 <InputNumber min={1} max={60} value={policy.care.idle_days} onChange={(v) => onChange({ ...policy, care: { ...policy.care, idle_days: Number(v) || 4 } })} aria-label="久别天数" /></label>
        <label>超过几天就不再问 <InputNumber min={2} max={365} value={policy.care.give_up_days} onChange={(v) => onChange({ ...policy, care: { ...policy.care, give_up_days: Number(v) || 45 } })} aria-label="放弃天数" /></label>
      </div>
      <Text type="secondary" className="me-hint">只对解锁了“久别问候”的等级生效，受他自己的主动消息开关和免打扰约束。</Text>
    </Card>
    <Card size="small" title="其他">
      <div className="aff-form-row">
        <label>互动每天最多加好感 <InputNumber min={0} max={50} value={policy.interact_affinity_daily_cap} onChange={(v) => onChange({ ...policy, interact_affinity_daily_cap: Number(v) || 0 })} aria-label="互动日上限" /></label>
        <label><Switch checked={policy.announce_levelup} onChange={(v) => onChange({ ...policy, announce_levelup: v })} aria-label="升级提醒" />升级后让 Agent 自然提一句</label>
      </div>
    </Card>
  </div>;
}

export function AffinityAdminPage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [params, setParams] = useSearchParams();
  const tab = pickTab(params.get("aff.tab"));
  const policyQuery = useApiQuery({ queryKey: ["affinity", "policy"], fetcher: () => affinityAdminApi.policy() });
  const statsQuery = useApiQuery({ queryKey: ["affinity", "stats"], fetcher: () => affinityAdminApi.stats() });
  const [draft, setDraft] = useState<AffinityPolicy | null>(null);
  const [saving, setSaving] = useState(false);
  const [decaying, setDecaying] = useState(false);
  const saved = policyQuery.data?.policy ?? null;
  useEffect(() => {
    if (saved) setDraft(structuredClone(saved));
  }, [saved]);
  const dirty = Boolean(draft && saved && !policyEqual(draft, saved));
  useUnsavedChanges(dirty);
  const problem = useMemo(() => (draft ? validatePolicy(draft) : null), [draft]);
  const meta = policyQuery.data?.meta;

  const save = async () => {
    if (!draft || problem) return;
    setSaving(true);
    try {
      const result = await affinityAdminApi.savePolicy(draft);
      setDraft(structuredClone(result.policy));
      policyQuery.reload();
      message.success("好感策略已保存，立即生效");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };
  const reset = async () => {
    setSaving(true);
    try {
      await affinityAdminApi.resetPolicy();
      policyQuery.reload();
      message.success("已恢复默认策略");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };
  const decay = async () => {
    setDecaying(true);
    try {
      const result = await affinityAdminApi.runDecay();
      message.success(result.affected ? `${result.affected} 人掉了好感（共 ${result.total}）` : "没有人需要衰减");
      statsQuery.reload();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDecaying(false);
    }
  };

  const editor = !draft || !meta ? <Skeleton active paragraph={{ rows: 8 }} /> : null;
  return <div className="page-stack">
    <PageHeader title="好感度" subtitle="让好感真正有用：每个等级多给哪些额度、解锁什么玩法，礼物、商店、每日任务和长期不来往的衰减。"
      onRefresh={() => { policyQuery.reload(); statsQuery.reload(); }} refreshing={policyQuery.refreshing || statsQuery.refreshing}
      status={draft ? <Tag color={draft.enabled ? "success" : "default"}>{draft.enabled ? "玩法开启" : "玩法关闭"}</Tag> : undefined} />
    {policyQuery.error && !draft && <QueryErrorAlert error={policyQuery.error} onRetry={policyQuery.reload} />}
    {draft && <div className="aff-save-bar">
      <label className="aff-form-row"><Switch checked={draft.enabled} onChange={(v) => setDraft({ ...draft, enabled: v })} aria-label="好感玩法总开关" />好感玩法总开关</label>
      <SaveStatus dirty={dirty} saving={saving} />
      {problem && <Tag color="red">{problem}</Tag>}
      <span style={{ flex: 1 }} />
      <Button icon={<UndoOutlined />} disabled={!dirty || saving} onClick={() => saved && setDraft(structuredClone(saved))}>撤销修改</Button>
      <Popconfirm title="恢复默认策略？" description="所有等级权益、礼物、商店和规则都会回到默认值" onConfirm={() => void reset()}><Button icon={<ReloadOutlined />} disabled={saving}>恢复默认</Button></Popconfirm>
      <Button type="primary" icon={<SaveOutlined />} disabled={!dirty || Boolean(problem)} loading={saving} onClick={() => void save()}>保存</Button>
    </div>}
    <Tabs activeKey={tab} onChange={(key) => { const next = new URLSearchParams(params); if (key === "overview") next.delete("aff.tab"); else next.set("aff.tab", key); setParams(next, { replace: true }); }} items={[
      { key: "overview", label: <span><DashboardOutlined /> 概览</span>, children: statsQuery.error && !statsQuery.data ? <QueryErrorAlert error={statsQuery.error} onRetry={statsQuery.reload} /> : statsQuery.data ? <StatsView stats={statsQuery.data} onDecay={() => void decay()} decaying={decaying} /> : <Skeleton active /> },
      { key: "levels", label: <span><HeartOutlined /> 等级权益</span>, children: editor ?? <div className="aff-grid">{draft!.levels.map((level, index) => <LevelEditor key={level.level} level={level} floor={meta!.levels[index]?.floor ?? 0} meta={meta!} onChange={(next) => setDraft({ ...draft!, levels: draft!.levels.map((row, i) => (i === index ? next : row)) })} />)}</div> },
      { key: "economy", label: <span><GiftOutlined /> 礼物与商店</span>, children: editor ?? <EconomyEditor policy={draft!} meta={meta!} onChange={setDraft} /> },
      { key: "rules", label: <span><SettingOutlined /> 任务·衰减·问候</span>, children: editor ?? <RulesEditor policy={draft!} meta={meta!} onChange={setDraft} /> },
    ]} />
  </div>;
}

export default AffinityAdminPage;
