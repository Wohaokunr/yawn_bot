import { BulbOutlined, DeleteOutlined, EyeInvisibleOutlined, HistoryOutlined, PlusOutlined, ShareAltOutlined, TeamOutlined, UndoOutlined } from "@ant-design/icons";
import { Alert, Button, Checkbox, Empty, Input, Modal, Popconfirm, Select, Skeleton, Switch, Tag, Tooltip, Typography } from "antd";
import { useState } from "react";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type MemoryFact, type MemoryState, spaceApi } from "./me-space-api";
import { formatQqTime } from "./me-meta";
import { MeSection, UrlTabs, useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";

const { Text, Paragraph } = Typography;
const TABS = ["facts", "groups", "link", "forgotten"] as const;

function ForgetDialog({ fact, onClose, onDone }: { fact: MemoryFact | null; onClose: () => void; onDone: (next: MemoryState) => void }): React.JSX.Element {
  const { busy, run } = useAction();
  const [part, setPart] = useState("");
  const [block, setBlock] = useState(true);
  const [everywhere, setEverywhere] = useState(false);
  const parts = fact ? fact.content.split("、").filter((item) => item.trim()) : [];
  return <Modal
    open={fact !== null}
    title={fact ? `忘掉「${fact.label}」` : ""}
    okText="忘掉"
    okButtonProps={{ danger: true, loading: busy === "forget" }}
    onCancel={onClose}
    destroyOnHidden
    onOk={async () => {
      if (!fact) return;
      const next = await run("forget", () => spaceApi.forgetFact({ key: fact.key, keyword: part || undefined, block, everywhere }), (r) => (r.result?.changed ? "忘掉了" : "没有找到要忘的内容"));
      if (next) {
        onDone(next);
        onClose();
        setPart("");
      }
    }}
  >
    {fact && <div className="me-stack">
      <Paragraph>现在记着：<Text strong>{fact.content}</Text></Paragraph>
      {parts.length > 1 && (
        <Select
          allowClear
          placeholder="整项都忘掉（也可以只选其中一个）"
          value={part || undefined}
          onChange={(value) => setPart(value ?? "")}
          options={parts.map((value) => ({ value, label: `只忘掉“${value}”` }))}
          className="me-full"
        />
      )}
      <Checkbox checked={block} onChange={(event) => setBlock(event.target.checked)}>以后聊天里也别再记回来</Checkbox>
      {block && <Checkbox checked={everywhere} onChange={(event) => setEverywhere(event.target.checked)}>群里也别再记（私聊和各群都忘掉）</Checkbox>}
      <Text type="secondary" className="me-hint">之后你亲口让 Bot 再记，就会解除“别再记回来”。</Text>
    </div>}
  </Modal>;
}

function FactsTab({ data, setData }: { data: MemoryState; setData: (next: MemoryState) => void }): React.JSX.Element {
  const { busy, run } = useAction();
  const [key, setKey] = useState<string>("hobby");
  const [content, setContent] = useState("");
  const [forgetting, setForgetting] = useState<MemoryFact | null>(null);
  const known = new Set(data.facts.map((fact) => fact.key));

  return <div className="me-stack">
    {data.recap && (
      <MeSection icon={<HistoryOutlined />} title="上次聊到哪了" hint={`Bot 自己整理的对话回顾，下次接着聊用 · ${data.recap.updatedAt ?? ""}`} extra={
        <Popconfirm title="清掉这份回顾？" okText="清掉" onConfirm={async () => { const next = await run("recap", () => spaceApi.clearRecap(), "清掉了"); if (next) setData(next); }}>
          <Button size="small" icon={<DeleteOutlined />} loading={busy === "recap"}>清掉</Button>
        </Popconfirm>
      }>
        <Paragraph className="me-recap">{data.recap.summary}</Paragraph>
        {data.recap.openThreads.length > 0 && <div className="me-chip-row">{data.recap.openThreads.map((item) => <Tag key={item}>还没下文：{item}</Tag>)}</div>}
      </MeSection>
    )}

    <MeSection icon={<BulbOutlined />} title="Bot 在私聊里记得你的事" hint="只在你和 Bot 的私聊里用；要不要让群里也知道，在“群聊和私聊怎么互通”里定。">
      {data.facts.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没记住什么。可以在下面亲口告诉它，或在私聊里说“记住我喜欢……”" />
      ) : (
        <ul className="me-fact-list">
          {data.facts.map((fact) => (
            <li key={fact.key}>
              <span className="me-fact-label">{fact.label}</span>
              <span className="me-plain-main">
                <span className="me-fact-content">{fact.content}</span>
                <Text type="secondary" className="me-hint">{fact.sourceLabel} · 更新于 {formatQqTime(fact.updatedAt)}{fact.expiresAt ? ` · ${formatQqTime(fact.expiresAt).slice(0, 10)} 后自动淡忘` : ""}</Text>
              </span>
              {fact.protected ? <Tooltip title="管理员设置的，不能在这里删；有问题联系管理员"><Tag>管理员设置</Tag></Tooltip> : (
                <Button size="small" icon={<EyeInvisibleOutlined />} onClick={() => setForgetting(fact)}>忘掉</Button>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="me-composer me-fact-add">
        <Select value={key} onChange={setKey} options={Object.entries(data.labels).map(([value, label]) => ({ value, label: `${label}${known.has(value) ? (data.listKeys.includes(value) ? "（追加）" : "（替换）") : ""}` }))} className="me-composer-select" aria-label="类别" popupMatchSelectWidth={false} />
        <Input className="me-composer-main" placeholder="亲口告诉 Bot 一件关于你的事" maxLength={100} value={content} onChange={(event) => setContent(event.target.value)} onPressEnter={() => document.getElementById("me-fact-save")?.click()} />
        <Button id="me-fact-save" type="primary" icon={<PlusOutlined />} disabled={!content.trim()} loading={busy === "add"} onClick={async () => { const next = await run("add", () => spaceApi.addFact(key, content.trim()), "记住了"); if (next) { setData(next); setContent(""); } }}>记住</Button>
      </div>
      {data.facts.some((fact) => !fact.protected) && (
        <div className="me-form-actions">
          <Popconfirm title="清空私聊记忆？" description="Bot 会忘掉在私聊里记得的所有事和对话回顾（管理员设置的除外）。群里的记忆不受影响。" okText="全部忘掉" okButtonProps={{ danger: true }} onConfirm={async () => { const next = await run("all", () => spaceApi.forgetAll(), "都忘掉了"); if (next) setData(next); }}>
            <Button danger size="small" loading={busy === "all"}>全部忘掉</Button>
          </Popconfirm>
        </div>
      )}
    </MeSection>
    <ForgetDialog fact={forgetting} onClose={() => setForgetting(null)} onDone={setData} />
  </div>;
}

function GroupsTab({ data }: { data: MemoryState }): React.JSX.Element {
  return <MeSection icon={<TeamOutlined />} title="群里对你的了解" hint="Bot 在你所在的群里整理出的、关于你自己的画像（不显示是谁说的）。这些只在群里用；想让它在私聊里也别提，去“互通”里关掉。">
    {data.groupProfile.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="群里还没有关于你的画像" /> : (
      <ul className="me-fact-list">
        {data.groupProfile.map((item, index) => (
          <li key={`${item.key}-${index}`}>
            <span className="me-fact-label">{item.label}</span>
            <span className="me-plain-main">
              <span className="me-fact-content">{item.content}</span>
              <Text type="secondary" className="me-hint">{item.groups.join("、")}{item.selfTold ? " · 你在群里亲口说的" : ""}{item.asOf ? ` · ${item.asOf}` : ""}</Text>
            </span>
          </li>
        ))}
      </ul>
    )}
  </MeSection>;
}

function LinkTab({ data, setData }: { data: MemoryState; setData: (next: MemoryState) => void }): React.JSX.Element {
  const { busy, run } = useAction();
  const groupOptions = data.groups.map((group) => ({ value: group.groupId, label: group.name || `群 ${group.groupId}` }));
  const put = async (key: string, patch: Parameters<typeof spaceApi.putLink>[0]): Promise<void> => {
    const next = await run(key, () => spaceApi.putLink(patch), "改好了");
    if (next) setData(next);
  };
  if (!data.enabled) return <Alert type="info" showIcon message="管理员没有开启群聊和私聊记忆互通" description="现在私聊和群里的记忆是分开的，互不影响。" />;
  return <div className="me-stack">
    <MeSection icon={<TeamOutlined />} title="群里的事带不带进私聊">
      <div className="me-switch-row">
        <span className="me-plain-main"><strong>私聊里可以提群里的事</strong><Text type="secondary" className="me-hint">比如“你刚在 xx 群说……”。关掉后私聊只聊私聊里的事。</Text></span>
        <Switch checked={data.settings.groupsInPrivate} loading={busy === "gip"} onChange={(on) => void put("gip", { groupsInPrivate: on })} />
      </div>
      {data.settings.groupsInPrivate && (
        <div className="me-switch-row">
          <span className="me-plain-main"><strong>这些群的事别带进私聊</strong></span>
          <Select mode="multiple" allowClear placeholder="都可以" value={data.settings.mutedGroups} options={groupOptions} onChange={(value: string[]) => void put("muted", { mutedGroups: value })} className="me-wide-select" loading={busy === "muted"} />
        </div>
      )}
    </MeSection>
    <MeSection icon={<ShareAltOutlined />} title="私聊里的事让不让群里知道" hint="默认谁也不告诉。打开的类别，Bot 在群里聊到你时会参考（不会主动公开说出来）。">
      {!data.shareEnabled ? <Alert type="info" showIcon message="管理员没有开放把私聊记忆共享给群" /> : <>
        <div className="me-switch-row">
          <span className="me-plain-main"><strong>允许群里也知道</strong></span>
          <Checkbox.Group value={data.settings.shareKeys} options={data.shareableKeys.map((item) => ({ value: item.key, label: item.label }))} onChange={(value) => void put("share", { shareKeys: value as string[] })} disabled={busy === "share"} />
        </div>
        {data.settings.shareKeys.length > 0 && (
          <div className="me-switch-row">
            <span className="me-plain-main"><strong>只告诉这些群</strong></span>
            <Select mode="multiple" allowClear placeholder="能看到的群都可以" value={data.settings.shareGroups} options={groupOptions} onChange={(value: string[]) => void put("sg", { shareGroups: value })} className="me-wide-select" loading={busy === "sg"} />
          </div>
        )}
        {data.groups.some((group) => group.seesShared.length > 0) && (
          <ul className="me-plain-list">
            {data.groups.filter((group) => group.seesShared.length > 0).map((group) => (
              <li key={group.groupId}><strong>{group.name}</strong><Text type="secondary" className="me-plain-main">能参考：{group.seesShared.map((item) => `${item.label}「${item.content}」`).join("；")}</Text></li>
            ))}
          </ul>
        )}
      </>}
    </MeSection>
    {data.ledger.length > 0 && (
      <MeSection icon={<HistoryOutlined />} title="最近的变动">
        <ul className="me-plain-list">
          {data.ledger.slice(0, 15).map((row, index) => (
            <li key={`${row.at}-${index}`}><Text type="secondary" className="me-hint me-mono">{row.at}</Text><span className="me-plain-main">{row.what}{row.group ? ` · ${row.group}` : ""}{row.groups && row.groups.length > 0 ? ` · ${row.groups.join("、")}` : ""}{row.keyword ? ` · “${row.keyword}”` : ""}</span>{row.by === "self" && <Tag>网页上</Tag>}</li>
          ))}
        </ul>
      </MeSection>
    )}
  </div>;
}

function ForgottenTab({ data, setData }: { data: MemoryState; setData: (next: MemoryState) => void }): React.JSX.Element {
  const { busy, run } = useAction();
  return <MeSection icon={<EyeInvisibleOutlined />} title="让 Bot 别再记回来的事" hint={`Bot 整理聊天时会跳过这些内容，${data.tombstoneDays} 天后自动失效。撤销后它可能又从聊天里学到。`}>
    {data.tombstones.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有" /> : (
      <ul className="me-plain-list">
        {data.tombstones.map((item) => (
          <li key={item.index}>
            <span className="me-plain-main"><strong>{item.what}</strong>{item.keyword && <Text> “{item.keyword}”</Text>}<Text type="secondary" className="me-hint"> · {item.scope}{item.until ? ` · 到 ${item.until}` : ""}</Text></span>
            <Popconfirm title="撤销这条？" okText="撤销" onConfirm={async () => { const next = await run(`t-${item.index}`, () => spaceApi.dropTombstone(item.index), "撤销了"); if (next) setData(next); }}>
              <Button size="small" icon={<UndoOutlined />} loading={busy === `t-${item.index}`}>撤销</Button>
            </Popconfirm>
          </li>
        ))}
      </ul>
    )}
  </MeSection>;
}

export function MyMemoryPage(): React.JSX.Element {
  const { query, data, setData } = useResource<MemoryState>(["me", "memory"], () => spaceApi.memory());
  return <div className="me-page">
    <PageHeader title="Bot 记得我" subtitle="看看 Bot 记住了你什么，不想让它记的随时让它忘掉。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 6 }} /> : !data.available ? (
      <Alert type="warning" showIcon message="记忆暂时不可用" description="管理员还没有完成数据库迁移，稍后再来。" />
    ) : (
      <UrlTabs
        keys={TABS}
        fallback="facts"
        items={[
          { key: "facts", label: `私聊记忆 ${data.facts.length}`, children: <FactsTab data={data} setData={setData} /> },
          { key: "groups", label: `群里的我 ${data.groupProfile.length}`, children: <GroupsTab data={data} /> },
          { key: "link", label: "群聊和私聊怎么互通", children: <LinkTab data={data} setData={setData} /> },
          { key: "forgotten", label: `别再记回来 ${data.tombstones.length}`, children: <ForgottenTab data={data} setData={setData} /> },
        ]}
      />
    )}
  </div>;
}
