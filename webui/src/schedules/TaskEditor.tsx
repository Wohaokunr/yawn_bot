import { ThunderboltOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Checkbox, Collapse, Drawer, Input, InputNumber, Radio, Segmented, Space, Switch, Tag } from "antd";
import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../api";
import { scheduleApi } from "./schedule-api";
import {
  draftFromRule,
  emptyRuleDraft,
  formatEvery,
  formatLocal,
  KIND_META,
  MODE_META,
  MONTH_LABELS,
  parseClockList,
  parseDateList,
  ruleFromDraft,
  RULE_PRESETS,
  sameRule,
  validateRuleDraft,
  WEEKDAY_LABELS,
} from "./schedule-meta";
import type { RuleDraft } from "./schedule-meta";
import type { ScheduleKind, ScheduleMode, SchedulePreview, ScheduleTaskDetail } from "./types";

const PREVIEW_DEBOUNCE_MS = 400;
const GROUP_ID_RE = /^\d{4,12}$/;

interface FormState {
  groupId: string;
  creatorUserId: string;
  title: string;
  instruction: string;
  mode: ScheduleMode;
  mentionCreator: boolean;
  maxRuns: number | null;
  rule: RuleDraft;
}

function initialForm(task: ScheduleTaskDetail | null, defaults: { groupId?: string }): FormState {
  if (task) {
    return {
      groupId: task.groupId,
      creatorUserId: task.creatorUserId,
      title: task.title,
      instruction: task.instruction,
      mode: task.mode,
      mentionCreator: task.mentionCreator,
      maxRuns: task.maxRuns,
      rule: draftFromRule(task.schedule),
    };
  }
  return {
    groupId: defaults.groupId ?? "",
    creatorUserId: "",
    title: "",
    instruction: "",
    mode: "notify",
    mentionCreator: true,
    maxRuns: null,
    rule: emptyRuleDraft("daily"),
  };
}

/** 钟点/日期这类“文本 → 列表”的输入：本地保留原文，解析结果回写表单。 */
function ListInput({
  value,
  parse,
  onChange,
  placeholder,
  ariaLabel,
}: {
  value: string[];
  parse: (text: string) => { items: string[]; invalid: string[] };
  onChange: (items: string[]) => void;
  placeholder: string;
  ariaLabel: string;
}): React.JSX.Element {
  const [text, setText] = useState(value.join(" "));
  const [invalid, setInvalid] = useState<string[]>([]);
  useEffect(() => {
    if (parse(text).items.join(",") !== value.join(",")) setText(value.join(" "));
  }, [value]);
  return (
    <>
      <Input
        value={text}
        placeholder={placeholder}
        aria-label={ariaLabel}
        status={invalid.length ? "error" : undefined}
        onChange={(event) => {
          setText(event.target.value);
          const parsed = parse(event.target.value);
          setInvalid(parsed.invalid);
          onChange(parsed.items);
        }}
      />
      {invalid.length > 0 && <span className="sbx-field-error">看不懂：{invalid.slice(0, 3).join("、")}</span>}
    </>
  );
}

const parseClocks = (text: string) => {
  const { times, invalid } = parseClockList(text);
  return { items: times, invalid };
};
const parseDates = (text: string) => {
  const { dates, invalid } = parseDateList(text);
  return { items: dates, invalid };
};

function Field({ label, error, hint, children }: { label: string; error?: string; hint?: string; children: React.ReactNode }): React.JSX.Element {
  return (
    <div className="sbx-field">
      <span className="sbx-field-label">{label}</span>
      {children}
      {error ? <span className="sbx-field-error">{error}</span> : hint ? <span className="sbx-muted">{hint}</span> : null}
    </div>
  );
}

