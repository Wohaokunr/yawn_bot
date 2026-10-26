import { SaveOutlined, UndoOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Input, InputNumber, Radio, Skeleton, Switch, Tag } from "antd";
import { useEffect, useMemo, useState } from "react";
import { QueryErrorAlert, SaveStatus, useApiQuery, useUnsavedChanges } from "../shared";
import { parseGroupList } from "../sandbox/sandbox-meta";
import { scheduleApi } from "./schedule-api";
import { describeScheduleAccess, SETTING_RANGES, validateScheduleSettings } from "./schedule-meta";
import type { ScheduleSettings } from "./types";

type NumericKey = keyof typeof SETTING_RANGES;

const SECTIONS: Array<{ title: string; description: string; keys: NumericKey[] }> = [
  {
    title: "数量与频率",
    description: "防止一个群或一个人把 Bot 设成闹钟轰炸机；管理台手动建的任务不受数量上限约束。",
    keys: ["maxActivePerGroup", "maxActivePerMember", "minIntervalMinutes", "maxHorizonDays", "groupDailyRuns"],
  },
  {
    title: "错过与失败",
    description: "Bot 重启或繁忙导致到点没执行时怎么办；失败会按 2/5/15 分钟退避重试。",
    keys: ["misfireGraceMinutes", "catchUpMaxHours", "maxConsecutiveFailures"],
  },
  {
    title: "执行",
    description: "调度器每 20 秒检查一次；同一个群的任务总是排队串行执行。",
    keys: ["concurrency", "runTimeoutSeconds", "historyLimit"],
  },
];

function GroupListField({ label, value, error, placeholder, onChange }: { label: string; value: string[]; error?: string; placeholder: string; onChange: (ids: string[]) => void }): React.JSX.Element {
  const [text, setText] = useState(value.join("\n"));
  const [invalid, setInvalid] = useState<string[]>([]);
  useEffect(() => {
    if (parseGroupList(text).ids.join(",") !== value.join(",")) setText(value.join("\n"));
  }, [value]);
  return (
    <label className="sbx-field">
      <span className="sbx-field-label">{label}</span>
      <Input.TextArea
        value={text}
        autoSize={{ minRows: 2, maxRows: 6 }}
        placeholder={placeholder}
        status={error || invalid.length ? "error" : undefined}
        onChange={(event) => {
          setText(event.target.value);
          const parsed = parseGroupList(event.target.value);
          setInvalid(parsed.invalid);
          onChange(parsed.ids);
        }}
      />
      {(error || invalid.length > 0) && <span className="sbx-field-error">{invalid.length ? `忽略了不合法的群号：${invalid.slice(0, 3).join("、")}` : error}</span>}
    </label>
  );
}

