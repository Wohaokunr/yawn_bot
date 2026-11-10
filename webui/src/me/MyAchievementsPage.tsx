import { LockOutlined, StarFilled, TrophyOutlined } from "@ant-design/icons";
import { Empty, Progress, Segmented, Skeleton, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { PageHeader, QueryErrorAlert } from "../shared";
import { useUiPrefs } from "./hub-ui";
import { type Achievement, type Achievements, hubApi } from "./me-hub-api";
import { MeSection, StatTile, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text } = Typography;

type Show = "all" | "done" | "todo";

function Badge({ item, fresh }: { item: Achievement; fresh: boolean }): React.JSX.Element {
  return <Tooltip title={item.done ? `${item.unlockedAt ?? ""} 达成` : `${item.value}/${item.target}`}>
    <article className={`me-badge${item.done ? " is-done" : ""}${fresh ? " is-fresh" : ""}`} aria-label={`${item.title}：${item.description}，${item.done ? "已达成" : `进度 ${item.value}/${item.target}`}`}>
      <span className="me-badge-icon" aria-hidden="true">{item.done ? item.icon : <LockOutlined />}</span>
      <strong className="me-badge-title">{item.title}{fresh && <Tag color="magenta" className="me-inline-tag">新</Tag>}</strong>
      <Text type="secondary" className="me-hint">{item.description}</Text>
      {item.done
        ? <Text type="secondary" className="me-hint me-mono">{item.unlockedAt}</Text>
        : <Progress percent={Math.round(item.progress * 100)} size="small" format={() => `${item.value}/${item.target}`} strokeColor="var(--brand)" />}
    </article>
  </Tooltip>;
}

export function MyAchievementsPage(): React.JSX.Element {
  const { query, data } = useResource<Achievements>(["me", "achievements"], () => hubApi.achievements());
  const { prefs, save } = useUiPrefs();
  const [show, setShow] = useState<Show>("all");
  const seen = useMemo(() => new Set(prefs?.seenAchievements ?? []), [prefs]);
  const doneKeys = useMemo(() => (data?.groups ?? []).flatMap((group) => group.items).filter((item) => item.done).map((item) => item.key), [data]);
  // 第一次看到的成就标“新”，离开页面时记为看过
  const [freshKeys, setFreshKeys] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (!prefs || !data) return undefined;
    const unseen = doneKeys.filter((key) => !seen.has(key));
    if (unseen.length === 0) return undefined;
    setFreshKeys(new Set(unseen));
    return () => save({ seenAchievements: doneKeys });
    // 只在数据第一次到齐时登记
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Boolean(prefs), Boolean(data)]);

  return <div className="me-page">
    <PageHeader title="成就" subtitle="按你在 Bot 这里留下的真实数据算：签到、关系、小本本、聊天、游戏。达成过的不会收回。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 8 }} /> : <>
      <section className="me-ach-hero">
        <Progress type="circle" size={84} percent={Math.round((data.unlocked / Math.max(data.total, 1)) * 100)} format={() => `${data.unlocked}/${data.total}`} strokeColor="var(--brand)" aria-label="成就完成度" />
        <div className="me-ach-hero-main">
          <strong><TrophyOutlined /> 已达成 {data.unlocked} 个</strong>
          <Text type="secondary" className="me-hint">{data.unlocked === data.total ? "全部达成，了不起！" : `还有 ${data.total - data.unlocked} 个等你解锁`}</Text>
          {data.next.length > 0 && <div className="me-ach-next">
            <Text type="secondary" className="me-hint">最接近的：</Text>
            {data.next.map((item) => <Tag key={item.key} icon={<StarFilled />}>{item.title} {item.value}/{item.target}</Tag>)}
          </div>}
        </div>
      </section>
      <div className="me-stat-row">
        {data.groups.map((group) => <StatTile key={group.label} label={group.label} value={`${group.unlocked}/${group.items.length}`} tone={group.unlocked === group.items.length ? "success" : undefined} />)}
      </div>
      <Segmented<Show> value={show} onChange={setShow} options={[{ value: "all", label: "全部" }, { value: "done", label: `已达成 ${data.unlocked}` }, { value: "todo", label: `还没达成 ${data.total - data.unlocked}` }]} aria-label="筛选成就" />
      {data.groups.map((group) => {
        const items = group.items.filter((item) => show === "all" || (show === "done") === item.done);
        if (items.length === 0) return null;
        return <MeSection key={group.label} title={group.label} extra={<Text type="secondary" className="me-hint">{group.unlocked}/{group.items.length}</Text>}>
          <div className="me-badge-grid">{items.map((item) => <Badge key={item.key} item={item} fresh={freshKeys.has(item.key)} />)}</div>
        </MeSection>;
      })}
      {show !== "all" && data.groups.every((group) => group.items.every((item) => (show === "done") !== item.done)) && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={show === "done" ? "还没有达成的成就，先去签个到吧" : "全都达成了"} />}
    </>}
  </div>;
}
