import { DownloadOutlined, LockOutlined, ReloadOutlined, UndoOutlined, UnlockOutlined, UploadOutlined } from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Skeleton,
  Slider,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../api";
import { downloadJson } from "../agent-debug/trace-analysis";
import { cleanPersonaCard, PERSONA_LIMITS, PERSONA_TRAIT_META, PERSONA_VOICE_TRAITS, PERSONA_SOCIAL_TRAITS } from "../agent-meta";
import type { PersonaTraitKey } from "../agent-meta";
import { formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { PersonaCard } from "../types";
import { privateApi, type PersonaOptions, type PersonaPolicy, type PersonaPreviewResult, type PersonaUserRow, type PersonaUserState } from "./private-api";
import { PERSONA_MODE_META, personaActionLabel, personaByLabel, tokenBar } from "./private-meta";

const { Text, Paragraph } = Typography;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const LIST_FIELDS: Array<{ key: "catchphrases" | "likes" | "avoidTopics" | "examples"; label: string; max: number }> = [
  { key: "catchphrases", label: "口癖", max: PERSONA_LIMITS.catchphrases },
  { key: "likes", label: "喜欢的话题", max: 8 },
  { key: "avoidTopics", label: "避开的话题", max: 8 },
  { key: "examples", label: "示例台词", max: 4 },
];

/** 人设卡表单：字段与群聊人设页一致，私聊这里只编辑一张卡。 */
export function PersonaCardForm({ value, onChange }: { value: PersonaCard; onChange: (card: PersonaCard) => void }): React.JSX.Element {
  const set = <K extends keyof PersonaCard>(key: K, next: PersonaCard[K]): void => onChange({ ...value, [key]: next });
  const trait = (key: PersonaTraitKey): React.JSX.Element => <Form.Item key={key} label={PERSONA_TRAIT_META[key].label} tooltip={PERSONA_TRAIT_META[key].help} className="pa-trait">
    <Slider min={0} max={4} value={value[key]} onChange={(level) => set(key, level)} tooltip={{ formatter: (level) => PERSONA_TRAIT_META[key].levels[level ?? 0] }} />
    <Text type="secondary" className="dbg-small">{PERSONA_TRAIT_META[key].levels[value[key] ?? 0]}</Text>
  </Form.Item>;
  return <Form layout="vertical" className="pa-persona-form">
    <div className="pa-form-grid">
      <Form.Item label="名字"><Input value={value.name} maxLength={PERSONA_LIMITS.name} onChange={(event) => set("name", event.target.value)} aria-label="名字" /></Form.Item>
      <Form.Item label="自称"><Input value={value.selfName} maxLength={PERSONA_LIMITS.alias} onChange={(event) => set("selfName", event.target.value)} placeholder="我" aria-label="自称" /></Form.Item>
      <Form.Item label="怎么称呼他" tooltip="他在聊天偏好里设过“叫我…”时以他自己的为准"><Input value={value.userTitle} maxLength={PERSONA_LIMITS.alias} onChange={(event) => set("userTitle", event.target.value)} aria-label="称呼" /></Form.Item>
    </div>
    <Form.Item label="身份"><Input.TextArea value={value.identity} maxLength={PERSONA_LIMITS.identity} autoSize={{ minRows: 1, maxRows: 3 }} onChange={(event) => set("identity", event.target.value)} aria-label="身份" /></Form.Item>
    <Form.Item label="性格"><Input.TextArea value={value.personality} maxLength={PERSONA_LIMITS.longText} autoSize={{ minRows: 2, maxRows: 5 }} onChange={(event) => set("personality", event.target.value)} aria-label="性格" /></Form.Item>
    <Form.Item label="说话方式"><Input.TextArea value={value.speakingStyle} maxLength={PERSONA_LIMITS.longText} autoSize={{ minRows: 2, maxRows: 5 }} onChange={(event) => set("speakingStyle", event.target.value)} aria-label="说话方式" /></Form.Item>
    <div className="pa-form-grid">
      {LIST_FIELDS.map((field) => <Form.Item key={field.key} label={field.label}>
        <Select mode="tags" value={value[field.key]} onChange={(next: string[]) => set(field.key, next.slice(0, field.max))} tokenSeparators={["，", ","]} maxCount={field.max} aria-label={field.label} open={false} />
      </Form.Item>)}
    </div>
    <div className="pa-form-grid">{[...PERSONA_VOICE_TRAITS, ...PERSONA_SOCIAL_TRAITS].map(trait)}</div>
  </Form>;
}

function PolicyCard({ policy, options, onSaved }: { policy: PersonaPolicy; options: PersonaOptions; onSaved: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [draft, setDraft] = useState(policy);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(policy), [policy]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(policy);
  const save = async (): Promise<void> => {
    setSaving(true);
    try {
      await privateApi.savePolicy({
        enabled: draft.enabled,
        selfEdit: draft.selfEdit,
        selfEditDaily: draft.selfEditDaily,
        selfFields: draft.selfFields,
        allowPresets: draft.allowPresets,
        allowLibrary: draft.allowLibrary,
        defaultTemplateId: draft.defaultTemplateId ?? "",
        expectedRevision: policy.revision,
      });
      message.success("人设策略已保存，下一轮私聊生效");
      onSaved();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) { message.warning("策略刚被别处改过，已刷新"); onSaved(); } else message.error(errorText(reason));
    } finally {
      setSaving(false);
    }
  };
  return <Card size="small" title="私聊人设策略" extra={<Space>
    {policy.updatedAt && <Text type="secondary" className="dbg-small">第 {policy.revision} 版 · {formatTime(policy.updatedAt)}</Text>}
    <Button type="primary" size="small" disabled={!dirty} loading={saving} onClick={() => void save()}>保存策略</Button>
  </Space>}>
    <div className="pa-policy">
      <label className="pa-switch"><Switch checked={draft.enabled} onChange={(enabled) => setDraft({ ...draft, enabled })} aria-label="启用每用户人设" /><span><strong>启用每用户人设</strong><small>关掉后所有人暂时都用私聊默认，已有设置保留</small></span></label>
      <label className="pa-switch"><Switch checked={draft.selfEdit} onChange={(selfEdit) => setDraft({ ...draft, selfEdit })} aria-label="允许本人在私聊里改" /><span><strong>允许本人在私聊里改</strong><small>用户可以说“以后自称本喵”这类话，Agent 用 my_persona 工具改</small></span></label>
      <label className="pa-switch"><Switch checked={draft.allowPresets} onChange={(allowPresets) => setDraft({ ...draft, allowPresets })} aria-label="允许本人套用预设" /><span><strong>允许本人套用预设</strong><small>如“换成温柔一点的模板”</small></span></label>
      <label className="pa-switch"><Switch checked={draft.allowLibrary} onChange={(allowLibrary) => setDraft({ ...draft, allowLibrary })} aria-label="允许本人选人设库模板" /><span><strong>允许本人选人设库模板</strong><small>人设库在群组 → Agent 工作台 → 人设里维护</small></span></label>
      <div className="pa-field"><Text strong>本人每天最多改</Text><InputNumber min={0} max={100} value={draft.selfEditDaily} onChange={(value) => setDraft({ ...draft, selfEditDaily: Number(value ?? 0) })} addonAfter="次" aria-label="每天最多改几次" /></div>
      <div className="pa-field"><Text strong>私聊默认人设</Text><Select
        allowClear
        placeholder="跟随群聊全局人设"
        value={draft.defaultTemplateId ?? undefined}
        onChange={(value?: string) => setDraft({ ...draft, defaultTemplateId: value ?? null })}
        options={options.library.map((item) => ({ value: item.id, label: item.name, title: item.summary }))}
        aria-label="私聊默认人设"
        style={{ minWidth: 220 }}
      /></div>
      <div className="pa-field pa-field-wide"><Text strong>本人能改的字段</Text><Select
        mode="multiple"
        value={draft.selfFields}
        onChange={(selfFields: string[]) => setDraft({ ...draft, selfFields })}
        options={Object.entries(options.fields).map(([value, label]) => ({ value, label }))}
        aria-label="本人能改的字段"
        maxTagCount="responsive"
      /></div>
    </div>
  </Card>;
}

function PersonaPreview({ card }: { card: PersonaCard }): React.JSX.Element {
  const [result, setResult] = useState<PersonaPreviewResult | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => {
      privateApi.previewPersona(cleanPersonaCard(card)).then((value) => { setResult(value); setError(""); }, (reason: unknown) => setError(errorText(reason)));
    }, 400);
    return () => window.clearTimeout(timer);
  }, [card]);
  if (error) return <Text type="danger">{error}</Text>;
  if (!result) return <Skeleton active paragraph={{ rows: 2 }} />;
  const bar = tokenBar(result.promptTokens, result.promptTokenLimit);
  return <div className="pa-compiled">
    {result.unsafe && <Alert type="error" showIcon message={result.unsafe} />}
    <Space><Text strong>模型看到的人设</Text><Text type="secondary">约 {result.promptTokens} / {result.promptTokenLimit} Token{result.trimmed ? "（超出已裁剪）" : ""}</Text></Space>
    <Progress percent={bar.percent} status={bar.status} size="small" showInfo={false} />
    <pre className="pst-card-text">{result.compiled}</pre>
  </div>;
}

