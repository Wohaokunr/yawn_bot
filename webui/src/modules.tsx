import { BookOutlined, CheckCircleOutlined, EyeOutlined, WarningOutlined } from "@ant-design/icons";
import { Alert, Card, Col, Descriptions, Drawer, Empty, Row, Space, Spin, Statistic, Table, Tabs, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import type { RpgModuleDetail, RpgModuleLintIssue, RpgModuleSummary } from "./types";

const { Text } = Typography;

function JsonCell({ value }: { value: unknown }): React.JSX.Element {
  return <pre>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>;
}

function ModuleEntityTable({ rows, columns }: { rows: Record<string, unknown>[]; columns: ColumnsType<Record<string, unknown>> }): React.JSX.Element {
  return rows.length === 0 ? <Empty description="暂无数据" /> : <Table<Record<string, unknown>> rowKey={(row) => String(row.id ?? JSON.stringify(row))} size="small" pagination={{ pageSize: 8, showSizeChanger: false }} dataSource={rows} columns={columns} />;
}

function moduleHealthTag(module: RpgModuleSummary): React.JSX.Element {
  const health = module.health;
  if (health.status === "error") return <Tag color="red" icon={<WarningOutlined />}>{health.errorCount} 错误</Tag>;
  if (health.status === "warning") return <Tag color="orange" icon={<WarningOutlined />}>{health.warningCount} 警告</Tag>;
  if (health.status === "schema-only") return <Tag color="blue">Schema 已通过</Tag>;
  return <Tag color="green" icon={<CheckCircleOutlined />}>健康</Tag>;
}

function StaticChecks({ detail }: { detail: RpgModuleDetail }): React.JSX.Element {
  const health = detail.health;
  const columns: ColumnsType<RpgModuleLintIssue> = [
    { title: "级别", dataIndex: "severity", width: 90, render: (value: RpgModuleLintIssue["severity"]) => <Tag color={value === "ERROR" ? "red" : value === "WARNING" ? "orange" : "blue"}>{value}</Tag> },
    { title: "范围", dataIndex: "section", width: 90 },
    { title: "定位", dataIndex: "path", width: 240 },
    { title: "检查结果", dataIndex: "message" },
    { title: "提示", dataIndex: "hint", ellipsis: true },
  ];
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Alert
      type={health.status === "error" ? "error" : health.status === "warning" ? "warning" : "success"}
      showIcon
      title={health.lintAvailable ? `静态检查：${health.errorCount} 错误 · ${health.warningCount} 警告 · ${health.infoCount} 提示` : "运行时 Schema 校验已通过"}
      description={health.lintAvailable ? "结果复用模组编辑器的静态检查规则；WebUI 只读展示，不会修改模组。" : "当前部署未包含编辑器 lint 工具，因此仅展示运行时加载校验状态。"}
    />
    {health.lintAvailable && (health.issues.length === 0
      ? <Empty description="静态检查零诊断" />
      : <Table rowKey={(row) => `${row.severity}-${row.path}-${row.message}`} size="small" pagination={{ pageSize: 12, showSizeChanger: false }} dataSource={health.issues} columns={columns} scroll={{ x: 900 }} />)}
  </Space>;
}

function ModuleDetailDrawer({ moduleId, onClose }: { moduleId: string | null; onClose: () => void }): React.JSX.Element {
  const [detail, setDetail] = useState<RpgModuleDetail | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (moduleId === null) { setDetail(null); setError(""); return; }
    let alive = true;
    setDetail(null);
    setError("");
    api<RpgModuleDetail>(`/rpg/modules/${encodeURIComponent(moduleId)}`)
      .then((result) => { if (alive) setDetail(result.data); })
      .catch((reason) => { if (alive) setError(reason instanceof Error ? reason.message : "模组详情加载失败"); });
    return () => { alive = false; };
  }, [moduleId]);
  const sceneColumns: ColumnsType<Record<string, unknown>> = [
    { title: "场景", render: (_, row) => <><Text strong>{String(row.name ?? row.id)}</Text><br /><Text type="secondary">{String(row.id ?? "")}</Text></> },
    { title: "旁白", dataIndex: "narration", render: (value) => <JsonCell value={value} /> },
    { title: "出口", render: (_, row) => <JsonCell value={row.exits} /> },
    { title: "检定点", render: (_, row) => <JsonCell value={row.checks} /> },
  ];
  const npcColumns: ColumnsType<Record<string, unknown>> = [
    { title: "NPC", render: (_, row) => <><Text strong>{String(row.name ?? row.id)}</Text><br /><Text type="secondary">{String(row.id ?? "")}</Text></> },
    { title: "公开简介", dataIndex: "publicDesc", render: (value) => <JsonCell value={value} /> },
    { title: "事实/社交节点", render: (_, row) => <JsonCell value={{ facts: row.facts, socialNodes: row.socialNodes }} /> },
    { title: "秘密", render: (_, row) => <Tag color="red">{Array.isArray(row.secrets) ? `${row.secrets.length} 条` : "—"}</Tag> },
  ];
  const clueColumns: ColumnsType<Record<string, unknown>> = [
    { title: "线索", render: (_, row) => <><Text strong>{String(row.name ?? row.id)}</Text><br /><Text type="secondary">{String(row.id ?? "")}</Text></> },
    { title: "类别", dataIndex: "category" },
    { title: "全文", dataIndex: "text", render: (value) => <JsonCell value={value} /> },
    { title: "来源提示", dataIndex: "sourceHint" },
  ];
  const simpleColumns: ColumnsType<Record<string, unknown>> = [
    { title: "编号", dataIndex: "id", width: 160 },
    { title: "名称", dataIndex: "name", width: 180 },
    { title: "详情", render: (_, row) => <JsonCell value={row} /> },
  ];
  return <Drawer open={moduleId !== null} onClose={onClose} width="min(1180px, 100%)" title={<Space><BookOutlined />{detail?.name ?? "模组详情"}</Space>}>
    {error && <Alert type="error" showIcon message="模组详情加载失败" description={error} />}
    {!detail && !error && <Spin />}
    {detail && <>
      <Descriptions className="section-row" size="small" column={3} items={[
        { key: "id", label: "模组 ID", children: detail.id },
        { key: "difficulty", label: "难度", children: <Tag color="purple">{detail.difficulty}</Tag> },
        { key: "players", label: "人数", children: `${detail.minPlayers}–${detail.maxPlayers}` },
        { key: "start", label: "起始场景", children: detail.startScene },
        { key: "time", label: "起始时刻", children: detail.time.start },
        { key: "generic", label: "通用结局", children: detail.genericEndings ? "启用" : "关闭" },
      ]} />
      <Row gutter={[12, 12]} className="section-row">
        <Col xs={12} md={6}><Card size="small"><Statistic title="场景" value={detail.sceneCount} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="线索" value={detail.clueCount} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="结局" value={detail.endingCount} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="静态健康" value={detail.health.errorCount + detail.health.warningCount} suffix="项需关注" /></Card></Col>
      </Row>
      <Card size="small" className="section-row" title="模组说明"><pre>{detail.description || "暂无说明"}</pre><pre>{detail.opening || "暂无开场"}</pre></Card>
      <Tabs items={[
        { key: "health", label: `静态检查 (${detail.health.errorCount + detail.health.warningCount})`, children: <StaticChecks detail={detail} /> },
        { key: "scenes", label: `场景 (${detail.scenes.length})`, children: <ModuleEntityTable rows={detail.scenes as unknown as Record<string, unknown>[]} columns={sceneColumns} /> },
        { key: "npcs", label: `NPC (${detail.npcs.length})`, children: <ModuleEntityTable rows={detail.npcs as unknown as Record<string, unknown>[]} columns={npcColumns} /> },
        { key: "monsters", label: `怪物 (${detail.monsters.length})`, children: <ModuleEntityTable rows={detail.monsters} columns={simpleColumns} /> },
        { key: "clues", label: `线索 (${detail.clues.length})`, children: <ModuleEntityTable rows={detail.clues as unknown as Record<string, unknown>[]} columns={clueColumns} /> },
        { key: "deductions", label: `推论 (${detail.deductions.length})`, children: <ModuleEntityTable rows={detail.deductions} columns={simpleColumns} /> },
        { key: "endings", label: `结局 (${detail.endings.length})`, children: <ModuleEntityTable rows={detail.endings} columns={simpleColumns} /> },
        { key: "events", label: `事件 (${detail.events.length})`, children: <ModuleEntityTable rows={detail.events} columns={simpleColumns} /> },
      ]} />
    </>}
  </Drawer>;
}

