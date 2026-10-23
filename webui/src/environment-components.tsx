import {
  CheckCircleFilled,
  CloseCircleFilled,
  DeleteOutlined,
  ExclamationCircleFilled,
  ExperimentOutlined,
  InfoCircleFilled,
  PoweroffOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useRef, useState } from "react";
import type { EnvironmentCategory } from "./environment-fields";
import type { EnvironmentDiagnostic, LLMProviderSnapshot } from "./types";

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

// ── 提供商 / 模型档位 / 任务路由 ──

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
  const keyState = removingKey ? ["red", "待移除"] : replacement ? ["orange", "待替换"] : provider.apiKeyConfigured ? ["green", "已配置"] : ["red", "未配置"];
  return <Card
    size="small"
    className="envx-card"
    title={(
      <Space size={8} wrap>
        <span className="env-badge tone-sky" aria-hidden>🔌</span>
        {provider.id || "未命名提供商"}
        {provider.builtIn && <Tag>内置</Tag>}
        {provider.overridden && <Tag color="red">外部环境覆盖</Tag>}
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
    <div className="envx-form">
      <label className="envx-form-row">
        <span>提供商 ID</span>
        <Input
          value={provider.id}
          disabled={!provider.isNew}
          onChange={(event) => onUpdate({ id: event.target.value.toLowerCase() })}
          placeholder="小写字母开头，例如 fast"
        />
      </label>
      <label className="envx-form-row">
        <span>Base URL</span>
        <Input
          value={provider.baseUrl}
          onChange={(event) => onUpdate({ baseUrl: event.target.value })}
          placeholder="https://example.com/v1"
          className="envx-mono"
        />
      </label>
      <label className="envx-form-row">
        <span>API Key <Tag color={keyState[0]} className="envx-tag">{keyState[1]}</Tag></span>
        <Input.Password
          value={replacement ? provider.apiKey ?? "" : ""}
          visibilityToggle={false}
          autoComplete="new-password"
          placeholder={provider.apiKeyConfigured ? "已配置，输入新值以替换" : "输入 API Key"}
          onChange={(event) => onUpdate({ apiKey: event.target.value || undefined })}
        />
      </label>
      <Space size={8} wrap className="envx-card-foot">
        <Tooltip title={usage.length ? usage.join("、") : "没有模型档位或任务使用它"}>
          <Text type="secondary" className="envx-small">
            {usage.length ? `被 ${usage.length} 处使用` : "暂未被使用"}
          </Text>
        </Tooltip>
        {provider.builtIn && provider.apiKeyConfigured && !removingKey && (
          <Popconfirm title="移除 default 提供商的 API Key？" onConfirm={() => onUpdate({ apiKey: null })}>
            <Button danger size="small" type="link">移除密钥</Button>
          </Popconfirm>
        )}
      </Space>
      {provider.overridden && (
        <Text type="danger" className="envx-small">保存只修改配置文件，重启后仍会被进程环境或环境文件覆盖。</Text>
      )}
    </div>
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
    className="envx-card"
    title={<Space size={8}><span className={`env-badge ${tone}`} aria-hidden>{icon}</span>{title}</Space>}
    extra={<Button size="small" icon={<ExperimentOutlined />} loading={testing} onClick={onTest}>测试</Button>}
  >
    <div className="envx-form">
      <Text type="secondary" className="envx-small">{description}</Text>
      {editors.map((node, index) => <div key={index}>{node}</div>)}
      <div className="envx-route">
        实际路由 <Text code>{resolvedRoute}</Text>{fallback ? <Tag className="envx-tag">继承默认档位</Tag> : null}
      </div>
      {testResult && <Alert
        type={testResult.ok ? "success" : "error"}
        showIcon
        title={testResult.ok ? `测试成功${testResult.latencyMs === undefined ? "" : ` · ${testResult.latencyMs.toFixed(1)} ms`}` : "测试失败"}
        description={`${testResult.message} · ${testResult.testedAt}`}
      />}
    </div>
  </Card>;
}

export interface TaskRouteRow {
  key: string;
  plugin: string;
  pluginIcon: string;
  label: string;
  profileEditor: React.ReactNode;
  thinkingEditor: React.ReactNode;
  route: string;
  dirty: boolean;
}

export function TaskRoutingTable({ rows }: { rows: TaskRouteRow[] }): React.JSX.Element {
  const columns: ColumnsType<TaskRouteRow> = [
    {
      title: "任务",
      dataIndex: "label",
      width: 200,
      render: (label: string, row) => (
        <Space size={6}>
          <span aria-hidden>{row.pluginIcon}</span>
          <span><Text type="secondary">{row.plugin} · </Text>{label}</span>
          {row.dirty && <Tag color="gold" className="envx-tag">未保存</Tag>}
        </Space>
      ),
    },
    { title: "模型档位", width: 200, render: (_, row) => row.profileEditor },
    { title: "推理", width: 220, render: (_, row) => row.thinkingEditor },
    { title: "实际路由", dataIndex: "route", render: (route: string) => <Text type="secondary" className="envx-small">{route}</Text> },
  ];
  return <Table
    className="envx-task-table"
    rowKey="key"
    size="small"
    columns={columns}
    dataSource={rows}
    pagination={false}
    scroll={{ x: 720 }}
  />;
}

// ── 体检 ──

const LEVEL_META: Record<EnvironmentDiagnostic["level"], { icon: React.ReactNode; label: string }> = {
  error: { icon: <CloseCircleFilled />, label: "错误" },
  warning: { icon: <ExclamationCircleFilled />, label: "提醒" },
  info: { icon: <InfoCircleFilled />, label: "提示" },
};

export function DiagnosticsPanel({
  items,
  onLocate,
}: {
  items: EnvironmentDiagnostic[];
  onLocate: (item: EnvironmentDiagnostic) => void;
}): React.JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const errors = items.filter((item) => item.level === "error").length;
  const warnings = items.filter((item) => item.level === "warning").length;
  if (items.length === 0) {
    return (
      <div className="envx-health is-ok" role="status">
        <CheckCircleFilled /> 配置体检：一切正常
      </div>
    );
  }
  // 默认只展开错误和提醒；纯提示折叠，避免顶部又堆一大片
  const visible = expanded ? items : items.filter((item) => item.level !== "info");
  const hidden = items.length - visible.length;
  return (
    <section className={`envx-health${errors ? " is-error" : warnings ? " is-warning" : ""}`} aria-label="配置体检">
      <div className="envx-health-head">
        <Text strong>配置体检</Text>
        {errors > 0 && <Tag color="red">{errors} 个错误</Tag>}
        {warnings > 0 && <Tag color="gold">{warnings} 个提醒</Tag>}
        {items.length - errors - warnings > 0 && <Tag>{items.length - errors - warnings} 个提示</Tag>}
        <span className="envx-grow" />
        {(hidden > 0 || expanded) && (
          <Button size="small" type="link" onClick={() => setExpanded((current) => !current)}>
            {expanded ? "只看重要的" : `显示全部（另有 ${hidden} 条提示）`}
          </Button>
        )}
      </div>
      <ul className="envx-health-list">
        {visible.map((item) => (
          <li key={`${item.title}-${item.keys.join(",")}`} className={`envx-health-item is-${item.level}`}>
            <span className="envx-health-icon" aria-label={LEVEL_META[item.level].label}>{LEVEL_META[item.level].icon}</span>
            <div className="envx-health-text">
              <Text strong>{item.title}</Text>
              <Text type="secondary" className="envx-small">{item.detail}</Text>
            </div>
            {item.keys.length > 0 && (
              <Button size="small" onClick={() => onLocate(item)}>定位</Button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

// ── 待重启 ──

export function RestartBanner({
  supported,
  restarting,
  onRestart,
}: {
  supported: boolean;
  restarting: boolean;
  onRestart: () => void;
}): React.JSX.Element {
  return (
    <div className="envx-restart" role="status">
      <PoweroffOutlined />
      <div className="envx-restart-text">
        <Text strong>{restarting ? "正在重启 YawnBot…" : "有已保存的修改尚未生效"}</Text>
        <Text type="secondary" className="envx-small">
          {restarting
            ? "重启通常需要十几秒到一分钟，恢复后页面会自动刷新。"
            : supported
              ? "重启 YawnBot 后生效；重启期间 Bot 和管理台会短暂断开。"
              : "当前运行方式不支持网页重启，请在服务器上执行 docker restart yawnbot（或重新运行 nb run）。"}
        </Text>
      </div>
      {supported && (
        <Button type="primary" danger loading={restarting} onClick={onRestart}>重启 YawnBot</Button>
      )}
    </div>
  );
}

// ── 分类导航 ──

export interface CategoryStat {
  total: number;
  customized: number;
  dirty: number;
  issues: number;
}

export function CategoryNav({
  categories,
  active,
  stats,
  onSelect,
}: {
  categories: EnvironmentCategory[];
  active: string | null;
  stats: Record<string, CategoryStat>;
  onSelect: (key: string) => void;
}): React.JSX.Element {
  const navRef = useRef<HTMLElement>(null);
  // 窄屏下导航是横向滚动条：把当前分类滚到可见位置
  useEffect(() => {
    const nav = navRef.current;
    if (!nav || nav.scrollWidth <= nav.clientWidth) return;
    const item = nav.querySelector<HTMLElement>(".envx-nav-item.is-active");
    if (item) nav.scrollLeft = item.offsetLeft - (nav.clientWidth - item.clientWidth) / 2;
  }, [active]);
  return (
    <nav className="envx-nav" aria-label="配置分类" ref={navRef}>
      {categories.map((category) => {
        const stat = stats[category.key] ?? { total: 0, customized: 0, dirty: 0, issues: 0 };
        return (
          <button
            key={category.key}
            type="button"
            className={`envx-nav-item${active === category.key ? " is-active" : ""}`}
            aria-current={active === category.key ? "page" : undefined}
            onClick={() => onSelect(category.key)}
            title={category.hint}
          >
            <span className="envx-nav-icon" aria-hidden>{category.icon}</span>
            <span className="envx-nav-label">{category.label}</span>
            <span className="envx-nav-badges">
              {stat.issues > 0 && <span className="envx-pill is-error" title="有问题的配置">{stat.issues}</span>}
              {stat.dirty > 0 && <span className="envx-pill is-dirty" title="未保存">{stat.dirty}</span>}
              {stat.issues === 0 && stat.dirty === 0 && (
                <span className="envx-nav-count" title={`共 ${stat.total} 项，已自定义 ${stat.customized} 项`}>
                  {stat.customized > 0 ? `${stat.customized}/${stat.total}` : stat.total}
                </span>
              )}
            </span>
          </button>
        );
      })}
    </nav>
  );
}

// ── 保存 ──

export function DirtySaveBar({
  totalChanges,
  errorCount,
  saving,
  onDiscard,
  onPreview,
}: {
  totalChanges: number;
  errorCount: number;
  saving: boolean;
  onDiscard: () => void;
  onPreview: () => void;
}): React.JSX.Element {
  return <div className="env-save-bar liquid-glass" role="status">
    <span className="env-save-bar-text">
      未保存修改 {totalChanges} 项
      {errorCount > 0 ? <Text type="danger"> · {errorCount} 项有误，修正后才能保存</Text> : " · 保存后重启生效"}
    </span>
    <Popconfirm title="撤销全部未保存的修改？" onConfirm={onDiscard}>
      <Button>撤销全部</Button>
    </Popconfirm>
    <Tooltip title="Ctrl/⌘ + S">
      <Button type="primary" icon={<SaveOutlined />} loading={saving} disabled={errorCount > 0} onClick={onPreview}>预览并保存</Button>
    </Tooltip>
  </div>;
}

export function EnvironmentDiffPreview({
  open,
  rows,
  dangers,
  saving,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  rows: EnvironmentDiffRow[];
  dangers: string[];
  saving: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}): React.JSX.Element {
  const columns: ColumnsType<EnvironmentDiffRow> = [
    { title: "配置", dataIndex: "key", width: 240, render: (value: string) => <Text code>{value}</Text> },
    { title: "当前", dataIndex: "before", render: (value: string, row) => row.secret ? <Text type="secondary">{value}</Text> : <span className="envx-diff-value">{value}</span> },
    { title: "保存后", dataIndex: "after", render: (value: string, row) => row.secret ? <Text strong>{value}</Text> : <span className="envx-diff-value is-new">{value}</span> },
  ];
  const areas = [...new Set(rows.map((row) => row.area))];
  return <Modal
    open={open}
    title={`保存 ${rows.length} 项修改`}
    width={880}
    okText="确认保存"
    cancelText="继续修改"
    okButtonProps={{ danger: dangers.length > 0 }}
    confirmLoading={saving}
    onCancel={onCancel}
    onOk={onConfirm}
  >
    <Space orientation="vertical" size={12} style={{ width: "100%" }}>
      {dangers.map((note) => <Alert key={note} type="warning" showIcon title={note} />)}
      {areas.map((area) => (
        <div key={area}>
          <div className="envx-diff-area">{area}</div>
          <Table
            rowKey="key"
            size="small"
            pagination={false}
            dataSource={rows.filter((row) => row.area === area)}
            columns={columns}
            scroll={{ x: 640 }}
          />
        </div>
      ))}
      <Text type="secondary" className="envx-small">敏感值始终脱敏，不会在预览中显示。保存只写配置文件，重启 YawnBot 后生效。</Text>
    </Space>
  </Modal>;
}
