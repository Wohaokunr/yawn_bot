// 个人页「好感与签到」里的玩法与权益：这一级有什么用、今日任务、送礼、商店、背包、互动、专属称呼，
// 以及羁绊（关系称号、在一起的天数、连续来往、Bot 今天的心情、今日缘分、周任务、徽章、心动瞬间、来信）。
// 只在对应标签打开时挂载（antd Tabs 默认懒渲染），所以不影响页面首屏。
import {
  CalendarOutlined,
  CheckCircleOutlined,
  FireOutlined,
  HeartOutlined,
  MailOutlined,
  CrownOutlined,
  GiftOutlined,
  LockOutlined,
  ShoppingOutlined,
  SmileOutlined,
  StarFilled,
  TagOutlined,
  ThunderboltOutlined,
  TrophyOutlined,
} from "@ant-design/icons";
import { Alert, Button, Empty, Input, InputNumber, Progress, Select, Skeleton, Tag, Tooltip, Typography } from "antd";
import { useState } from "react";
import { QueryErrorAlert } from "../shared";
import { MeSection, StatTile, useAction, useResource } from "../me/space-ui";
import { affinityMeApi } from "./affinity-api";
import { badgeCounts, bondSettleText, bonusText, dailyBars, discountLabel, perkSummary, questDone, questPercent, sortBadges, TREND_TONE, unclaimed, weeklyPercent } from "./affinity-meta";
import type { ActionResponse, PerksState } from "./types";
import "./affinity.css";

const { Text } = Typography;

function questNote(response: Pick<ActionResponse, "quests">): string | null {
  const q = response.quests;
  const parts: string[] = [];
  if (q?.claimed?.length) parts.push(`完成任务：${q.claimed.join("、")}${q.affinity ? `，好感 +${q.affinity}` : ""}${q.bonus_item ? `，送你一张${q.bonus_item}` : ""}`);
  const bond = bondSettleText(q?.bond);
  if (bond) parts.push(bond);
  return parts.length ? parts.join("；") : null;
}

/** 权益：这一级有什么、下一级多什么、各级一览。 */
export function AffinityPerksView({ data }: { data: PerksState }): React.JSX.Element {
  const { current, next } = data;
  return <div className="aff-grid">
    <MeSection icon={<CrownOutlined />} title={`「${data.level}」的权益`} hint={current.behavior ? `Bot 会：${current.behavior}` : undefined}>
      {current.bonuses.length === 0 && current.unlocks.length === 0 ? <Text type="secondary">还没有加成，多来签到、聊天就会升级。</Text> : <>
        <ul className="aff-bonus-list">
          {current.bonuses.map((bonus) => <li key={bonus.key}><ThunderboltOutlined aria-hidden="true" /><span>{bonusText(bonus)}</span></li>)}
          <li><GiftOutlined aria-hidden="true" /><span>每天能送礼 {current.gift_daily} 次{current.shop_discount_pct ? `，商店 ${discountLabel(current.shop_discount_pct)}` : ""}</span></li>
        </ul>
        <div className="aff-tags">{current.unlocks.map((unlock) => <Tooltip key={unlock.key} title={unlock.description}><Tag icon={<CheckCircleOutlined />} color="success">{unlock.label}</Tag></Tooltip>)}</div>
      </>}
    </MeSection>
    <MeSection icon={<TrophyOutlined />} title={next ? `升到「${next.level}」还会多` : "已经是最高等级"} hint={next?.to_next != null ? `还差 ${next.to_next} 点` : undefined}>
      {!next ? <Text type="secondary">所有权益都解锁啦。</Text> : <>
        {next.unlocks.length > 0 && <div className="aff-tags">{next.unlocks.map((unlock) => <Tooltip key={unlock.key} title={unlock.description}><Tag icon={<LockOutlined />}>{unlock.label}</Tag></Tooltip>)}</div>}
        <ul className="aff-bonus-list">
          {next.better.map((row) => <li key={row.key}><ThunderboltOutlined aria-hidden="true" /><span>{row.label} 再 +{row.delta}{row.unit === "%" ? "%" : ""}</span></li>)}
          {next.gift_daily > current.gift_daily && <li><GiftOutlined aria-hidden="true" /><span>每天送礼 {next.gift_daily} 次</span></li>}
        </ul>
        {Object.keys(next.levelup_items).length > 0 && <Text type="secondary" className="me-hint">升级礼包会送道具</Text>}
      </>}
    </MeSection>
    <MeSection icon={<StarFilled />} title="各等级一览" className="aff-span">
      <ol className="aff-ladder">
        {data.ladder.map((row, index) => (
          <li key={row.name} className={index === data.levelIndex ? "is-current" : index < data.levelIndex ? "is-passed" : undefined}>
            <strong>{row.name}</strong><Text type="secondary" className="me-hint">{row.floor}+</Text>
            <span className="aff-ladder-text">{perkSummary(row)}</span>
          </li>
        ))}
      </ol>
    </MeSection>
  </div>;
}