export function RuleEditor({
  draft,
  errors,
  onChange,
}: {
  draft: RuleDraft;
  errors: Record<string, string>;
  onChange: (next: RuleDraft) => void;
}): React.JSX.Element {
  const set = <K extends keyof RuleDraft>(key: K, value: RuleDraft[K]) => onChange({ ...draft, [key]: value });
  const usesTimes = draft.kind === "daily" || draft.kind === "weekly" || draft.kind === "monthly";
  return (
    <div className="sch-rule">
      <div className="sch-presets">
        <span className="sbx-muted">快速填：</span>
        {RULE_PRESETS.map((preset) => (
          <Tag.CheckableTag key={preset.key} checked={false} onChange={() => onChange(preset.apply(draft))}>{preset.label}</Tag.CheckableTag>
        ))}
      </div>
      <Segmented
        block
        value={draft.kind}
        onChange={(value) => set("kind", value as ScheduleKind)}
        options={(Object.keys(KIND_META) as ScheduleKind[]).map((kind) => ({ value: kind, label: KIND_META[kind].label }))}
      />
      <p className="sbx-muted sch-kind-hint">{KIND_META[draft.kind].hint}</p>

      {draft.kind === "once" && (
        <>
          <Radio.Group value={draft.onceMode} onChange={(event) => set("onceMode", event.target.value as RuleDraft["onceMode"])} optionType="button"
            options={[{ value: "delay", label: "多久以后" }, { value: "at", label: "指定时间" }]} />
          {draft.onceMode === "delay" ? (
            <Field label="几分钟后" error={errors.delayMinutes}>
              <InputNumber min={1} max={525600} value={draft.delayMinutes} onChange={(value) => set("delayMinutes", typeof value === "number" ? value : null)} addonAfter="分钟" aria-label="几分钟后" />
            </Field>
          ) : (
            <Field label="执行时间（北京时间）" error={errors.at}>
              <Input value={draft.at} placeholder="2026-12-31 20:00" onChange={(event) => set("at", event.target.value)} aria-label="执行时间" />
            </Field>
          )}
        </>
      )}

      {draft.kind === "interval" && (
        <>
          <Field label="每隔" error={errors.everyMinutes} hint={draft.everyMinutes ? `即每 ${formatEvery(draft.everyMinutes)}一次` : undefined}>
            <InputNumber min={1} max={44640} value={draft.everyMinutes} onChange={(value) => set("everyMinutes", typeof value === "number" ? value : null)} addonAfter="分钟" aria-label="间隔分钟" />
          </Field>
          <div className="sbx-switch-row">
            <Switch checked={draft.windowEnabled} onChange={(value) => set("windowEnabled", value)} aria-label="限定时间窗口" />
            <div>
              <strong>只在每天的某个时段内触发</strong>
              <span className="sbx-muted">例如 09:00–22:00；跨午夜（22:00–02:00）也可以</span>
            </div>
          </div>
          {draft.windowEnabled && (
            <Space>
              <Input value={draft.windowStart} onChange={(event) => set("windowStart", event.target.value)} style={{ width: 96 }} aria-label="窗口开始" />
              <span>到</span>
              <Input value={draft.windowEnd} onChange={(event) => set("windowEnd", event.target.value)} style={{ width: 96 }} aria-label="窗口结束" />
            </Space>
          )}
          {errors.window && <span className="sbx-field-error">{errors.window}</span>}
        </>
      )}

      {draft.kind === "weekly" && (
        <Field label="星期" error={errors.weekdays}>
          <Checkbox.Group
            value={draft.weekdays}
            onChange={(values) => set("weekdays", (values as number[]).slice().sort((a, b) => a - b))}
            options={WEEKDAY_LABELS.map((label, index) => ({ value: index + 1, label }))}
          />
        </Field>
      )}

      {draft.kind === "monthly" && (
        <Field label="每月几号" error={errors.monthDays} hint="填 -1 表示每月最后一天；没有 31 号的月份会跳过 31">
          <ListInput
            value={draft.monthDays.map(String)}
            ariaLabel="每月几号"
            placeholder="1 15 -1"
            parse={(text) => {
              const items: string[] = [];
              const invalid: string[] = [];
              for (const raw of text.split(/[\s,，、]+/)) {
                if (!raw) continue;
                const value = Number(raw);
                if (!Number.isInteger(value) || value === 0 || value < -1 || value > 31) invalid.push(raw);
                else if (!items.includes(String(value))) items.push(String(value));
              }
              return { items, invalid };
            }}
            onChange={(items) => set("monthDays", items.map(Number))}
          />
        </Field>
      )}

      {usesTimes && (
        <Field label="钟点" error={errors.times} hint="可以填多个，用空格隔开，例如 08:00 12:30 21:00">
          <ListInput value={draft.times} parse={parseClocks} onChange={(items) => set("times", items)} placeholder="09:00" ariaLabel="钟点" />
        </Field>
      )}

      {draft.kind === "cron" && (
        <Field label="cron 表达式" error={errors.cron} hint="分 时 日 月 周，例如 0 9 * * 1-5（工作日 9 点）、*/30 9-18 * * *；也支持 @daily 等别名">
          <Input value={draft.cron} onChange={(event) => set("cron", event.target.value)} className="sch-mono" aria-label="cron 表达式" />
        </Field>
      )}

      {draft.kind !== "once" && (
        <Collapse
          size="small"
          ghost
          items={[
            {
              key: "advanced",
              label: "生效范围、限定月份与跳过日期",
              children: (
                <>
                  <div className="sbx-field-grid">
                    <Field label="从什么时候开始" error={errors.startAt}>
                      <Input value={draft.startAt} placeholder="立即" onChange={(event) => set("startAt", event.target.value)} aria-label="开始时间" />
                    </Field>
                    <Field label="到什么时候结束" error={errors.endAt}>
                      <Input value={draft.endAt} placeholder="不限" onChange={(event) => set("endAt", event.target.value)} aria-label="结束时间" />
                    </Field>
                  </div>
                  {usesTimes && (
                    <Field label="只在这些月份">
                      <Checkbox.Group
                        value={draft.months}
                        onChange={(values) => set("months", (values as number[]).slice().sort((a, b) => a - b))}
                        options={MONTH_LABELS.map((label, index) => ({ value: index + 1, label }))}
                      />
                    </Field>
                  )}
                  <Field label="跳过这些日期" error={errors.skipDates} hint="节假日等，写成 YYYY-MM-DD，空格隔开">
                    <ListInput value={draft.skipDates} parse={parseDates} onChange={(items) => set("skipDates", items)} placeholder="2026-10-01 2026-10-02" ariaLabel="跳过日期" />
                  </Field>
                </>
              ),
            },
          ]}
        />
      )}
    </div>
  );
}

