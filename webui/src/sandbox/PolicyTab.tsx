import { ClearOutlined, SaveOutlined, UndoOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Checkbox, Empty, Input, InputNumber, Popconfirm, Radio, Skeleton, Switch, Table, Tag } from "antd";
import { useEffect, useMemo, useState } from "react";
import { QueryErrorAlert, SaveStatus, useApiQuery, useUnsavedChanges } from "../shared";
import { sandboxApi } from "./sandbox-api";
import {
  describeAccess,
  formatEpoch,
  INBOX_MODE_META,
  INBOX_TYPE_META,
  parseGroupList,
  SETTING_RANGES,
  settingsEqual,
  validateSettings,
} from "./sandbox-meta";
import type { SandboxInboxMode, SandboxInboxType, SandboxSettings, SandboxStatus, SandboxUsage } from "./types";

type NumericKey = keyof typeof SETTING_RANGES;

const LIMIT_SECTIONS: Array<{ title: string; description: string; keys: NumericKey[] }> = [
  {
    title: "次数与频率",
    description: "按北京时间每天零点重置；沙箱繁忙或离线导致没执行的不扣次数。",
    keys: ["memberDailyRuns", "groupDailyRuns", "memberCooldownSeconds"],
  },
  {
    title: "单次执行",
    description: "每次运行的硬上限，超出会被立即终止；runner 自身还有一层不可越过的天花板。",
    keys: ["timeoutSeconds", "memoryMb", "diskQuotaMb"],
  },
  {
    title: "回复与发送",
    description: "控制回给模型的输出长度，以及产物发到群里时的大小上限。",
    keys: ["maxOutputChars", "maxShareMb"],
  },
];

const INBOX_LIMIT_KEYS: NumericKey[] = ["inboxMaxFileMb", "inboxMaxPerTurn", "inboxHistoryMinutes", "inboxGroupDailyMb"];

function NumericField({
  field,
  draft,
  server,
  errors,
  disabled,
  onChange,
}: {
  field: NumericKey;
  draft: SandboxSettings;
  server: SandboxSettings;
  errors: Record<string, string>;
  disabled?: boolean;
  onChange: (key: NumericKey, value: number) => void;
}): React.JSX.Element {
  const range = SETTING_RANGES[field];
  const changed = draft[field] !== server[field];
  return (
    <label className="sbx-field">
      <span className="sbx-field-label">
        {range.label}
        {changed && <Tag color="gold">原 {server[field]}</Tag>}
      </span>
      <InputNumber
        min={range.min}
        max={range.max}
        value={draft[field]}
        disabled={disabled}
        status={errors[field] ? "error" : undefined}
        onChange={(value) => onChange(field, typeof value === "number" ? Math.round(value) : range.min)}
        addonAfter={range.unit}
        style={{ width: "100%" }}
      />
      {errors[field] && <span className="sbx-field-error">{errors[field]}</span>}
    </label>
  );
}

