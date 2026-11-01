// 规则与配置：板子构成、角色图鉴、阶段时长、当前配置与群内命令速查。
import { SettingOutlined } from "@ant-design/icons";
import { Button, Input, Segmented, Skeleton, Table, Tag, Typography } from "antd";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { QueryErrorAlert } from "../shared";
import type { WwBoard, WwMeta } from "./types";
import { configRows, configValueText, formatDuration, phaseInfo, roleInfo, STAGE_META } from "./ww-meta";
import { RatioBar } from "./WwBits";

const { Text } = Typography;
type RulesTab = "boards" | "roles" | "timing" | "commands";

export function RulesView({ meta, error, onRetry, onOpenBoard }: { meta: WwMeta | null; error: string; onRetry: () => void; onOpenBoard?: (board: string) => void }): React.JSX.Element {
  const [tab, setTab] = useState<RulesTab>("boards");
  if (error && !meta) return <QueryErrorAlert error={error} onRetry={onRetry} />;
  if (!meta) return <Skeleton active paragraph={{ rows: 8 }} />;
  return (
    <div className="ww2-rules">
      <Segmented value={tab} onChange={(value) => setTab(value as RulesTab)} options={[
        { value: "boards", label: `板子 ${meta.boards.length}` },
        { value: "roles", label: `角色 ${meta.roles.length}` },
        { value: "timing", label: "时长与配置" },
        { value: "commands", label: `命令 ${meta.commands?.length ?? 0}` },
      ]} />
      {tab === "boards" && <div className="ww2-board-grid">{meta.boards.map((board) => <BoardCard key={board.key} board={board} onOpen={onOpenBoard ? () => onOpenBoard(board.key) : undefined} />)}</div>}
      {tab === "roles" && <RoleGallery meta={meta} />}
      {tab === "timing" && <TimingPanel meta={meta} />}
      {tab === "commands" && <CommandsPanel meta={meta} />}
    </div>
  );
}

function BoardCard({ board, onOpen }: { board: WwBoard; onOpen?: () => void }): React.JSX.Element {
  const [count, setCount] = useState(board.counts[board.counts.length - 1]?.players ?? 0);
  const composition = board.counts.find((row) => row.players === count) ?? board.counts[0];
  return (
    <article className="ww2-panel ww2-board">
      <header>
        <h4>{board.key}</h4>
        {board.isDefault && <Tag color="pink">默认</Tag>}
        {board.silenceMode && <Tag>{board.silenceMode === "vote" ? "禁票长老" : "禁言长老"}</Tag>}
        <span className="ww2-grow" />
        {onOpen && <Button size="small" type="link" onClick={onOpen}>看战绩</Button>}
      </header>
      {board.description && <p className="ww2-muted">{board.description}</p>}
      <div className="ww2-chip-row">
        <Text type="secondary">支持人数</Text>
        {board.counts.length > 1
          ? <Segmented size="small" value={count} onChange={(value) => setCount(Number(value))} options={board.counts.map((row) => ({ value: row.players, label: `${row.players} 人` }))} />
          : <Tag>{composition?.players} 人</Tag>}
      </div>
      {composition && (
        <>
          <RatioBar segments={[
            { value: composition.wolves, tone: "wolf", label: "狼人" },
            { value: composition.gods, tone: "god", label: "神职" },
            { value: composition.villagers, tone: "villager", label: "平民" },
          ]} />
          <div className="ww2-legend">
            <span><i className="is-wolf" />狼 {composition.wolves}</span>
            <span><i className="is-god" />神 {composition.gods}</span>
            <span><i className="is-villager" />民 {composition.villagers}</span>
          </div>
          <div className="ww2-chip-row">
            {composition.roles.map((row) => <span key={row.role} className={`ww2-role is-${row.faction}`}>{roleInfo(row.role, row.faction).icon}{row.role} × {row.count}</span>)}
          </div>
        </>
      )}
    </article>
  );
}

