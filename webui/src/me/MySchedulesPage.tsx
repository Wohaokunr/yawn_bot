// 我的定时任务：私聊提醒和群定时任务放在一处看、一处改。
// 私聊提醒只发给自己；群任务在设的群里执行。群任务只能管自己设的，群主/管理员还能管本群别人设的。
import { CaretRightOutlined, ClockCircleOutlined, DeleteOutlined, EditOutlined, PauseOutlined, PlusOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Drawer, Empty, Input, InputNumber, Popconfirm, Radio, Segmented, Select, Skeleton, Space, Switch, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import { draftFromRule, emptyRuleDraft, formatLocal, privateSourceMeta, privateStatusMeta, ruleFromDraft, sourceLabel, STATUS_META, validateRuleDraft } from "../schedules/schedule-meta";
import type { RuleDraft } from "../schedules/schedule-meta";
import { parseMentionUsers, RuleEditor } from "../schedules/TaskEditor";
import type { ScheduleMode, ScheduleStatus } from "../schedules/types";
import { isPrivateItem, myScheduleApi } from "./me-schedule-api";
import type { MyScheduleAction, MyScheduleDetail, MyScheduleGroup, MyScheduleItem, MySchedulePreview, MySchedulesData } from "./me-schedule-api";
import "./me.css";

const { Text } = Typography;

type Channel = "all" | "private" | "group";

interface FormState {
  ref: string | null;
  target: "private" | "group";
  groupId: string;
  content: string;
  title: string;
  mode: ScheduleMode;
  maxRuns: number | null;
  mentionMe: boolean;
  mentionAll: boolean;
  mentionUsers: string;
  rule: RuleDraft;
  revision?: number;
}

function blankForm(): FormState {
  return { ref: null, target: "private", groupId: "", content: "", title: "", mode: "notify", maxRuns: null, mentionMe: true, mentionAll: false, mentionUsers: "", rule: emptyRuleDraft("daily") };
}

function itemText(item: MyScheduleItem): { content: string; rule: string } {
  return "content" in item ? { content: item.content, rule: item.description } : { content: item.instruction, rule: item.description };
}

function statusTag(item: MyScheduleItem): React.JSX.Element {
  if (isPrivateItem(item)) {
    const meta = privateStatusMeta(String(item.status));
    return <Tag color={meta.color}>{meta.label}</Tag>;
  }
  const meta = STATUS_META[item.status as ScheduleStatus] ?? { label: String(item.status), color: "default" };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

function whereTag(item: MyScheduleItem): React.JSX.Element {
  if (isPrivateItem(item)) {
    const source = "sourceKind" in item ? privateSourceMeta(String(item.sourceKind)) : privateSourceMeta("private");
    return <Space size={4}><Tag color="blue">私聊提醒我</Tag><Tag color={source.color}>{source.label}</Tag></Space>;
  }
  const task = item as Extract<MyScheduleItem, { groupId: string }>;
  return (
    <Space size={4} wrap>
      <Tag color="purple">群「{task.groupName ?? task.groupId}」</Tag>
      {task.sourceKind && task.sourceKind !== "agent" && <Tag>{sourceLabel(task.sourceKind)}</Tag>}
      {task.mine === false && <Tag color="gold">{task.creatorName ?? task.creatorUserId} 设的（你是管理员）</Tag>}
    </Space>
  );
}

function canPause(item: MyScheduleItem): boolean {
  return item.status === "active" || item.status === "retrying";
}

function isOpen(item: MyScheduleItem): boolean {
  return ["active", "paused", "retrying", "broken"].includes(String(item.status));
}

function ScheduleRow({ item, onEdit, onAction }: { item: MyScheduleItem; onEdit: (item: MyScheduleItem) => void; onAction: (item: MyScheduleItem, action: MyScheduleAction) => void }): React.JSX.Element {
  const { content, rule } = itemText(item);
  const mode = item.mode === "agent" ? (isPrivateItem(item) ? "到点替我去办" : "到点唤醒 Agent") : "到点提醒";
  return (
    <li className="me-schedule-row">
      <span className="me-schedule-icon" aria-hidden="true"><ClockCircleOutlined /></span>
      <span className="me-schedule-main">
        <span className="me-schedule-title">{item.title ? <strong>{item.title}：</strong> : null}{content}</span>
        <span className="me-schedule-meta">
          {whereTag(item)}
          {statusTag(item)}
          <Text type="secondary">{rule} · {mode}</Text>
        </span>
        <Text type="secondary" className="me-hint">
          {item.nextRunAt ? `下次 ${formatLocal(item.nextRunAt)}` : "暂无下次"} · 已执行 {item.runCount} 次{item.maxRuns ? ` / 共 ${item.maxRuns} 次` : ""} · <code>{item.ref}</code>
        </Text>
      </span>
      {isOpen(item) && (
        <span className="me-schedule-actions">
          <Tooltip title="修改"><Button size="small" type="text" icon={<EditOutlined />} aria-label={`修改 ${item.ref}`} onClick={() => onEdit(item)} /></Tooltip>
          {canPause(item) && <Tooltip title="暂停"><Button size="small" type="text" icon={<PauseOutlined />} aria-label={`暂停 ${item.ref}`} onClick={() => onAction(item, "pause")} /></Tooltip>}
          {item.status === "paused" && <Tooltip title="恢复"><Button size="small" type="text" icon={<CaretRightOutlined />} aria-label={`恢复 ${item.ref}`} onClick={() => onAction(item, "resume")} /></Tooltip>}
          <Popconfirm title={isPrivateItem(item) ? "删除这条私聊提醒？" : "取消这个群定时任务？"} okText="确定" cancelText="再想想" onConfirm={() => onAction(item, "cancel")}>
            <Tooltip title={isPrivateItem(item) ? "删除" : "取消"}><Button size="small" type="text" danger icon={<DeleteOutlined />} aria-label={`取消 ${item.ref}`} /></Tooltip>
          </Popconfirm>
        </span>
      )}
    </li>
  );
}

function ScheduleForm({ open, form, groups, data, onClose, onSaved }: {
  open: boolean;
  form: FormState;
  groups: MyScheduleGroup[];
  data: MySchedulesData | null;
  onClose: () => void;
  onSaved: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [state, setState] = useState<FormState>(form);
  const [preview, setPreview] = useState<MySchedulePreview | null>(null);
  const [saving, setSaving] = useState(false);
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => { setState((current) => ({ ...current, [key]: value })); setPreview(null); };
  const ruleErrors = useMemo(() => validateRuleDraft(state.rule), [state.rule]);
  const creatable = groups.filter((group) => group.canCreate);
  const policy = data?.policy;
  const agentAllowed = state.target === "private" ? policy?.privateAgentMode ?? true : policy?.allowAgentMode ?? true;
  const editing = state.ref !== null;
  const contentError = !state.content.trim() ? "写一下到点要做什么" : "";
  const groupError = state.target === "group" && !state.groupId ? "选一个群" : "";
  const invalid = Boolean(contentError || groupError || Object.keys(ruleErrors).length);

  const runPreview = async () => {
    try {
      setPreview(await myScheduleApi.preview({ target: state.target, schedule: ruleFromDraft(state.rule), maxRuns: state.maxRuns, mode: state.mode }));
    } catch (error) {
      setPreview({ valid: false, error: error instanceof Error ? error.message : "预览失败" });
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const payload = {
        target: state.target,
        groupId: state.target === "group" ? state.groupId : undefined,
        content: state.content.trim(),
        title: state.title.trim(),
        mode: agentAllowed ? state.mode : "notify" as ScheduleMode,
        schedule: ruleFromDraft(state.rule),
        maxRuns: state.maxRuns,
        mentionMe: state.mentionMe,
        ...(state.target === "group" ? { mentionAll: state.mentionAll, mentionUserIds: parseMentionUsers(state.mentionUsers).ids } : {}),
      };
      if (editing && state.ref) {
        await myScheduleApi.update(state.ref, { ...payload, revision: state.revision });
        message.success("已保存");
      } else {
        const result = await myScheduleApi.create(payload);
        message.success(result.created ? (state.target === "group" ? "群定时任务设好了" : "私聊提醒设好了") : "已经有一模一样的，沿用原来那条");
      }
      onSaved();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer open={open} onClose={onClose} title={editing ? `修改 ${state.ref}` : "新建定时任务"} width={560} destroyOnHidden
      extra={<Button type="primary" loading={saving} disabled={invalid} onClick={() => void save()}>{editing ? "保存" : "设好"}</Button>}>
      <div className="me-schedule-form">
        <div className="sbx-field">
          <span className="sbx-field-label">发到哪里</span>
          <Radio.Group value={state.target} disabled={editing} optionType="button" onChange={(event) => set("target", event.target.value as FormState["target"])}
            options={[
              { value: "private", label: "私聊提醒我" },
              { value: "group", label: "发到群里", disabled: !policy?.crossCreate || creatable.length === 0 || data?.groupReady === false },
            ]} />
          {!editing && state.target === "private" && <span className="sbx-muted">只发给你自己，和你在私聊里让 Bot 设的提醒是同一份。</span>}
          {!editing && policy && !policy.crossCreate && <span className="sbx-muted">管理员没开放在私聊/网页上给群设任务。</span>}
        </div>
        {state.target === "group" && (
          <label className="sbx-field">
            <span className="sbx-field-label">哪个群</span>
            <Select value={state.groupId || undefined} disabled={editing} placeholder="选择你在的群" status={groupError ? "error" : undefined} onChange={(value) => set("groupId", value)} aria-label="选择群"
              options={groups.map((group) => ({ value: group.groupId, label: `${group.groupName}${group.canCreate ? "" : group.scheduleOpen ? "（仅管理员可设）" : "（没开放定时任务）"}`, disabled: !group.canCreate }))} />
            {groupError && <span className="sbx-field-error">{groupError}</span>}
            <span className="sbx-muted">到点在这个群里执行，群里所有人都能看到；只有你和群管理员能改。</span>
          </label>
        )}
        <label className="sbx-field">
          <span className="sbx-field-label">到点做什么</span>
          <Input.TextArea value={state.content} autoSize={{ minRows: 2, maxRows: 6 }} maxLength={state.target === "group" ? 600 : 300} showCount status={contentError ? "error" : undefined}
            placeholder={state.target === "group" ? "例如：提醒大家周五前交周报" : "例如：喝水 / 查一下明天天气告诉我"} onChange={(event) => set("content", event.target.value)} aria-label="到点做什么" />
        </label>
        <label className="sbx-field">
          <span className="sbx-field-label">标题（可选）</span>
          <Input value={state.title} maxLength={40} onChange={(event) => set("title", event.target.value)} aria-label="标题" />
        </label>
        <div className="sbx-field">
          <span className="sbx-field-label">方式</span>
          <Radio.Group value={agentAllowed ? state.mode : "notify"} optionType="button" onChange={(event) => set("mode", event.target.value as ScheduleMode)}
            options={[
              { value: "notify", label: "到点提醒" },
              { value: "agent", label: state.target === "private" ? "到点替我去办" : "到点唤醒 Agent", disabled: !agentAllowed },
            ]} />
          <span className="sbx-muted">{state.mode === "agent" && agentAllowed
            ? state.target === "private" ? "到点 Bot 会在私聊里自己去办（查资料、整理信息），办完告诉你；那一轮不会替你去群里说话。" : "到点 Agent 在群里思考并可调用工具，再决定说什么。"
            : "到点把内容原样发出来，最省也最稳。"}</span>
        </div>
        <RuleEditor draft={state.rule} errors={ruleErrors} onChange={(next) => set("rule", next)} />
        <div className="sch-field-grid-3 sbx-field-grid">
          <label className="sbx-field">
            <span className="sbx-field-label">最多执行几次（可选）</span>
            <InputNumber min={1} max={10000} value={state.maxRuns} onChange={(value) => set("maxRuns", typeof value === "number" ? Math.round(value) : null)} style={{ width: "100%" }} aria-label="最多执行几次" />
          </label>
          {state.target === "group" && (
            <div className="sbx-switch-row">
              <Switch checked={state.mentionMe} onChange={(value) => set("mentionMe", value)} aria-label="提醒时 @我" />
              <div><strong>提醒时 @我</strong></div>
            </div>
          )}
          {state.target === "group" && (
            <div className="sbx-switch-row">
              <Switch checked={state.mentionAll} onChange={(value) => set("mentionAll", value)} aria-label="提醒时 @全体成员" />
              <div><strong>提醒时 @全体成员</strong><span className="sbx-muted">要你是这个群的群主/管理员</span></div>
            </div>
          )}
        </div>
        {state.target === "group" && (
          <label className="sbx-field">
            <span className="sbx-field-label">提醒时还要 @ 谁（可选）</span>
            <Input.TextArea
              value={state.mentionUsers}
              autoSize={{ minRows: 1, maxRows: 3 }}
              placeholder="群成员 QQ 号，空格或逗号分隔"
              status={parseMentionUsers(state.mentionUsers).invalid.length ? "error" : undefined}
              onChange={(event) => set("mentionUsers", event.target.value)}
              aria-label="提醒时还要 @ 的成员"
            />
            {parseMentionUsers(state.mentionUsers).invalid.length > 0 && <span className="sbx-field-error">不是 QQ 号：{parseMentionUsers(state.mentionUsers).invalid.slice(0, 3).join("、")}</span>}
          </label>
        )}
        <Space>
          <Button onClick={() => void runPreview()} disabled={Object.keys(ruleErrors).length > 0}>看看接下来几次</Button>
        </Space>
        {preview && (preview.valid
          ? <Alert type="success" showIcon message={preview.description} description={(preview.upcoming ?? []).map(formatLocal).join("、")} />
          : <Alert type="error" showIcon message={preview.error ?? "规则不对"} description={preview.hint ?? undefined} />)}
      </div>
    </Drawer>
  );
}

function formFromDetail(detail: MyScheduleDetail): FormState {
  const isPrivate = isPrivateItem(detail);
  const content = "content" in detail ? detail.content : detail.instruction;
  return {
    ref: detail.ref,
    target: isPrivate ? "private" : "group",
    groupId: "groupId" in detail ? detail.groupId : "",
    content,
    title: detail.title ?? "",
    mode: detail.mode,
    maxRuns: detail.maxRuns ?? null,
    mentionMe: "mentionCreator" in detail ? detail.mentionCreator : true,
    mentionAll: "mentionAll" in detail ? Boolean(detail.mentionAll) : false,
    mentionUsers: "mentionUserIds" in detail ? (detail.mentionUserIds ?? []).join("\n") : "",
    rule: draftFromRule(detail.schedule),
    revision: "revision" in detail ? detail.revision : undefined,
  };
}

export function MySchedulesPage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [channel, setChannel] = useState<Channel>("all");
  const [managed, setManaged] = useState(false);
  const [finished, setFinished] = useState(false);
  const [form, setForm] = useState<FormState | null>(null);
  const [formKey, setFormKey] = useState(0);
  const query = useApiQuery<MySchedulesData>({
    queryKey: ["me", "schedules", channel, managed, finished],
    fetcher: (signal) => myScheduleApi.list({ channel, scope: managed ? "managed" : "mine", includeFinished: finished }, signal),
    invalidation: { resources: ["schedules"] },
  });
  const data = query.data;

  const openForm = (next: FormState) => { setForm(next); setFormKey((value) => value + 1); };
  const edit = async (item: MyScheduleItem) => {
    try {
      openForm(formFromDetail(await myScheduleApi.detail(item.ref)));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "读取失败");
    }
  };
  const act = async (item: MyScheduleItem, action: MyScheduleAction) => {
    try {
      await myScheduleApi.action(item.ref, action);
      message.success(action === "pause" ? "已暂停" : action === "resume" ? "已恢复" : "已取消");
      query.reload();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    }
  };

  const privateCount = data?.items.filter(isPrivateItem).length ?? 0;
  const groupCount = (data?.items.length ?? 0) - privateCount;
  const managesAny = data?.groups.some((group) => group.role === "owner" || group.role === "admin") ?? false;

  return <div className="me-page">
    <PageHeader
      title="我的定时任务"
      subtitle={data ? `私聊提醒 ${privateCount} 条 · 群定时任务 ${groupCount} 个；在私聊里跟 Bot 说“看看我的定时任务”也能管` : "正在读取"}
      onRefresh={query.reload}
      refreshing={query.refreshing}
      extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => openForm(blankForm())}>新建</Button>}
    />
    <div className="sbx-toolbar">
      <Segmented<Channel> value={channel} onChange={setChannel} options={[{ value: "all", label: "全部" }, { value: "private", label: "私聊提醒" }, { value: "group", label: "群定时任务" }]} />
      {managesAny && (
        <span className="sbx-switch-row me-inline-switch">
          <Switch size="small" checked={managed} onChange={setManaged} aria-label="也看我管理的群" />
          <span>也看我管理的群里别人设的</span>
        </span>
      )}
      <span className="sbx-switch-row me-inline-switch">
        <Switch size="small" checked={finished} onChange={setFinished} aria-label="显示已结束" />
        <span>显示已结束</span>
      </span>
    </div>
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {data && !data.groupReady && <Alert type="warning" showIcon message="群定时任务暂不可用" description="管理员还没完成数据库迁移；私聊提醒不受影响。" />}
    {!data && !query.error && <Skeleton active paragraph={{ rows: 4 }} />}
    {data && data.items.length === 0 && (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span>还没有定时任务。<br />点「新建」，或者私聊 Bot 说“每天晚上 9 点提醒我背单词”。</span>} />
    )}
    {data && data.items.length > 0 && (
      <ul className="me-schedule-list">
        {data.items.map((item) => <ScheduleRow key={item.ref} item={item} onEdit={(row) => void edit(row)} onAction={(row, action) => void act(row, action)} />)}
      </ul>
    )}
    <Alert className="me-safety-note" type="info" showIcon
      message="私聊提醒只有你自己能看到"
      description="私聊提醒的内容不会出现在任何群里；发到群里的任务，群里的人都能看到内容。在私聊里让 Bot 给群设任务时，它会先跟你确认一句再发。" />
    {form && <ScheduleForm key={formKey} open={!!form} form={form} groups={data?.groups ?? []} data={data ?? null} onClose={() => setForm(null)} onSaved={() => { setForm(null); query.reload(); }} />}
  </div>;
}