function UserDrawer({ userId, options, onClose, onChanged }: { userId: string; options: PersonaOptions; onClose: () => void; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<PersonaUserState>({
    queryKey: ["private-persona-user", userId],
    fetcher: (signal) => privateApi.personaUser(userId, signal),
    invalidation: { resources: ["private_agent_persona"] },
  });
  const [card, setCard] = useState<PersonaCard | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveAsOpen, setSaveAsOpen] = useState(false);
  const [templateName, setTemplateName] = useState("");
  const [switchUser, setSwitchUser] = useState(true);
  const [groupId, setGroupId] = useState("");
  const [importText, setImportText] = useState("");
  const state = query.data;
  useEffect(() => { if (state) setCard(state.card); }, [state]);
  const dirty = Boolean(state && card && JSON.stringify(card) !== JSON.stringify(state.card));

  const act = async (work: () => Promise<unknown>, done: string): Promise<void> => {
    setBusy(true);
    try {
      await work();
      message.success(done);
      query.reload();
      onChanged();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) { message.warning("这位用户的人设刚被改过（可能是他自己在私聊里改的），已刷新"); query.reload(); }
      else message.error(errorText(reason));
    } finally {
      setBusy(false);
    }
  };

  return <Drawer open width="min(820px, 100vw)" onClose={onClose} title={<span>私聊人设 · {userId}</span>} extra={state && card && <Space wrap size={4}>
    <Button size="small" disabled={!dirty} onClick={() => setCard(state.card)}>撤销编辑</Button>
    <Button type="primary" size="small" loading={busy} disabled={!dirty && state.mode === "custom"} onClick={() => void act(() => privateApi.setCard(userId, cleanPersonaCard(card), state.revision), "已保存为他的专属人设，下一句开始生效")}>保存为专属人设</Button>
  </Space>}>
    {query.error && !state ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : !state || !card ? <Skeleton active /> : <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <Descriptions size="small" column={{ xs: 1, sm: 2 }} bordered>
        <Descriptions.Item label="现在用的"><Space wrap><Tag color={state.personal ? "magenta" : "default"}>{state.sourceLabel}</Tag><Tag color={PERSONA_MODE_META[state.mode].color}>{PERSONA_MODE_META[state.mode].label}</Tag></Space></Descriptions.Item>
        <Descriptions.Item label="摘要">{state.summary}</Descriptions.Item>
        <Descriptions.Item label="称呼他">{state.callThem ?? "—"}{state.callThemSource === "chat_prefs" && <Text type="secondary">（他在聊天偏好里设的）</Text>}</Descriptions.Item>
        <Descriptions.Item label="本人今天改了">{state.selfEditsToday} / {state.selfEditDaily} 次</Descriptions.Item>
        <Descriptions.Item label="最近修改">{state.updatedAt ? `${formatTime(state.updatedAt)} · ${personaByLabel(state.updatedBy)}` : "—"}</Descriptions.Item>
        <Descriptions.Item label="锁定"><Switch checked={state.locked} loading={busy} checkedChildren={<LockOutlined />} unCheckedChildren={<UnlockOutlined />} onChange={(locked) => void act(() => privateApi.lock(userId, locked), locked ? "已锁定：他不能再自己改" : "已解锁")} aria-label="锁定人设" /></Descriptions.Item>
      </Descriptions>
      {state.missingTemplate && <Alert type="warning" showIcon message="他选的人设库模板已被删除，现在回退到私聊默认" />}
      {state.suspended && <Alert type="info" showIcon message="每用户人设总开关已关闭，他的设置暂不生效" />}
      <Space wrap>
        <Select
          placeholder="改为跟随人设库模板"
          options={options.library.map((item) => ({ value: item.id, label: item.name, title: item.summary }))}
          onChange={(value: string) => void act(() => privateApi.useTemplate(userId, value, state.revision), "已改为跟随这张模板")}
          style={{ minWidth: 200 }}
          aria-label="选择人设库模板"
          value={null}
        />
        <Select
          placeholder="套用预设"
          options={options.presets.map((item) => ({ value: item.id, label: item.label, title: item.description }))}
          onChange={(value: string) => void act(() => privateApi.usePreset(userId, value, state.revision), "已套用预设")}
          style={{ minWidth: 160 }}
          aria-label="套用预设"
          value={null}
        />
        <Space.Compact>
          <Input value={groupId} onChange={(event) => setGroupId(event.target.value.trim())} placeholder="群号" aria-label="复制群聊人设的群号" style={{ width: 120 }} />
          <Button disabled={!/^\d+$/.test(groupId)} onClick={() => void act(() => privateApi.copyGroup(userId, groupId, state.revision), "已复制群聊人设")}>复制该群人设</Button>
        </Space.Compact>
      </Space>
      <Space wrap>
        <Button icon={<UndoOutlined />} disabled={state.history.length === 0} onClick={() => void act(() => privateApi.undo(userId), "已撤销上一次修改")}>撤销上一次</Button>
        <Popconfirm title="恢复跟随默认？" description="他的专属卡会留在历史里，可以撤销。" onConfirm={() => act(() => privateApi.reset(userId, state.revision), "已恢复跟随默认")}>
          <Button disabled={state.mode === "follow"}>恢复跟随默认</Button>
        </Popconfirm>
        <Button onClick={() => { setTemplateName(state.effectiveCard.name); setSaveAsOpen(true); }}>存进人设库</Button>
        <Button icon={<DownloadOutlined />} onClick={() => void privateApi.exportPersona(userId).then((value) => downloadJson(`yawnbot-private-persona-${userId}.json`, value), (reason: unknown) => message.error(errorText(reason)))}>导出</Button>
        <Popconfirm title="彻底删除他的私聊人设？" description="包括修改历史，不能撤销。" okButtonProps={{ danger: true }} onConfirm={() => act(() => privateApi.clear(userId), "已删除")}>
          <Button danger>彻底删除</Button>
        </Popconfirm>
      </Space>
      <Card size="small" title="编辑专属人设卡" extra={state.mode !== "custom" && <Text type="secondary" className="dbg-small">当前不是专属卡，下面是他现在生效的人设，保存后变成专属</Text>}>
        <PersonaCardForm value={card} onChange={setCard} />
      </Card>
      <PersonaPreview card={card} />
      <Card size="small" title="导入">
        <Input.TextArea value={importText} onChange={(event) => setImportText(event.target.value)} placeholder="粘贴导出的 yawnbot.private-persona JSON" autoSize={{ minRows: 2, maxRows: 8 }} aria-label="导入人设 JSON" />
        <Button icon={<UploadOutlined />} style={{ marginTop: 8 }} disabled={!importText.trim()} onClick={() => {
          let payload: Record<string, unknown>;
          try { payload = JSON.parse(importText) as Record<string, unknown>; } catch { message.error("不是合法的 JSON"); return; }
          void act(() => privateApi.importPersona(userId, payload, state.revision), "已导入");
        }}>导入为专属人设</Button>
      </Card>
      <Card size="small" title={`修改历史（最近 ${state.history.length} 次）`}>
        {state.history.length === 0 ? <Text type="secondary">还没有改过</Text> : <ol className="pst-history">
          {state.history.map((item, index) => <li key={`${item.at}-${index}`}>
            <Text>{formatTime(item.at)} · {personaByLabel(item.by)} · {personaActionLabel(item.action)}</Text>
            {item.changed.length > 0 && <Text type="secondary">：改了 {item.changed.join("、")}</Text>}
            {item.before.summary && <div><Text type="secondary" className="dbg-small">改之前：{item.before.templateName ? `模板「${item.before.templateName}」 ` : ""}{item.before.summary}</Text></div>}
          </li>)}
        </ol>}
      </Card>
    </Space>}
    <Modal open={saveAsOpen} title="把他现在的人设存进人设库" okText="保存" cancelText="取消" confirmLoading={busy} onCancel={() => setSaveAsOpen(false)} onOk={() => void act(() => privateApi.saveAsTemplate(userId, templateName.trim(), switchUser), "已存进人设库").then(() => setSaveAsOpen(false))}>
      <Space orientation="vertical" style={{ width: "100%" }}>
        <Input value={templateName} onChange={(event) => setTemplateName(event.target.value)} maxLength={32} placeholder="模板名" aria-label="模板名" />
        <label className="pa-switch"><Switch checked={switchUser} onChange={setSwitchUser} /><span>同时让他改为跟随这张模板</span></label>
      </Space>
    </Modal>
  </Drawer>;
}