export function ModulesPage(): React.JSX.Element {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const load = useCallback(() => api<RpgModuleSummary[]>("/rpg/modules").then((result) => result.data), []);
  const query = useApiQuery(load);
  const columns: ColumnsType<RpgModuleSummary> = [
    { title: "模组", render: (_, row) => <><Text strong>{row.name}</Text><br /><Text type="secondary">{row.id}</Text></> },
    { title: "简介", dataIndex: "description", ellipsis: true },
    { title: "难度", dataIndex: "difficulty", width: 90, render: (value: string) => <Tag color="purple">{value}</Tag> },
    { title: "人数", width: 90, render: (_, row) => `${row.minPlayers}–${row.maxPlayers}` },
    { title: "内容", width: 240, render: (_, row) => `${row.sceneCount} 场景 · ${row.npcCount} NPC · ${row.clueCount} 线索 · ${row.endingCount} 结局` },
    { title: "健康", width: 120, render: (_, row) => moduleHealthTag(row) },
    { title: "操作", width: 90, render: (_, row) => <a onClick={() => setSelectedId(row.id)}><EyeOutlined /> 详情</a> },
  ];
  const modules = query.data ?? [];
  const healthy = modules.filter((item) => item.health.status === "healthy").length;
  const attention = modules.filter((item) => item.health.status === "warning" || item.health.status === "error").length;
  const scenes = modules.reduce((sum, item) => sum + item.sceneCount, 0);
  const clues = modules.reduce((sum, item) => sum + item.clueCount, 0);
  const endings = modules.reduce((sum, item) => sum + item.endingCount, 0);
  return <>
    <PageHeader title="跑团模组库" subtitle="只读查看已加载模组的内容规模、运行健康与静态检查结果；编辑仍由专用模组工具负责" onRefresh={query.reload} refreshing={query.refreshing} />
    <Row gutter={[12, 12]} className="section-row">
      <Col xs={12} lg={6}><Card size="small"><Statistic title="已加载模组" value={modules.length} /></Card></Col>
      <Col xs={12} lg={6}><Card size="small"><Statistic title="健康 / 需关注" value={healthy} suffix={`/ ${attention}`} /></Card></Col>
      <Col xs={12} lg={6}><Card size="small"><Statistic title="场景 / 线索" value={scenes} suffix={`/ ${clues}`} /></Card></Col>
      <Col xs={12} lg={6}><Card size="small"><Statistic title="声明结局" value={endings} /></Card></Col>
    </Row>
    <Card>
      {query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : query.data && query.data.length > 0
          ? <Table rowKey="id" loading={query.loading} dataSource={query.data} columns={columns} pagination={{ pageSize: 20, showSizeChanger: false }} />
          : query.loading ? <Spin /> : <Empty description="暂无已加载模组" />}
    </Card>
    <ModuleDetailDrawer moduleId={selectedId} onClose={() => setSelectedId(null)} />
  </>;
}