export function PolicyTab(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<{ settings: ScheduleSettings; defaults: ScheduleSettings }>({
    queryKey: ["schedule-settings"],
    fetcher: (signal) => scheduleApi.settings(signal),
    invalidation: { resources: ["schedules"] },
  });
  const server = query.data?.settings ?? null;
  const [draft, setDraft] = useState<ScheduleSettings | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    // 服务端更新时，只在没有未保存修改的情况下同步草稿。
    if (server && (draft === null || JSON.stringify(draft) === JSON.stringify(server))) setDraft(server);
  }, [server]);

  const dirty = !!server && !!draft && JSON.stringify(draft) !== JSON.stringify(server);
  useUnsavedChanges(dirty);
  const errors = useMemo(() => (draft ? validateScheduleSettings(draft) : {}), [draft]);

  if (!draft || !server) {
    return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Skeleton active />;
  }
  const set = <K extends keyof ScheduleSettings>(key: K, value: ScheduleSettings[K]) => setDraft((current) => (current ? { ...current, [key]: value } : current));

  const save = async () => {
    setSaving(true);
    try {
      const saved = await scheduleApi.saveSettings(draft);
      setDraft(saved);
      message.success("策略已保存，下一次调度立即生效");
      query.reload();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="sbx-policy">
      <div className="sbx-policy-summary">
        <div>
          <strong>{describeScheduleAccess(draft)}</strong>
          <span className="sbx-muted">定时任务唤醒的回合里，Agent 不能再创建定时任务，也不能用管理/高危工具。</span>
        </div>
        <span className="sbx-toolbar-spacer" />
        <SaveStatus dirty={dirty} saving={saving} />
        <Button icon={<UndoOutlined />} disabled={!dirty} onClick={() => setDraft(server)}>撤销</Button>
        <Button icon={<UndoOutlined />} onClick={() => query.data && setDraft({ ...query.data.defaults })}>恢复默认</Button>
        <Button type="primary" icon={<SaveOutlined />} disabled={!dirty || Object.keys(errors).length > 0} loading={saving} onClick={() => void save()}>保存</Button>
      </div>

      <Card size="small" title="开放范围">
        <div className="sbx-switch-row">
          <Switch checked={draft.enabled} onChange={(value) => set("enabled", value)} aria-label="启用定时任务" />
          <div>
            <strong>允许 Agent 创建和执行定时任务</strong>
            <span className="sbx-muted">关掉后已有任务不会被删除，到点会记为“未开放跳过”。</span>
          </div>
        </div>
        <div className="sbx-field">
          <span className="sbx-field-label">哪些群</span>
          <Radio.Group value={draft.groupMode} onChange={(event) => set("groupMode", event.target.value as ScheduleSettings["groupMode"])} optionType="button"
            options={[{ value: "all", label: "所有开启 Agent 的群" }, { value: "allowlist", label: "只有白名单里的群" }]} />
        </div>
        <div className="sbx-field-grid">
          {draft.groupMode === "allowlist" && (
            <GroupListField label="白名单群" value={draft.allowedGroups} error={errors.allowedGroups} placeholder="每行一个群号" onChange={(ids) => set("allowedGroups", ids)} />
          )}
          <GroupListField label="禁用的群" value={draft.blockedGroups} error={errors.blockedGroups} placeholder="这些群总是不能用" onChange={(ids) => set("blockedGroups", ids)} />
        </div>
        <div className="sbx-field">
          <span className="sbx-field-label">谁能让 Bot 设定时任务</span>
          <Radio.Group value={draft.whoCanCreate} onChange={(event) => set("whoCanCreate", event.target.value as ScheduleSettings["whoCanCreate"])} optionType="button"
            options={[{ value: "members", label: "所有群成员" }, { value: "admins", label: "仅群主/管理员" }]} />
          <span className="sbx-muted">成员总能查看本群任务、取消自己设的任务。</span>
        </div>
        <div className="sbx-switch-row">
          <Switch checked={draft.allowAgentMode} onChange={(value) => set("allowAgentMode", value)} aria-label="允许唤醒 Agent" />
          <div>
            <strong>允许“唤醒 Agent”方式</strong>
            <span className="sbx-muted">到点让 Agent 思考并可调用工具（会消耗模型额度）。关掉后只能建“直接提醒”。</span>
          </div>
        </div>
      </Card>

      {SECTIONS.map((section) => (
        <Card key={section.title} size="small" title={section.title}>
          <p className="sbx-muted">{section.description}</p>
          <div className="sbx-field-grid sch-field-grid-3">
            {section.keys.map((key) => {
              const range = SETTING_RANGES[key];
              return (
                <label className="sbx-field" key={key}>
                  <span className="sbx-field-label">{range.label}{draft[key] !== server[key] && <Tag color="gold">原 {server[key]}</Tag>}</span>
                  <InputNumber min={range.min} max={range.max} value={draft[key]} status={errors[key] ? "error" : undefined}
                    onChange={(value) => set(key, typeof value === "number" ? Math.round(value) : range.min)} addonAfter={range.unit} style={{ width: "100%" }} aria-label={range.label} />
                  {errors[key] && <span className="sbx-field-error">{errors[key]}</span>}
                </label>
              );
            })}
          </div>
          {section.title === "错过与失败" && (
            <div className="sbx-field">
              <span className="sbx-field-label">超过宽限还没执行的</span>
              <Radio.Group value={draft.missedPolicy} onChange={(event) => set("missedPolicy", event.target.value as ScheduleSettings["missedPolicy"])} optionType="button"
                options={[{ value: "run_once", label: "补跑一次" }, { value: "skip", label: "直接跳过" }]} />
              <span className="sbx-muted">多次错过也只补一次；超过“最多补跑多久以前的”就不补了，免得半夜补一堆早安。</span>
            </div>
          )}
        </Card>
      ))}

      <Card size="small" title="免打扰与提醒格式">
        <div className="sbx-switch-row">
          <Switch checked={draft.quietHoursEnabled} onChange={(value) => set("quietHoursEnabled", value)} aria-label="免打扰时段" />
          <div>
            <strong>免打扰时段</strong>
            <span className="sbx-muted">这段时间里到点的任务按下面的方式处理；可以跨午夜。</span>
          </div>
        </div>
        <div className="sbx-field-grid sch-field-grid-3">
          <label className="sbx-field">
            <span className="sbx-field-label">开始</span>
            <Input value={draft.quietStart} disabled={!draft.quietHoursEnabled} status={errors.quietStart ? "error" : undefined} onChange={(event) => set("quietStart", event.target.value.trim())} aria-label="免打扰开始" />
            {errors.quietStart && <span className="sbx-field-error">{errors.quietStart}</span>}
          </label>
          <label className="sbx-field">
            <span className="sbx-field-label">结束</span>
            <Input value={draft.quietEnd} disabled={!draft.quietHoursEnabled} status={errors.quietEnd ? "error" : undefined} onChange={(event) => set("quietEnd", event.target.value.trim())} aria-label="免打扰结束" />
            {errors.quietEnd && <span className="sbx-field-error">{errors.quietEnd}</span>}
          </label>
          <div className="sbx-field">
            <span className="sbx-field-label">落在时段内的</span>
            <Radio.Group value={draft.quietPolicy} disabled={!draft.quietHoursEnabled} onChange={(event) => set("quietPolicy", event.target.value as ScheduleSettings["quietPolicy"])} optionType="button"
              options={[{ value: "defer", label: "推迟到结束" }, { value: "skip", label: "跳过这次" }]} />
          </div>
        </div>
        <label className="sbx-field">
          <span className="sbx-field-label">直接提醒的前缀</span>
          <Input value={draft.notifyPrefix} maxLength={8} status={errors.notifyPrefix ? "error" : undefined} onChange={(event) => set("notifyPrefix", event.target.value)} style={{ maxWidth: 200 }} aria-label="提醒前缀" />
          <span className="sbx-muted">例如“⏰ ”，留空就只发提醒内容</span>
        </label>
        {draft.quietHoursEnabled && draft.quietPolicy === "defer" && (
          <Alert type="info" showIcon message="推迟的任务会在免打扰结束时集中执行，同一个群按顺序来。" />
        )}
      </Card>
    </div>
  );
}