/** 「群聊附件入箱」：群友发的图片/文件/语音/视频怎样放进内置电脑。 */
export function InboxPolicyCard({
  draft,
  server,
  errors,
  runnerUploadMb,
  onChange,
}: {
  draft: SandboxSettings;
  server: SandboxSettings;
  errors: Record<string, string>;
  runnerUploadMb?: number;
  onChange: <K extends keyof SandboxSettings>(key: K, value: SandboxSettings[K]) => void;
}): React.JSX.Element {
  const off = !draft.inboxEnabled;
  const overRunner = runnerUploadMb != null && draft.inboxMaxFileMb > runnerUploadMb;
  return (
    <Card size="small" title="群聊附件入箱" className="sbx-card-wide">
      <div className="sbx-switch-row">
        <Switch checked={draft.inboxEnabled} onChange={(value) => onChange("inboxEnabled", value)} aria-label="群聊附件入箱" />
        <div>
          <strong>把群里发的附件放进内置电脑</strong>
          <span className="sbx-muted">
            图片、文件、语音、视频会放进对应群工作区的 inbox/，Agent 用 Python 或命令行直接处理。只放沙箱开放的群，退出记忆的成员的附件不会被放入。
          </span>
        </div>
      </div>
      <div className="sbx-field">
        <span className="sbx-field-label">什么时候自动放</span>
        <Radio.Group
          value={draft.inboxAutoImport}
          disabled={off}
          onChange={(event) => onChange("inboxAutoImport", event.target.value as SandboxInboxMode)}
          optionType="button"
          options={(Object.keys(INBOX_MODE_META) as SandboxInboxMode[]).map((value) => ({ value, label: INBOX_MODE_META[value].label }))}
        />
        <span className="sbx-muted">{INBOX_MODE_META[draft.inboxAutoImport].hint}</span>
      </div>
      <div className="sbx-field">
        <span className="sbx-field-label">允许的附件类型</span>
        <Checkbox.Group
          value={draft.inboxTypes}
          disabled={off}
          onChange={(values) => onChange("inboxTypes", (Object.keys(INBOX_TYPE_META) as SandboxInboxType[]).filter((type) => values.includes(type)))}
          options={(Object.keys(INBOX_TYPE_META) as SandboxInboxType[]).map((value) => ({ value, label: `${INBOX_TYPE_META[value].emoji} ${INBOX_TYPE_META[value].label}` }))}
        />
        {errors.inboxTypes && <span className="sbx-field-error">{errors.inboxTypes}</span>}
      </div>
      <div className="sbx-field-grid sbx-field-grid-4">
        {INBOX_LIMIT_KEYS.map((key) => (
          <NumericField key={key} field={key} draft={draft} server={server} errors={errors} disabled={off} onChange={(field, value) => onChange(field, value)} />
        ))}
      </div>
      {overRunner && (
        <Alert
          type="warning"
          showIcon
          message={`沙箱 runner 单个上传上限是 ${runnerUploadMb}MB，超过它的附件仍会被拒绝`}
          description="调大 runner 的 SANDBOX_MAX_UPLOAD_MB，或者把这里改小。"
        />
      )}
      {draft.inboxHistoryMinutes === 0 && !off && (
        <p className="sbx-muted">回溯时间为 0：只处理本条消息和它引用的消息里的附件。</p>
      )}
    </Card>
  );
}

/** 「私聊里的虚拟电脑」：每位好友一个独立工作区 p<QQ>，不占群配额。 */
export function PrivateComputerCard({
  draft,
  server,
  errors,
  onChange,
}: {
  draft: SandboxSettings;
  server: SandboxSettings;
  errors: Record<string, string>;
  onChange: <K extends keyof SandboxSettings>(key: K, value: SandboxSettings[K]) => void;
}): React.JSX.Element {
  const off = !draft.enabled || !draft.privateEnabled;
  return (
    <Card size="small" title="私聊里的虚拟电脑" className="sbx-card-wide">
      <div className="sbx-switch-row">
        <Switch
          checked={draft.privateEnabled}
          disabled={!draft.enabled}
          onChange={(value) => onChange("privateEnabled", value)}
          aria-label="私聊里的虚拟电脑"
        />
        <div>
          <strong>私聊 Agent 和它的后台小助手可以用虚拟电脑</strong>
          <span className="sbx-muted">
            每位好友一个独立工作区 p&lt;QQ&gt;，群里和其他人都看不到；不占群和成员的次数。产物由私聊 Agent 直接发图片或文件给本人。
            {!draft.enabled && " 上面的总开关关着，私聊也用不了。"}
          </span>
        </div>
      </div>
      <div className="sbx-switch-row">
        <Switch
          checked={draft.privateInbox}
          disabled={off || !draft.inboxEnabled}
          onChange={(value) => onChange("privateInbox", value)}
          aria-label="私聊附件入箱"
        />
        <div>
          <strong>私聊发来的附件自动放进他的电脑</strong>
          <span className="sbx-muted">图片、文件等放进他工作区的 inbox/，类型和单个大小沿用上面「群聊附件入箱」的设置。</span>
        </div>
      </div>
      <div className="sbx-field-grid">
        {(["privateDailyRuns", "privateInboxDailyMb"] as NumericKey[]).map((key) => (
          <NumericField key={key} field={key} draft={draft} server={server} errors={errors} disabled={off} onChange={(field, value) => onChange(field, value)} />
        ))}
      </div>
    </Card>
  );
}