function PreviewPanel({ preview, loading }: { preview: SchedulePreview | null; loading: boolean }): React.JSX.Element {
  if (!preview) return <div className="sch-preview is-idle">{loading ? "正在计算…" : "填好规则后这里会显示接下来的触发时间"}</div>;
  if (!preview.valid) {
    return (
      <Alert type="warning" showIcon message={preview.error} description={preview.hint ?? undefined} className="sch-preview-alert" />
    );
  }
  return (
    <div className="sch-preview" aria-live="polite">
      <strong>{preview.description}</strong>
      {preview.upcoming?.length ? (
        <ol>
          {preview.upcoming.map((item) => <li key={item}>{formatLocal(item)}</li>)}
        </ol>
      ) : (
        <span className="sbx-muted">按这个规则之后不会再触发</span>
      )}
      <span className="sbx-muted">按北京时间计算</span>
    </div>
  );
}

export function TaskEditor({
  open,
  task,
  defaultGroupId,
  agentModeAllowed,
  onClose,
  onSaved,
}: {
  open: boolean;
  task: ScheduleTaskDetail | null;
  defaultGroupId?: string;
  agentModeAllowed: boolean;
  onClose: () => void;
  onSaved: (task: ScheduleTaskDetail) => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [form, setForm] = useState<FormState>(() => initialForm(task, { groupId: defaultGroupId }));
  const [preview, setPreview] = useState<SchedulePreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState("");

  useEffect(() => {
    if (open) {
      setForm(initialForm(task, { groupId: defaultGroupId }));
      setServerError("");
      setPreview(null);
    }
  }, [open, task, defaultGroupId]);

  const ruleErrors = useMemo(() => validateRuleDraft(form.rule), [form.rule]);
  const rule = useMemo(() => ruleFromDraft(form.rule), [form.rule]);
  const ruleKey = JSON.stringify(rule);

  // 规则变了就去后端算一次预览（防抖），保证页面上看到的和调度器算的是同一套。
  useEffect(() => {
    if (!open || Object.keys(ruleErrors).length) {
      setPreview(null);
      return undefined;
    }
    const controller = new AbortController();
    setPreviewing(true);
    const timer = window.setTimeout(() => {
      scheduleApi.preview(rule, form.maxRuns, form.mode, controller.signal)
        .then(setPreview)
        .catch((error: unknown) => {
          if (!controller.signal.aborted) setPreview({ valid: false, error: error instanceof Error ? error.message : "预览失败" });
        })
        .finally(() => {
          if (!controller.signal.aborted) setPreviewing(false);
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [open, ruleKey, form.maxRuns, form.mode]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((current) => ({ ...current, [key]: value }));
  const editing = task !== null;
  const errors: Record<string, string> = { ...ruleErrors };
  if (!editing && !GROUP_ID_RE.test(form.groupId)) errors.groupId = "填群号（4–12 位数字）";
  if (!editing && !/^\d{5,12}$/.test(form.creatorUserId)) errors.creatorUserId = "填该群一位成员的 QQ 号";
  if (!form.instruction.trim()) errors.instruction = "写清楚到点要做什么";
  if (form.mode === "agent" && !agentModeAllowed && !(editing && task.mode === "agent")) errors.mode = "策略里关闭了“唤醒 Agent”，只能用直接提醒";
  const blocked = Object.keys(errors).length > 0 || (preview !== null && !preview.valid);

  const save = async () => {
    setSaving(true);
    setServerError("");
    try {
      const input = {
        title: form.title.trim(),
        instruction: form.instruction.trim(),
        mode: form.mode,
        schedule: rule,
        maxRuns: form.maxRuns,
        mentionCreator: form.mentionCreator,
      };
      const saved = editing
        ? await scheduleApi.update(task.id, { ...input, revision: task.revision })
        : await scheduleApi.create({ ...input, groupId: form.groupId, creatorUserId: form.creatorUserId });
      message.success(editing ? "已保存" : "已创建定时任务");
      onSaved(saved);
    } catch (error) {
      setServerError(error instanceof ApiError || error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width="min(820px, 100%)"
      title={editing ? `编辑定时任务 #${task.id}` : "新建定时任务"}
      className="sch-editor"
      destroyOnHidden
      extra={
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" loading={saving} disabled={blocked} onClick={() => void save()}>{editing ? "保存" : "创建"}</Button>
        </Space>
      }
    >
      <div className="sch-editor-layout">
        <div className="sch-editor-main">
          {serverError && <Alert type="error" showIcon message={serverError} className="sbx-gap" />}
          {editing ? (
            <p className="sbx-muted">群 {task.groupName ?? task.groupId} · 以 {task.creatorName ?? task.creatorUserId}（{task.creatorUserId}）的名义</p>
          ) : (
            <div className="sbx-field-grid">
              <Field label="群号" error={errors.groupId}>
                <Input value={form.groupId} onChange={(event) => set("groupId", event.target.value.trim())} aria-label="群号" />
              </Field>
              <Field label="以谁的名义" error={errors.creatorUserId} hint="Agent 会把这位成员当作“叫它的人”，@提醒也发给他">
                <Input value={form.creatorUserId} onChange={(event) => set("creatorUserId", event.target.value.trim())} aria-label="创建者 QQ" />
              </Field>
            </div>
          )}
          <Field label="执行方式" error={errors.mode} hint={MODE_META[form.mode].hint}>
            <Radio.Group
              value={form.mode}
              onChange={(event) => set("mode", event.target.value as ScheduleMode)}
              optionType="button"
              options={(Object.keys(MODE_META) as ScheduleMode[]).map((value) => ({ value, label: MODE_META[value].label }))}
            />
          </Field>
          <Field label={form.mode === "agent" ? "到点让 Agent 做什么" : "提醒内容"} error={errors.instruction}>
            <Input.TextArea
              value={form.instruction}
              maxLength={600}
              showCount
              autoSize={{ minRows: 3, maxRows: 8 }}
              placeholder={form.mode === "agent" ? "例如：查一下今天的天气，提醒大家要不要带伞" : "例如：该喝水啦"}
              onChange={(event) => set("instruction", event.target.value)}
              aria-label="指令"
            />
          </Field>
          <div className="sbx-field-grid">
            <Field label="标题（可选）" hint="留空会从内容自动生成">
              <Input value={form.title} maxLength={40} onChange={(event) => set("title", event.target.value)} aria-label="标题" />
            </Field>
            <Field label="最多执行几次" hint="留空不限；单次任务固定 1 次">
              <InputNumber
                min={1}
                max={10000}
                value={form.maxRuns}
                disabled={form.rule.kind === "once"}
                onChange={(value) => set("maxRuns", typeof value === "number" ? value : null)}
                style={{ width: "100%" }}
                aria-label="最多执行几次"
              />
            </Field>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={form.mentionCreator} onChange={(value) => set("mentionCreator", value)} aria-label="@创建者" />
            <div>
              <strong>到点 @ 创建者</strong>
              <span className="sbx-muted">关掉后只发到群里，不单独提醒人</span>
            </div>
          </div>
          <h4 className="sbx-subhead">什么时候执行</h4>
          <RuleEditor draft={form.rule} errors={ruleErrors} onChange={(next) => set("rule", next)} />
        </div>
        <aside className="sch-editor-side">
          <h4 className="sbx-subhead"><ThunderboltOutlined /> 接下来会在</h4>
          <PreviewPanel preview={preview} loading={previewing} />
          {editing && !sameRule(task.schedule, rule) && (
            <Alert type="info" showIcon message="改了时间规则" description="保存后会从现在重新开始计算，已执行次数清零。" />
          )}
        </aside>
      </div>
    </Drawer>
  );
}