export function PersonaTab({ userId, onPickUser }: { userId: string; onPickUser: (userId: string | null) => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [q, setQ] = useState("");
  const [source, setSource] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [openUser, setOpenUser] = useState("");
  const query = useApiQuery({
    queryKey: ["private-persona-users", q, source],
    fetcher: (signal) => privateApi.personaUsers({ q, source }, signal),
    invalidation: { resources: ["private_agent_persona", "agent_persona_library"] },
  });
  const data = query.data;
  const templateNames = useMemo(() => new Map((data?.options.library ?? []).map((item) => [item.id, item.name])), [data]);

  const batch = async (action: "reset" | "lock" | "unlock" | "clear"): Promise<void> => {
    try {
      const result = await privateApi.batch(selected, action);
      message.success(`已处理 ${result.done.length} 位${result.failed.length ? `，${result.failed.length} 位失败` : ""}`);
      setSelected([]);
      query.reload();
    } catch (reason) {
      message.error(errorText(reason));
    }
  };

  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 8 }} />;
  const drawerUser = userId || openUser;

  return <Space orientation="vertical" size="middle" style={{ width: "100%" }} className="pa-persona">
    <Paragraph type="secondary" className="pa-block">
      每位用户在私聊里可以有自己的人设：管理员在这里给，或者用户在私聊里自己说（受下面的策略约束）。生效顺序：本人专属卡 → 本人选的人设库模板 → 私聊默认 → 群聊全局人设 → 内置默认。只影响私聊，群聊不受影响。
    </Paragraph>
    <div className="pa-stats">
      <Tag color="magenta">专属 {data.stats.custom}</Tag>
      <Tag color="purple">跟随模板 {data.stats.library}</Tag>
      <Tag>跟随默认（有历史）{data.stats.follow}</Tag>
      <Tag color={data.stats.locked ? "gold" : undefined}>锁定 {data.stats.locked}</Tag>
      {data.stats.missingTemplate > 0 && <Tag color="orange">模板已删 {data.stats.missingTemplate}</Tag>}
      <Tag color="blue">今天本人改了 {data.stats.selfEditsToday} 次</Tag>
      {Object.entries(data.templateUsage).map(([id, count]) => <Tag key={id} className="dbg-tag-soft">「{templateNames.get(id) ?? `#${id}`}」{count} 人</Tag>)}
    </div>
    <PolicyCard policy={data.policy} options={data.options} onSaved={query.reload} />
    <Card size="small" title="有私聊人设的用户" extra={<Space wrap>
      <Input.Search allowClear placeholder="QQ、人设名或模板" onSearch={setQ} style={{ width: 200 }} aria-label="搜索用户" />
      <Select value={source} onChange={setSource} style={{ width: 140 }} aria-label="按来源筛选" options={[{ value: "", label: "全部" }, { value: "custom", label: "专属" }, { value: "library", label: "跟随模板" }, { value: "follow", label: "跟随默认" }]} />
      <Input.Search placeholder="打开某位用户" enterButton="打开" onSearch={(value) => { const clean = value.trim(); if (/^\d{5,12}$/.test(clean)) { setOpenUser(clean); onPickUser(clean); } else message.warning("请输入 QQ 号"); }} style={{ width: 200 }} aria-label="打开用户" />
      <Button icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing} aria-label="刷新人设列表" />
    </Space>}>
      {selected.length > 0 && <Space wrap className="pa-batch">
        <Text>已选 {selected.length} 位：</Text>
        <Button size="small" onClick={() => void batch("reset")}>恢复跟随默认</Button>
        <Button size="small" onClick={() => void batch("lock")}>锁定</Button>
        <Button size="small" onClick={() => void batch("unlock")}>解锁</Button>
        <Popconfirm title={`彻底删除 ${selected.length} 位用户的私聊人设？`} okButtonProps={{ danger: true }} onConfirm={() => batch("clear")}><Button size="small" danger>彻底删除</Button></Popconfirm>
      </Space>}
      <Table<PersonaUserRow>
        size="small"
        rowKey="userId"
        dataSource={data.users}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        rowSelection={{ selectedRowKeys: selected, onChange: (keys) => setSelected(keys.map(String)) }}
        locale={{ emptyText: "还没有人有私聊人设。用户在私聊里说“以后自称本喵”，或在右上角打开某位用户来设置。" }}
        onRow={(row) => ({ onClick: () => { setOpenUser(row.userId); onPickUser(row.userId); }, className: "pa-row" })}
        scroll={{ x: 720 }}
        columns={[
          { title: "用户", dataIndex: "userId", width: 120 },
          { title: "方式", width: 120, render: (_, row) => <Tag color={PERSONA_MODE_META[row.mode].color}>{PERSONA_MODE_META[row.mode].label}</Tag> },
          { title: "人设", render: (_, row) => <span className="pa-cell"><strong>{row.name}</strong><small>{row.templateName ? `模板「${row.templateName}」 · ` : ""}{row.summary}</small></span> },
          { title: "状态", width: 140, render: (_, row) => <Space size={4} wrap>{row.locked && <Tag color="gold">锁定</Tag>}{row.missingTemplate && <Tag color="orange">模板已删</Tag>}{row.selfEditsToday > 0 && <Tag color="blue">今天改 {row.selfEditsToday}</Tag>}</Space> },
          { title: "最近修改", width: 170, render: (_, row) => row.updatedAt ? <span className="pa-cell"><span>{formatTime(row.updatedAt)}</span><small>{personaByLabel(row.updatedBy)}</small></span> : "—" },
        ]}
      />
    </Card>
    {drawerUser && <UserDrawer key={drawerUser} userId={drawerUser} options={data.options} onClose={() => { setOpenUser(""); onPickUser(null); }} onChanged={query.reload} />}
  </Space>;
}