function GroupListField({
  label,
  value,
  onChange,
  error,
  placeholder,
}: {
  label: string;
  value: string[];
  onChange: (ids: string[]) => void;
  error?: string;
  placeholder: string;
}): React.JSX.Element {
  const [text, setText] = useState(value.join("\n"));
  const [invalid, setInvalid] = useState<string[]>([]);
  useEffect(() => {
    // 服务端数据或“恢复”改变了列表时同步文本框。
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
      {(error || invalid.length > 0) && (
        <span className="sbx-field-error">{invalid.length ? `忽略了不合法的群号：${invalid.slice(0, 3).join("、")}` : error}</span>
      )}
    </label>
  );
}

function UsageTables({ usage }: { usage: SandboxUsage }): React.JSX.Element {
  return (
    <div className="sbx-usage">
      <Table
        size="small"
        rowKey="groupId"
        title={() => `今日各群用量（${usage.day ?? "—"}）`}
        dataSource={usage.groups}
        pagination={false}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="今天还没有群用过" /> }}
        columns={[
          { title: "群", dataIndex: "groupId", render: (value: string, row) => row.groupName ? `${row.groupName}（${value}）` : value },
          {
            title: "次数",
            dataIndex: "runs",
            width: 120,
            render: (value: number) => <Tag color={usage.groupLimit && value >= usage.groupLimit ? "red" : undefined}>{value} / {usage.groupLimit}</Tag>,
          },
        ]}
      />
      <Table
        size="small"
        rowKey={(row) => `${row.groupId}:${row.userId}`}
        title={() => "今日成员用量（前 50）"}
        dataSource={usage.members}
        pagination={false}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无" /> }}
        columns={[
          { title: "成员", dataIndex: "userId" },
          { title: "群", dataIndex: "groupId", render: (value: string, row) => row.groupName ?? value },
          {
            title: "次数",
            dataIndex: "runs",
            width: 120,
            render: (value: number) => <Tag color={usage.memberLimit && value >= usage.memberLimit ? "red" : undefined}>{value} / {usage.memberLimit}</Tag>,
          },
        ]}
      />
      {usage.private && (
        <Table
          size="small"
          rowKey="userId"
          title={() => "今日私聊用量（前 50）"}
          dataSource={usage.private}
          pagination={false}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="今天还没有人在私聊里用过" /> }}
          columns={[
            { title: "用户", dataIndex: "userId", render: (value: string) => `${value}（p${value}）` },
            {
              title: "次数",
              dataIndex: "runs",
              width: 120,
              render: (value: number) => <Tag color={usage.privateLimit && value >= usage.privateLimit ? "red" : undefined}>{value} / {usage.privateLimit ?? "—"}</Tag>,
            },
            { title: "入箱", dataIndex: "inboxBytes", width: 110, render: (value: number) => `${(value / 1024 / 1024).toFixed(1)} MB` },
          ]}
        />
      )}
    </div>
  );
}