/** 玩法：任务、送礼、商店、背包、互动、称呼。 */
export function AffinityPlayView({ data, onChange }: { data: PerksState; onChange: (next: PerksState) => void }): React.JSX.Element {
  const { busy, run } = useAction();
  const [giftCount, setGiftCount] = useState(1);
  const [repairGroup, setRepairGroup] = useState<string | null>(null);
  const [nickname, setNickname] = useState(data.nickname);
  const apply = async (key: string, task: () => Promise<ActionResponse>, message: (r: ActionResponse) => string) => {
    const response = await run(key, task, (r) => [message(r), questNote(r)].filter(Boolean).join("；"));
    if (response) onChange(response.state);
  };
  const giftsLeft = Math.max(data.gifts.dailyLimit - data.gifts.sentToday, 0);
  const quests = data.quests;
  return <div className="aff-grid">
    {!data.enabled && <Alert className="aff-span" type="info" showIcon message="好感玩法被管理员关掉了" description="等级仍会影响 Bot 说话的语气，送礼、商店、任务暂时用不了。" />}
    <div className="me-stat-row aff-span">
      <StatTile label="签到积分" value={data.wallet.total} sub={`${data.wallet.groups.length} 个群合计`} tone="brand" />
      <StatTile label="今日任务" value={`${questDone(quests)}/${quests.items.length}`} sub={unclaimed(quests) ? `${unclaimed(quests)} 个待领` : undefined} tone="success" />
      <StatTile label="今天送礼" value={`${data.gifts.sentToday}/${data.gifts.dailyLimit}`} />
      <StatTile label="背包" value={data.bag.items.reduce((sum, item) => sum + item.count, 0)} sub={data.bag.shield_until ? `护盾到 ${data.bag.shield_until}` : undefined} tone="warning" />
    </div>

    <MeSection icon={<CheckCircleOutlined />} title="今日任务" hint={`每完成一个好感 +${quests.reward_affinity}${quests.all_done_item_name ? `，全做完送${quests.all_done_item_name}` : ""}`} extra={
      unclaimed(quests) > 0 ? <Button size="small" type="primary" loading={busy === "claim"} onClick={() => void apply("claim", () => affinityMeApi.claim(), () => "领好了")}>领取</Button> : undefined
    }>
      {!quests.enabled ? <Text type="secondary">每日任务没开。</Text> : <>
        <Progress percent={questPercent(quests)} size="small" strokeColor="var(--brand)" aria-label="任务进度" />
        <ul className="aff-quest-list">
          {quests.items.map((item) => <li key={item.key} className={item.done ? "is-done" : undefined}>
            <span>{item.done ? "✅" : "⬜"} {item.title}</span>
            <Text type="secondary" className="me-hint">{item.progress}/{item.target}{item.claimed ? " · 已领" : ""}</Text>
          </li>)}
        </ul>
      </>}
    </MeSection>

    <MeSection icon={<GiftOutlined />} title="送 Bot 礼物" hint={`花签到积分，今天还能送 ${giftsLeft} 次；今日心仪效果 ×${data.gifts.favoriteMultiplier}`} extra={
      <InputNumber size="small" min={1} max={10} value={giftCount} onChange={(value) => setGiftCount(Number(value) || 1)} aria-label="数量" />
    }>
      {data.gifts.menu.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可送的礼物" /> : (
        <div className="aff-gift-grid">
          {data.gifts.menu.map((gift) => {
            const favorite = data.gifts.favorite === gift.key;
            const cost = gift.finalPrice * giftCount;
            const locked = gift.unlocked === false;
            return <button key={gift.key} type="button" className={`aff-gift${favorite ? " is-favorite" : ""}${locked ? " is-locked" : ""}`} disabled={!data.enabled || locked || giftsLeft < giftCount || cost > data.wallet.total || busy !== null}
              title={locked ? `「${gift.minLevel}」才能送` : undefined}
              onClick={() => void apply(`gift:${gift.key}`, () => affinityMeApi.gift(gift.key, giftCount), (r) => String((r.result as { reaction?: string }).reaction ?? "送出去啦"))}>
              <span className="aff-gift-emoji" aria-hidden="true">{gift.emoji}</span>
              <strong>{gift.name}</strong>
              <span className="me-hint">{locked ? <><LockOutlined /> 「{gift.minLevel}」解锁</> : <>{cost} 积分{favorite ? " · 今日心仪" : ""}</>}</span>
            </button>;
          })}
        </div>
      )}
    </MeSection>

    <MeSection icon={<ShoppingOutlined />} title="好感商店" hint="用签到积分换道具；等级越高折扣越多">
      {data.shop.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没上架" /> : (
        <ul className="me-plain-list">
          {data.shop.map((row) => <li key={row.key}>
            <span className="me-plain-main"><strong>{row.name}</strong> <Text type="secondary">· {row.description}</Text><br /><Text type="secondary" className="me-hint">{row.final_price} 积分{row.final_price !== row.price ? `（原价 ${row.price}）` : ""} · 已有 {row.have}/{row.max_hold}{row.unlocked ? "" : ` · 「${row.min_level}」解锁`}</Text></span>
            <Button size="small" disabled={!data.enabled || !row.unlocked || row.have >= row.max_hold || row.final_price > data.wallet.total} loading={busy === `buy:${row.key}`}
              onClick={() => void apply(`buy:${row.key}`, () => affinityMeApi.buy(row.key, 1), () => `换到一张${row.name}`)}>兑换</Button>
          </li>)}
        </ul>
      )}
    </MeSection>

    <MeSection icon={<TagOutlined />} title="背包" hint={data.bag.lucky_today ? "今天下一次签到积分翻倍" : undefined}>
      {data.bag.items.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="空空的，升级会送道具" /> : (
        <ul className="me-plain-list">
          {data.bag.items.map((item) => <li key={item.key}>
            <span className="me-plain-main"><strong>{item.name} ×{item.count}</strong> <Text type="secondary">· {item.description}</Text></span>
            {item.key === "repair_card" && data.wallet.groups.length > 1 && <Select size="small" allowClear placeholder="自动选群" className="aff-group-select" value={repairGroup} onChange={setRepairGroup}
              options={data.wallet.groups.map((group) => ({ value: String(group.group_id), label: group.group_name }))} />}
            <Button size="small" disabled={!data.enabled} loading={busy === `use:${item.key}`}
              onClick={() => void apply(`use:${item.key}`, () => affinityMeApi.use(item.key, item.key === "repair_card" ? repairGroup : null), (r) => String((r.result as { message?: string }).message ?? "用好了"))}>使用</Button>
          </li>)}
        </ul>
      )}
    </MeSection>

    <MeSection icon={<SmileOutlined />} title="互动" hint="越熟能做的越多；群里和私聊发 /摸头 这类命令也一样">
      <div className="aff-interact">
        {data.interactions.map((row) => <Tooltip key={row.key} title={row.unlocked ? `做过 ${row.count} 次` : `「${row.needLevel ?? "?"}」解锁`}>
          <Button disabled={!data.enabled || !row.unlocked} icon={row.unlocked ? undefined : <LockOutlined />} loading={busy === `act:${row.word}`}
            onClick={() => void apply(`act:${row.word}`, () => affinityMeApi.interact(row.word), (r) => String((r.result as { message?: string }).message ?? ""))}>{row.word}</Button>
        </Tooltip>)}
      </div>
    </MeSection>

    <MeSection icon={<TagOutlined />} title="专属称呼" hint={data.nicknameUnlockLevel ? `「${data.nicknameUnlockLevel}」解锁；群里和私聊的 Bot 都会这样叫你` : "没有开放"}>
      <div className="aff-nickname">
        <Input maxLength={12} value={nickname} placeholder="比如：小明" onChange={(event) => setNickname(event.target.value)} aria-label="专属称呼" disabled={!data.enabled} />
        <Button type="primary" disabled={!data.enabled || nickname.trim() === data.nickname} loading={busy === "nick"} onClick={() => void apply("nick", () => affinityMeApi.nickname(nickname.trim()), (r) => String((r.result as { message?: string }).message ?? "好了"))}>保存</Button>
        {data.nickname && <Button disabled={!data.enabled} loading={busy === "nick-clear"} onClick={() => { setNickname(""); void apply("nick-clear", () => affinityMeApi.nickname(""), () => "已取消"); }}>清除</Button>}
      </div>
      {data.nickname && !data.nicknameActive && <Text type="warning" className="me-hint">现在的等级还没解锁，暂时不生效</Text>}
    </MeSection>

    <MeSection icon={<TrophyOutlined />} title="升级与送礼记录" className="aff-span">
      {data.levelHistory.length === 0 && data.gifts.log.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有记录" /> : (
        <ul className="me-plain-list">
          {data.levelHistory.map((row) => <li key={`l${row.at}`}><Tag color={row.up ? "success" : "default"}>{row.up ? "升级" : "降级"}</Tag><span className="me-plain-main">到「{row.level}」</span><Text type="secondary" className="me-hint me-mono">{row.at.replace("T", " ").slice(0, 16)}</Text></li>)}
          {data.gifts.log.map((row) => <li key={`g${row.at}${row.key}`}><Tag color="magenta">送礼</Tag><span className="me-plain-main">{row.name}×{row.count} · {row.cost} 积分{row.favorite ? " · 今日心仪" : ""}</span><Text type="secondary" className="me-hint me-mono">{row.at.replace("T", " ").slice(0, 16)}</Text></li>)}
        </ul>
      )}
    </MeSection>
  </div>;
}

