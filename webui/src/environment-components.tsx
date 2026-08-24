import { DeleteOutlined, ExperimentOutlined, SaveOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  Modal,
  Popconfirm,
  Row,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { EnvironmentEntry, LLMProviderSnapshot } from "./types";

const { Text } = Typography;

export interface LLMProviderDraft extends LLMProviderSnapshot {
  draftKey: string;
  apiKey?: string | null;
  isNew?: boolean;
}

export interface ModelTestState {
  ok: boolean;
  message: string;
  latencyMs?: number;
  testedAt: string;
}

export interface EnvironmentDiffRow {
  key: string;
  area: string;
  before: string;
  after: string;
  secret?: boolean;
  restartRequired: boolean;
}

export function ProviderEditor({
  provider,
  usage,
  onUpdate,
  onDelete,
}: {
  provider: LLMProviderDraft;
  usage: string[];
  onUpdate: (patch: Partial<LLMProviderDraft>) => void;
  onDelete: () => void;
}): React.JSX.Element {
  const replacement = typeof provider.apiKey === "string" && provider.apiKey.length > 0;
  const removingKey = provider.apiKey === null;
  return <Card
    size="small"
    title={(
      <Space size={8} wrap>
        <span className="env-badge tone-sky" aria-hidden>🔌</span>
        {provider.id || "未命名提供商"}
        {provider.builtIn && <Tag>内置</Tag>}
        {provider.overridden && <Tag color="red">外部环境覆盖</Tag>}
        <Tag color="orange">需重启</Tag>
      </Space>
    )}
    extra={!provider.builtIn && (
      <Popconfirm
        title="删除这个提供商？"
        description="若模型档位仍在使用它，需要先切换档位。"
        onConfirm={onDelete}
      >
        <Button danger type="text" icon={<DeleteOutlined />} aria-label={`删除提供商 ${provider.id}`} />
      </Popconfirm>
    )}
  >
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Text strong>提供商 ID</Text>
        <Input
          value={provider.id}
          disabled={!provider.isNew}
          onChange={(event) => onUpdate({ id: event.target.value })}
          placeholder="例如 fast"
        />
      </div>
      <div>
        <Text strong>Base URL</Text>
        <Input
          value={provider.baseUrl}
          onChange={(event) => onUpdate({ baseUrl: event.target.value })}
          placeholder="https://example.com/v1"
        />
      </div>
      <div>
        <Space size={6} wrap>
          <Text strong>API Key</Text>
          <Tag color={removingKey ? "red" : replacement ? "orange" : provider.apiKeyConfigured ? "green" : "default"}>
            {removingKey ? "待移除" : replacement ? "待替换" : provider.apiKeyConfigured ? "已配置" : "未配置"}
          </Tag>
        </Space>
        <Input.Password
          value={replacement ? provider.apiKey ?? "" : ""}
          visibilityToggle={false}
          autoComplete="new-password"
          placeholder={provider.apiKeyConfigured ? "已配置，输入新值以替换" : "输入 API Key"}
          onChange={(event) => onUpdate({ apiKey: event.target.value || undefined })}
        />
      </div>
      {provider.builtIn && provider.apiKeyConfigured && (
        <Popconfirm title="移除 default 提供商的 API Key？" onConfirm={() => onUpdate({ apiKey: null })}>
          <Button danger size="small">移除密钥</Button>
        </Popconfirm>
      )}
      <div>
        <Text type="secondary">当前使用：</Text>{" "}
        {usage.length > 0 ? usage.map((item) => <Tag key={item}>{item}</Tag>) : <Tag>未被模型/任务使用</Tag>}
      </div>
      {provider.overridden && (
        <Text type="danger">保存只修改根 .env，重启后仍会被进程环境或环境文件覆盖。</Text>
      )}
    </Space>
  </Card>;
}

export function ModelProfileCard({
  title,
  description,
  icon,
  tone,
  editors,
  resolvedRoute,
  fallback,
  testing,
  testResult,
  onTest,
}: {
  title: string;
  description: string;
  icon: string;
  tone: string;
  editors: React.ReactNode[];
  resolvedRoute: string;
  fallback: boolean;
  testing: boolean;
  testResult?: ModelTestState;
  onTest: () => void;
}): React.JSX.Element {
  return <Card
    size="small"
    title={<Space size={8}><span className={`env-badge ${tone}`} aria-hidden>{icon}</span>{title}<Tag color="orange">需重启</Tag></Space>}
  >
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <Text type="secondary">{description}</Text>
      {editors.map((node, index) => <div key={index}>{node}</div>)}
      <Text type="secondary">实际路由：{resolvedRoute}{fallback ? "（继承默认档位）" : ""}</Text>
      <Button icon={<ExperimentOutlined />} loading={testing} onClick={onTest}>测试此档位</Button>
      {testResult && <Alert
        type={testResult.ok ? "success" : "error"}
        showIcon
        title={testResult.ok ? `最近测试成功${testResult.latencyMs === undefined ? "" : ` · ${testResult.latencyMs.toFixed(1)} ms`}` : "最近测试失败"}
        description={`${testResult.message} · ${testResult.testedAt}`}
      />}
    </Space>
  </Card>;
}

export function TaskRoutingPanel({
  plugin,
  icon,
  tone,
  tasks,
}: {
  plugin: string;
  icon: string;
  tone: string;
  tasks: Array<{ key: string; label: string; profileEditor: React.ReactNode; thinkingEditor: React.ReactNode; routeHint: string }>;
}): React.JSX.Element {
  return <Card
    size="small"
    title={<Space size={8}><span className={`env-badge ${tone}`} aria-hidden>{icon}</span>{plugin}<Tag color="orange">需重启</Tag></Space>}
  >
    <Space orientation="vertical" size="large" style={{ width: "100%" }}>
      {tasks.map((task) => <Card size="small" key={task.key} title={task.label} extra={<Text type="secondary">{task.routeHint}</Text>}>
        <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
          {task.profileEditor}
          {task.thinkingEditor}
        </Space>
      </Card>)}
    </Space>
  </Card>;
}

export function EnvironmentSection({
  entries,
  columns,
  loading,
}: {
  entries: EnvironmentEntry[];
  columns: ColumnsType<EnvironmentEntry>;
  loading: boolean;
}): React.JSX.Element {
  return <Table
    rowKey="key"
    loading={loading}
    columns={columns}
    dataSource={entries}
    pagination={false}
    scroll={{ x: 960 }}
  />;
}

export function DirtySaveBar({
  totalChanges,
  saving,
  onDiscard,
  onPreview,
}: {
  totalChanges: number;
  saving: boolean;
  onDiscard: () => void;
  onPreview: () => void;
}): React.JSX.Element {
  return <div className="env-save-bar liquid-glass" role="status">
    <span className="env-save-bar-text">未保存修改 {totalChanges} 项 · 保存后需重启</span>
    <Button onClick={onDiscard}>撤销全部</Button>
    <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={onPreview}>预览并保存</Button>
  </div>;
}

export function EnvironmentDiffPreview({
  open,
  rows,
  saving,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  rows: EnvironmentDiffRow[];
  saving: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}): React.JSX.Element {
  const columns: ColumnsType<EnvironmentDiffRow> = [
    { title: "范围", dataIndex: "area", width: 130 },
    { title: "配置", dataIndex: "key", width: 220, render: (value: string) => <Text code>{value}</Text> },
    { title: "当前", dataIndex: "before", render: (value: string, row) => row.secret ? <Text type="secondary">{value}</Text> : value },
    { title: "保存后", dataIndex: "after", render: (value: string, row) => row.secret ? <Text strong>{value}</Text> : value },
    { title: "生效", width: 90, render: () => <Tag color="orange">需重启</Tag> },
  ];
  return <Modal
    open={open}
    title="保存前差异预览"
    width={920}
    okText="确认保存"
    cancelText="继续修改"
    confirmLoading={saving}
    onCancel={onCancel}
    onOk={onConfirm}
  >
    <Alert className="section-alert" type="info" showIcon title="以下修改只写入根 .env；敏感值始终脱敏，不会在预览中显示。" />
    <Table rowKey={(row) => `${row.area}-${row.key}`} size="small" pagination={false} dataSource={rows} columns={columns} scroll={{ x: 800 }} />
  </Modal>;
}

export function ProviderGrid({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <Row gutter={[16, 16]}>{children}</Row>;
}

export function ProviderGridItem({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <Col xs={24} lg={12} xl={8}>{children}</Col>;
}
