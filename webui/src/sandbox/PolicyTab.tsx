import { ClearOutlined, SaveOutlined, UndoOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Empty, Input, InputNumber, Popconfirm, Radio, Skeleton, Switch, Table, Tag } from "antd";
import { useEffect, useMemo, useState } from "react";
import { QueryErrorAlert, SaveStatus, useApiQuery, useUnsavedChanges } from "../shared";
import { sandboxApi } from "./sandbox-api";
import {
  describeAccess,
  formatEpoch,
  parseGroupList,
  SETTING_RANGES,
  settingsEqual,
  validateSettings,
} from "./sandbox-meta";
import type { SandboxSettings, SandboxStatus, SandboxUsage } from "./types";

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

        {LIMIT_SECTIONS.map((section) => (
          <Card size="small" title={section.title} key={section.title}>
            <p className="sbx-muted">{section.description}</p>
            {section.keys.map((key) => {
              const range = SETTING_RANGES[key];
              const changed = draft[key] !== server[key];
              return (
                <label className="sbx-field" key={key}>
                  <span className="sbx-field-label">
                    {range.label}
                    {changed && <Tag color="gold">原 {server[key]}</Tag>}
                  </span>
                  <InputNumber
                    min={range.min}
                    max={range.max}
                    value={draft[key]}
                    status={errors[key] ? "error" : undefined}
                    onChange={(value) => update(key, typeof value === "number" ? Math.round(value) : range.min)}
                    addonAfter={range.unit}
                    style={{ width: "100%" }}
                  />
                  {errors[key] && <span className="sbx-field-error">{errors[key]}</span>}
                </label>
              );
            })}
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