function RoleGallery({ meta }: { meta: WwMeta }): React.JSX.Element {
  const [query, setQuery] = useState("");
  const used = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const board of meta.boards) for (const row of board.counts.flatMap((item) => item.roles)) {
      const list = map.get(row.role) ?? [];
      if (!list.includes(board.key)) list.push(board.key);
      map.set(row.role, list);
    }
    return map;
  }, [meta.boards]);
  const needle = query.trim();
  const roles = meta.roles.filter((role) => !needle || role.role.includes(needle) || role.skill.includes(needle));
  return (
    <>
      <Input.Search allowClear placeholder="搜索角色或技能关键字" value={query} onChange={(event) => setQuery(event.target.value)} style={{ maxWidth: 280 }} />
      <div className="ww2-role-grid">
        {roles.map((role) => (
          <article key={role.role} className={`ww2-panel ww2-role-card is-${role.faction}`}>
            <header>
              <span className="ww2-role-icon" aria-hidden="true">{roleInfo(role.role, role.faction).icon}</span>
              <div>
                <h4>{role.role}</h4>
                <span className="ww2-muted">{role.faction === "wolf" ? "狼人阵营" : role.isGod ? "好人 · 神职" : "好人 · 平民"}</span>
              </div>
            </header>
            <p className="ww2-skill">{role.skill || "暂无说明"}</p>
            {role.voteVariantSkill && <details><summary>禁票长老板子的说明</summary><p className="ww2-skill">{role.voteVariantSkill}</p></details>}
            <div className="ww2-chip-row">
              <Text type="secondary">出现在</Text>
              {(used.get(role.role) ?? []).map((board) => <Tag key={board} bordered={false}>{board}</Tag>)}
              {!used.get(role.role) && <Text type="secondary">暂无板子</Text>}
            </div>
          </article>
        ))}
      </div>
    </>
  );
}

function TimingPanel({ meta }: { meta: WwMeta }): React.JSX.Element {
  const phases = Object.entries(meta.phaseTimeouts).map(([phase, seconds]) => ({ phase, seconds, info: phaseInfo(phase) }));
  const groups = configRows(meta.config);
  return (
    <div className="ww2-timing">
      <section className="ww2-panel">
        <h4>每个阶段的窗口</h4>
        <p className="ww2-muted">收到有效行动会提前结束；夜晚角色缺席时也会随机等待一段时间，避免泄露身份。</p>
        <Table size="small" rowKey="phase" pagination={false} dataSource={phases} columns={[
          { title: "环节", render: (_, row) => <>{STAGE_META[row.info.stage].icon} {row.info.label}</> },
          { title: "说明", render: (_, row) => <Text type="secondary">{row.info.hint}</Text> },
          { title: "时长", width: 110, render: (_, row) => formatDuration(row.seconds) },
        ]} />
      </section>
      <section className="ww2-panel">
        <header className="ww2-panel-head">
          <h4>当前配置</h4>
          <Link to="/environment?env.q=WW_"><Button size="small" icon={<SettingOutlined />}>去环境配置修改</Button></Link>
        </header>
        <p className="ww2-muted">这些值来自 .env 的 WW_* 配置项，修改后需重启生效。</p>
        {groups.map((group) => (
          <div key={group.group} className="ww2-config-group">
            <h5>{group.group}</h5>
            <dl className="ww2-config">
              {group.rows.map((row) => (
                <div key={row.key}>
                  <dt>{row.label}<code>{row.key.toUpperCase()}</code></dt>
                  <dd>{configValueText(row)}</dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </section>
    </div>
  );
}

const SCOPE_LABEL: Record<string, string> = { group: "群聊", private: "私聊" };
const PERMISSION_LABEL: Record<string, string> = { player: "局内玩家", room_host_or_admin: "房主/管理", admin: "管理员" };

function CommandsPanel({ meta }: { meta: WwMeta }): React.JSX.Element {
  const [query, setQuery] = useState("");
  const needle = query.trim();
  const rows = (meta.commands ?? []).filter((item) => !needle || item.name.includes(needle) || item.description.includes(needle) || item.aliases.some((alias) => alias.includes(needle)));
  return (
    <section className="ww2-panel">
      <Input.Search allowClear placeholder="搜索命令" value={query} onChange={(event) => setQuery(event.target.value)} style={{ maxWidth: 260, marginBottom: 10 }} />
      <Table size="small" rowKey="name" pagination={false} dataSource={rows} scroll={{ x: 560 }} columns={[
        { title: "命令", width: 140, render: (_, row) => <code>/{row.name}</code> },
        { title: "说明", dataIndex: "description" },
        { title: "别名", render: (_, row) => row.aliases.length ? row.aliases.map((alias) => <Tag key={alias} bordered={false}>/{alias}</Tag>) : "—" },
        { title: "场景", width: 80, render: (_, row) => SCOPE_LABEL[row.scope ?? ""] ?? "群/私聊" },
        { title: "权限", width: 100, render: (_, row) => PERMISSION_LABEL[row.permission ?? ""] ?? "所有人" },
      ]} />
      <p className="ww2-muted">夜间技能（刀、查验、救、毒等）也可以直接私聊机器人自由文本，如“刀3”“查验5”。</p>
    </section>
  );
}
