import { DeleteOutlined, RedoOutlined, SaveOutlined } from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useMemo, useState } from "react";
import { api, ApiError } from "./api";
import { PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import type {
  EnvironmentEntry,
  EnvironmentPatchResult,
  EnvironmentSnapshot,
  EnvironmentValueSource,
} from "./types";

const { Text } = Typography;

const MODEL_CONFIGS = [
  {
    title: "默认模型",
    description: "复杂对话、KP 与狼人杀决策",
    modelKey: "AI_MODEL",
    thinkingKey: "AI_DEFAULT_THINKING",
    multimodalKey: "AI_DEFAULT_MULTIMODAL",
  },
  {
    title: "轻量模型",
    description: "高频短文本与结构化任务；留空回退默认模型",
    modelKey: "AI_LIGHT_MODEL",
    thinkingKey: "AI_LIGHT_THINKING",
    multimodalKey: "AI_LIGHT_MULTIMODAL",
  },
  {
    title: "识图模型",
    description: "图片描述与不支持图片时的转述降级",
    modelKey: "AI_VISION_MODEL",
    thinkingKey: "AI_VISION_THINKING",
    multimodalKey: undefined,
  },
] as const;

const TASK_CONFIGS = [
  {
    plugin: "Agent",
    tasks: [
      ["普通对话 / 工具", "AGENT_DIALOGUE_LLM_PROFILE", "AGENT_DIALOGUE_THINKING"],
      ["主动发言", "AGENT_PROACTIVE_LLM_PROFILE", "AGENT_PROACTIVE_THINKING"],
      ["记忆整理", "AGENT_MEMORY_LLM_PROFILE", "AGENT_MEMORY_THINKING"],
      ["图片描述", "AGENT_IMAGE_LLM_PROFILE", "AGENT_IMAGE_THINKING"],
    ],
  },
  {
    plugin: "RPG",
    tasks: [
      ["KP 叙事 / 工具", "RPG_KP_LLM_PROFILE", "RPG_KP_THINKING"],
      ["NPC 路由", "RPG_NPC_ROUTER_LLM_PROFILE", "RPG_NPC_ROUTER_THINKING"],
      ["NPC 短对白", "RPG_NPC_LLM_PROFILE", "RPG_NPC_THINKING"],
    ],
  },
  {
    plugin: "狼人杀",
    tasks: [
      ["AI 行动决策", "WW_DECISION_LLM_PROFILE", "WW_DECISION_THINKING"],
      ["AI 短发言", "WW_SPEECH_LLM_PROFILE", "WW_SPEECH_THINKING"],
    ],
  },
] as const;

const LLM_CONFIG_KEYS = new Set<string>([
  ...MODEL_CONFIGS.flatMap((item) => [
    item.modelKey,
    item.thinkingKey,
    ...(item.multimodalKey ? [item.multimodalKey] : []),
  ]),
  ...TASK_CONFIGS.flatMap((group) =>
    group.tasks.flatMap(([, profileKey, thinkingKey]) => [profileKey, thinkingKey]),
  ),
]);

const SOURCE_META: Record<EnvironmentValueSource, { label: string; color: string }> = {
  process: { label: "进程环境覆盖", color: "red" },
  environment: { label: "环境文件覆盖", color: "orange" },
  env: { label: "根 .env", color: "green" },
  default: { label: "默认值", color: "default" },
};

const ENUM_LABELS: Record<string, string> = {
  default: "默认模型",
  light: "轻量模型",
  vision: "识图模型",
  inherit: "继承模型档位",
  auto: "自动",
  enabled: "开启推理",
  disabled: "关闭推理",
  supported: "支持图片",
  unsupported: "不支持图片",
};

function enumLabel(item: EnvironmentEntry, value: string): string {
  if (value === "auto") {
    return item.key.endsWith("_MULTIMODAL")
      ? "auto（自动探测图片能力）"
      : "auto（不发送推理参数）";
  }
  return `${value}（${ENUM_LABELS[value] ?? value}）`;
}

export function filterEnvironmentEntries(
  entries: EnvironmentEntry[],
  search: string,
): EnvironmentEntry[] {
  const needle = search.trim().toLocaleLowerCase();
  if (!needle) return entries;
  return entries.filter((item) =>
    [item.key, item.section, item.description].some((value) =>
      value.toLocaleLowerCase().includes(needle),
    ),
  );
}

export function groupEnvironmentEntries(
  entries: EnvironmentEntry[],
): Array<{ section: string; entries: EnvironmentEntry[] }> {
  const groups = new Map<string, EnvironmentEntry[]>();
  for (const item of entries) {
    const group = groups.get(item.section) ?? [];
    group.push(item);
    groups.set(item.section, group);
  }
  return [...groups].map(([section, grouped]) => ({ section, entries: grouped }));
}

export function EnvironmentPage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const load = useCallback(
    () => api<EnvironmentSnapshot>("/environment").then((response) => response.data),
    [],
  );
  const query = useApiQuery(load);
  const [search, setSearch] = useState("");
  const [changes, setChanges] = useState<Record<string, string | null>>({});
  const [saving, setSaving] = useState(false);

  const filtered = useMemo(
    () => filterEnvironmentEntries(
      (query.data?.entries ?? []).filter((item) => !LLM_CONFIG_KEYS.has(item.key)),
      search,
    ),
    [query.data?.entries, search],
  );
  const entryByKey = useMemo(
    () => new Map((query.data?.entries ?? []).map((item) => [item.key, item])),
    [query.data?.entries],
  );
  const groups = useMemo(() => groupEnvironmentEntries(filtered), [filtered]);
  const changedKeys = Object.keys(changes);

  const setValue = (key: string, value: string | null) => {
    setChanges((current) => ({ ...current, [key]: value }));
  };
  const undo = (key: string) => {
    setChanges((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  };

  const save = async () => {
    if (!query.data || changedKeys.length === 0) return;
    setSaving(true);
    try {
      const { data } = await api<EnvironmentPatchResult>("/environment", {
        method: "PATCH",
        body: JSON.stringify({
          version: query.data.version,
          changes: changedKeys.map((key) => ({ key, value: changes[key] })),
        }),
      });
      setChanges({});
      query.reload();
      if (data.restartRequired) message.success("环境配置已保存，重启 YawnBot 后生效");
      if (data.updatedKeys.includes("WEBUI_ADMIN_TOKEN")) {
        message.warning("重启后当前管理会话将失效，请使用新 Token 登录");
      }
      if (changes.WEBUI_ENABLED === "false") {
        message.warning("重启后 WebUI 将关闭");
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        setChanges({});
        query.reload();
        message.error("配置文件已变化，页面已刷新，请重新修改");
      } else {
        message.error(reason instanceof Error ? reason.message : "保存失败");
      }
    } finally {
      setSaving(false);
    }
  };

  const editor = (item: EnvironmentEntry): React.ReactNode => {
    const dirty = Object.hasOwn(changes, item.key);
    const draft = dirty ? changes[item.key] : item.value;
    if (draft === null && dirty) return <Tag color="red">保存后移除根 .env 配置</Tag>;
    const common = {
      value: draft ?? "",
      onChange: (value: string) => setValue(item.key, value),
    };
    if (item.kind === "enum") {
      return (
        <Select
          value={draft ?? undefined}
          placeholder={item.defaultValue === null ? "未配置" : `默认：${item.defaultValue}`}
          onChange={(value) => setValue(item.key, value)}
          style={{ width: "100%" }}
          options={item.options.map((value) => ({
            value,
            label: enumLabel(item, value),
          }))}
        />
      );
    }
    if (item.kind === "boolean") {
      return (
        <Select
          value={draft ?? undefined}
          placeholder={item.defaultValue === null ? "未配置" : `默认：${item.defaultValue}`}
          onChange={(value) => setValue(item.key, value)}
          style={{ width: "100%" }}
          options={[
            { value: "true", label: "true（开启）" },
            { value: "false", label: "false（关闭）" },
          ]}
        />
      );
    }
    if (item.secret) {
      return (
        <Input.Password
          value={dirty ? draft ?? "" : ""}
          placeholder={item.effectiveConfigured ? "已配置，输入新值以替换" : "未配置"}
          onChange={(event) => setValue(item.key, event.target.value)}
          visibilityToggle={false}
        />
      );
    }
    return (
      <Input
        value={common.value}
        placeholder={item.defaultValue === null ? "未配置" : `默认：${item.defaultValue}`}
        onChange={(event) => common.onChange(event.target.value)}
      />
    );
  };

  const columns: ColumnsType<EnvironmentEntry> = [
    {
      title: "配置项",
      dataIndex: "key",
      width: 260,
      render: (key: string, item) => (
        <Space orientation="vertical" size={2}>
          <Text code copyable>{key}</Text>
          {item.description && <Text type="secondary">{item.description}</Text>}
        </Space>
      ),
    },
    {
      title: "来源",
      width: 150,
      render: (_, item) => (
        <Space orientation="vertical" size={2}>
          <Tag color={SOURCE_META[item.source].color}>{SOURCE_META[item.source].label}</Tag>
          {item.secret && item.effectiveConfigured && <Text type="secondary">已配置（值已隐藏）</Text>}
          {item.overridden && <Text type="danger">根 .env 修改当前不生效</Text>}
        </Space>
      ),
    },
    { title: "根 .env 值", render: (_, item) => editor(item) },
    {
      title: "操作",
      width: 110,
      render: (_, item) => (
        <Space>
          <Button
            aria-label={`移除 ${item.key}`}
            title="移除根 .env 配置"
            icon={<DeleteOutlined />}
            disabled={!item.configured && !Object.hasOwn(changes, item.key)}
            onClick={() => setValue(item.key, null)}
          />
          <Button
            aria-label={`撤销 ${item.key}`}
            title="撤销未保存修改"
            icon={<RedoOutlined />}
            disabled={!Object.hasOwn(changes, item.key)}
            onClick={() => undo(item.key)}
          />
        </Space>
      ),
    },
  ];

  const llmEditor = (key: string, label: string): React.ReactNode => {
    const item = entryByKey.get(key);
    if (!item) return null;
    return (
      <Space orientation="vertical" size={4} style={{ width: "100%" }}>
        <Space size={4} wrap>
          <Text strong>{label}</Text>
          <Text code>{key}</Text>
          {item.overridden && <Tag color="red">当前被外部环境覆盖</Tag>}
        </Space>
        {editor(item)}
      </Space>
    );
  };

  return (
    <Space orientation="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="环境配置"
        subtitle="集中修改根 .env；全部变更仅在重启 YawnBot 后生效"
        extra={(
          <Button
            type="primary"
            icon={<SaveOutlined />}
            disabled={changedKeys.length === 0}
            loading={saving}
            onClick={() => void save()}
          >
            保存 {changedKeys.length > 0 ? `${changedKeys.length} 项` : ""}
          </Button>
        )}
      />
      <Alert
        type="warning"
        showIcon
        title="敏感配置只允许替换，不会从服务端回显"
        description={`当前环境：${query.data?.environment ?? "—"}。进程环境变量或 ${query.data?.environmentFile ?? ".env.<ENVIRONMENT>"} 的值优先于根 .env。`}
      />
      <Card title="LLM 模型档位" extra={<Text type="secondary">保存后重启生效</Text>}>
        <Row gutter={[16, 16]}>
          {MODEL_CONFIGS.map((model) => (
            <Col xs={24} xl={8} key={model.modelKey}>
              <Card size="small" title={model.title}>
                <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
                  <Text type="secondary">{model.description}</Text>
                  {llmEditor(model.modelKey, "模型 ID")}
                  {llmEditor(model.thinkingKey, "全局推理")}
                  {model.multimodalKey && llmEditor(model.multimodalKey, "图片能力")}
                </Space>
              </Card>
            </Col>
          ))}
        </Row>
      </Card>
      <Card title="子插件任务路由" extra={<Text type="secondary">任务推理选择 inherit 时继承模型档位</Text>}>
        <Row gutter={[16, 16]}>
          {TASK_CONFIGS.map((group) => (
            <Col xs={24} xl={8} key={group.plugin}>
              <Card size="small" title={group.plugin}>
                <Space orientation="vertical" size="large" style={{ width: "100%" }}>
                  {group.tasks.map(([label, profileKey, thinkingKey]) => (
                    <Card size="small" key={profileKey} title={label}>
                      <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
                        {llmEditor(profileKey, "模型档位")}
                        {llmEditor(thinkingKey, "推理覆盖")}
                      </Space>
                    </Card>
                  ))}
                </Space>
              </Card>
            </Col>
          ))}
        </Row>
      </Card>
      <Input.Search
        allowClear
        placeholder="搜索配置名、分组或说明"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
      />
      {query.error && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
      {!query.loading && groups.length === 0 && <Empty description="没有匹配的配置项" />}
      {groups.map((group) => (
        <Card key={group.section} title={group.section} size="small">
          <Table
            rowKey="key"
            loading={query.loading}
            columns={columns}
            dataSource={group.entries}
            pagination={false}
            scroll={{ x: 900 }}
          />
        </Card>
      ))}
    </Space>
  );
}