const CHANNEL_LABEL: Record<string, string> = { private: "私聊", group: "群里" };

function shortTime(at: string): string {
  return at.replace("T", " ").slice(0, 16);
}

/** 羁绊：我们的关系、今日缘分、周任务、徽章、心动瞬间、来信、最近两周。 */
export function AffinityBondView({ data, onChange }: { data: PerksState; onChange: (next: PerksState) => void }): React.JSX.Element {
  const { busy, run } = useAction();
  const bond = data.bond;
  const [openLetter, setOpenLetter] = useState<number | null>(null);
  const [showAllBadges, setShowAllBadges] = useState(false);
  if (!bond) return <Empty description="羁绊信息暂时取不到" />;
  const weekly = bond.weekly;
  const counts = badgeCounts(bond.badges);
  const badges = sortBadges(bond.badges);
  const shownBadges = showAllBadges ? badges : badges.slice(0, 9);
  const bars = dailyBars(bond.daily);
  const draw = async () => {
    const response = await run("fortune", () => affinityMeApi.fortune(), (r) => [r.result.first_today ? `抽到「${r.result.tier}」${r.result.affinity_up ? "，好感 +1" : ""}` : `今天已经抽过啦：${r.result.tier}`, questNote(r)].filter(Boolean).join("；"));
    if (response) onChange(response.state);
  };
  return <div className="aff-grid">
    <section className="aff-bond-hero aff-span" aria-label="我们的关系">
      <span className="aff-bond-mood" aria-hidden="true">{bond.mood.emoji}</span>
      <div className="aff-bond-main">
        <Text type="secondary">我们是</Text>
        <strong className="aff-bond-title">{bond.title || data.level}</strong>
        <Text type="secondary" className="me-hint">{bond.firstDay ? `从 ${bond.firstDay} 认识，已经 ${bond.daysTogether} 天` : "刚刚认识"} · 一起来往过 {bond.activeDays} 天</Text>
        <div className="aff-tags">
          <Tag color={TREND_TONE[bond.trend] ?? "default"}>最近{bond.trend}</Tag>
          <Tooltip title={bond.mood.hint}><Tag>Bot 今天{bond.mood.label}</Tag></Tooltip>
          {bond.initiativeText && <Tooltip title="Bot 主动找你的程度随关系变化"><Tag>{bond.initiativeText}</Tag></Tooltip>}
        </div>
        {bond.speechStyle && <Text type="secondary" className="me-hint">现在它和你说话：{bond.speechStyle}</Text>}
      </div>
      <div className="aff-bond-streak">
        <FireOutlined aria-hidden="true" />
        <strong>{bond.streak}</strong>
        <Text type="secondary" className="me-hint">天连续来往{bond.bestStreak > bond.streak ? ` · 最长 ${bond.bestStreak}` : ""}</Text>
        {bond.nextStreakMilestone != null && <Text type="secondary" className="me-hint">再 {Math.max(bond.nextStreakMilestone - bond.streak, 0)} 天到 {bond.nextStreakMilestone} 天奖励</Text>}
      </div>
    </section>

    <MeSection icon={<StarFilled />} title="今日缘分" hint="每天一签，第一次抽会涨一点好感；群里和私聊发 /今日缘分 也行" extra={
      <Button size="small" type={bond.fortune ? "default" : "primary"} disabled={!data.enabled} loading={busy === "fortune"} onClick={() => void draw()}>{bond.fortune ? "再看一眼" : "抽一签"}</Button>
    }>
      {!bond.fortune ? <Text type="secondary">今天还没抽，看看我们今天的缘分？</Text> : (
        <div className="aff-fortune">
          <strong className="aff-fortune-tier">{bond.fortune.tier}</strong>
          <ul className="aff-bonus-list">
            <li><span>宜：{bond.fortune.yi} · 忌：{bond.fortune.ji}</span></li>
            <li><GiftOutlined aria-hidden="true" /><span>幸运礼物：{bond.fortune.lucky_gift}</span></li>
            <li><SmileOutlined aria-hidden="true" /><span>{bond.fortune.advice}</span></li>
          </ul>
        </div>
      )}
    </MeSection>

    <MeSection icon={<CalendarOutlined />} title="本周任务" hint={`${weekly.ends} 截止；每完成一个好感 +${weekly.reward_affinity}${weekly.all_done_item_name ? `，全做完送${weekly.all_done_item_name}` : ""}`}>
      {!weekly.enabled ? <Text type="secondary">周任务没开。</Text> : <>
        <Progress percent={weeklyPercent(weekly)} size="small" strokeColor="var(--brand)" aria-label="周任务进度" />
        <ul className="aff-quest-list">
          {weekly.items.map((item) => <li key={item.key} className={item.done ? "is-done" : undefined}>
            <span>{item.done ? "✅" : "⬜"} {item.title}</span>
            <Text type="secondary" className="me-hint">{item.progress}/{item.target}{item.claimed ? " · 已领" : ""}</Text>
          </li>)}
        </ul>
        <Text type="secondary" className="me-hint">做完会自动发奖励，下次签到、聊天或打开这里时到账。</Text>
      </>}
    </MeSection>

    <MeSection icon={<TrophyOutlined />} title="羁绊徽章" hint={counts.total ? `已得到 ${counts.earned}/${counts.total}` : undefined} className="aff-span" extra={
      badges.length > 9 ? <Button size="small" type="link" onClick={() => setShowAllBadges(!showAllBadges)}>{showAllBadges ? "收起" : `全部 ${badges.length} 个`}</Button> : undefined
    }>
      {badges.length === 0 ? <Text type="secondary">徽章没开。</Text> : (
        <ul className="aff-badge-grid">
          {shownBadges.map((badge) => <li key={badge.key} className={badge.earned ? "is-earned" : undefined}>
            <span className="aff-badge-emoji" aria-hidden="true">{badge.earned ? badge.emoji : "🔒"}</span>
            <strong>{badge.name}</strong>
            <Text type="secondary" className="me-hint">{badge.description}</Text>
            {badge.earned ? <Text type="secondary" className="me-hint me-mono">{badge.earned_at ?? ""}</Text>
              : <Progress percent={Math.round((Math.min(badge.progress, badge.target) / Math.max(badge.target, 1)) * 100)} size="small" format={() => `${Math.min(badge.progress, badge.target)}/${badge.target}`} aria-label={`${badge.name} 进度`} />}
          </li>)}
        </ul>
      )}
    </MeSection>

    <MeSection icon={<HeartOutlined />} title="心动瞬间" hint="Bot 被你打动或被冒犯时会记下来，好感也会跟着变一点">
      {bond.moments.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有，多和它聊聊" /> : (
        <ul className="me-plain-list">
          {bond.moments.map((row) => <li key={`${row.at}${row.reason}`}>
            <Tag color={row.kind === "up" ? "magenta" : "default"}>{row.kind === "up" ? "心动" : "有点难过"}</Tag>
            <span className="me-plain-main">{row.reason}</span>
            <Text type="secondary" className="me-hint me-mono">{CHANNEL_LABEL[row.channel] ?? ""} {shortTime(row.at)}</Text>
          </li>)}
        </ul>
      )}
    </MeSection>

    <MeSection icon={<MailOutlined />} title="Bot 的来信" hint="升到新等级时它会给你写一封信（要那一级解锁“来信”）">
      {bond.letters.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有来信" /> : (
        <ul className="me-plain-list">
          {bond.letters.map((letter, index) => <li key={`${letter.at}${letter.level}`} className="aff-letter">
            <span className="me-plain-main">
              <button type="button" className="aff-letter-toggle" aria-expanded={openLetter === index} onClick={() => setOpenLetter(openLetter === index ? null : index)}>💌 升到「{letter.level}」那天</button>
              {openLetter === index && <span className="aff-letter-text">{letter.text}</span>}
            </span>
            <Text type="secondary" className="me-hint me-mono">{letter.sent ? "" : "还没送达 · "}{shortTime(letter.at)}</Text>
          </li>)}
        </ul>
      )}
    </MeSection>

    <MeSection icon={<ThunderboltOutlined />} title="最近两周涨了多少" className="aff-span">
      <div className="aff-bars" role="img" aria-label={`最近 14 天好感变化：${bars.map((row) => `${row.day.slice(5)} +${row.gain}`).join("，")}`}>
        {bars.map((row) => <Tooltip key={row.day} title={`${row.day}：+${row.gain}`}>
          <span className="aff-bar"><span className="aff-bar-fill" style={{ height: `${Math.max(row.height, row.gain > 0 ? 6 : 0)}%` }} /><span className="aff-bar-label">{row.day.slice(8)}</span></span>
        </Tooltip>)}
      </div>
    </MeSection>
  </div>;
}

/** 带取数的包装：标签打开时才请求 /me/affinity/perks。 */
export function MyAffinityPlay({ view }: { view: "perks" | "play" | "bond" }): React.JSX.Element {
  const { query, data, setData } = useResource<PerksState>(["me", "affinity", "perks"], () => affinityMeApi.perks());
  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 6 }} />;
  if (view === "bond") return <AffinityBondView data={data} onChange={setData} />;
  return view === "perks" ? <AffinityPerksView data={data} /> : <AffinityPlayView data={data} onChange={setData} />;
}