export function PolicyTab({ status }: { status: SandboxStatus }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const settingsQuery = useApiQuery({
    queryKey: ["sandbox-settings"],
    fetcher: (signal) => sandboxApi.settings(signal),
    invalidation: { resources: ["sandbox"] },
  });
  const usageQuery = useApiQuery({
    queryKey: ["sandbox-usage"],
    fetcher: (signal) => sandboxApi.usage(signal),
    invalidation: { resources: ["sandbox"] },
  });
  const server = settingsQuery.data?.settings ?? null;
  const [draft, setDraft] = useState<SandboxSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [pruning, setPruning] = useState(false);

  // 没有本地修改时跟随服务器；有修改时不覆盖草稿。
  useEffect(() => {
    if (server && (!draft || settingsEqual(draft, server))) setDraft(server);
  }, [server]);

  const dirty = !!draft && !!server && !settingsEqual(draft, server);
  useUnsavedChanges(dirty);
  const errors = useMemo(() => (draft ? validateSettings(draft) : {}), [draft]);
  const hasErrors = Object.keys(errors).length > 0;

  if (settingsQuery.error && !draft) return <QueryErrorAlert error={settingsQuery.error} onRetry={settingsQuery.reload} />;
  if (!draft || !server) return <Skeleton active />;

  const update = <K extends keyof SandboxSettings>(key: K, value: SandboxSettings[K]) => setDraft({ ...draft, [key]: value });
  const save = async () => {
    setSaving(true);
    try {
      const saved = await sandboxApi.saveSettings(draft);
      setDraft(saved);
      settingsQuery.reload();
      message.success("沙箱策略已保存，立即生效");
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
          <strong>{describeAccess(draft)}</strong>
          <span className="sbx-muted">设置保存在 data/agent_sandbox/settings.json，不需要重启 Bot。</span>
        </div>
        <span className="sbx-toolbar-spacer" />
        <SaveStatus dirty={dirty} saving={saving} />
        <Button icon={<UndoOutlined />} disabled={!dirty} onClick={() => setDraft(server)}>撤销修改</Button>
        <Popconfirm title="恢复为默认策略？" description="只改草稿，点保存后才生效。" onConfirm={() => settingsQuery.data && setDraft(settingsQuery.data.defaults)}>
          <Button>恢复默认</Button>
        </Popconfirm>
        <Button type="primary" icon={<SaveOutlined />} disabled={!dirty || hasErrors} loading={saving} onClick={() => void save()}>保存</Button>
      </div>

      <div className="sbx-grid">
        <Card size="small" title="开放范围" className="sbx-card-wide">
          <div className="sbx-switch-row">
            <Switch checked={draft.enabled} onChange={(value) => update("enabled", value)} />
            <div>
              <strong>允许 Agent 运行代码</strong>
              <span className="sbx-muted">关闭后 Agent 发现不到沙箱工具，也不能查看或删除工作区文件；管理员控制台不受影响。</span>
            </div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.adminOnly} onChange={(value) => update("adminOnly", value)} />
            <div>
              <strong>仅群主和管理员可用</strong>
              <span className="sbx-muted">打开后普通成员让 Agent 跑代码会被如实拒绝。</span>
            </div>
          </div>
          <Radio.Group
            className="sbx-gap"
            value={draft.groupMode}
            onChange={(event) => update("groupMode", event.target.value as SandboxSettings["groupMode"])}
            optionType="button"
            options={[
              { value: "all", label: "所有群" },
              { value: "allowlist", label: "只开放白名单群" },
            ]}
          />
          <div className="sbx-field-grid">
            {draft.groupMode === "allowlist" && (
              <GroupListField
                label="白名单群号"
                value={draft.allowedGroups}
                onChange={(ids) => update("allowedGroups", ids)}
                error={errors.allowedGroups}
                placeholder="每行一个群号，也可以用逗号分隔"
              />
            )}
            <GroupListField
              label="禁用的群（优先于白名单）"
              value={draft.blockedGroups}
              onChange={(ids) => update("blockedGroups", ids)}
              error={errors.blockedGroups}
              placeholder="这些群永远不能用沙箱"
            />
          </div>
        </Card>

        <InboxPolicyCard draft={draft} server={server} errors={errors} runnerUploadMb={status.health?.maxUploadMb} onChange={update} />

        <PrivateComputerCard draft={draft} server={server} errors={errors} onChange={update} />

        {LIMIT_SECTIONS.map((section) => (
          <Card size="small" title={section.title} key={section.title}>
            <p className="sbx-muted">{section.description}</p>
            {section.keys.map((key) => (
              <NumericField key={key} field={key} draft={draft} server={server} errors={errors} onChange={(field, value) => update(field, value)} />
            ))}
          </Card>
        ))}

        <Card size="small" title="安全兜底">
          <div className="sbx-switch-row">
            <Switch checked={draft.allowUnisolated} onChange={(value) => update("allowUnisolated", value)} />
            <div>
              <strong>隔离自检失败时仍允许执行</strong>
              <span className="sbx-muted">只用于本地开发。生产环境打开后，群成员的代码可能联网或互相读取文件。</span>
            </div>
          </div>
          {draft.allowUnisolated && <Alert type="error" showIcon message="危险：已允许在未隔离的环境里执行群成员的代码" />}
        </Card>

        <Card size="small" title="维护">
          <p className="sbx-muted">
            runner 每小时自动清理超过 {status.health?.retentionDays ?? 14} 天没动过的工作区文件；运行历史只保留最近的若干条。
          </p>
          <Button
            icon={<ClearOutlined />}
            loading={pruning}
            disabled={!status.online}
            onClick={async () => {
              setPruning(true);
              try {
                const result = await sandboxApi.prune();
                message.success(`清理完成：删除 ${result.removed} 项（${formatEpoch(result.at)}）`);
              } catch (error) {
                message.error(error instanceof Error ? error.message : "清理失败");
              } finally {
                setPruning(false);
              }
            }}
          >立即清理</Button>
        </Card>
      </div>

      {usageQuery.data ? <UsageTables usage={usageQuery.data} /> : usageQuery.error ? <QueryErrorAlert error={usageQuery.error} onRetry={usageQuery.reload} /> : <Skeleton active />}
    </div>
  );
}
