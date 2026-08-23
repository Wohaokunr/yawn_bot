import { BookOutlined, EyeOutlined } from "@ant-design/icons";
import { Alert, Card, Descriptions, Drawer, Empty, Space, Spin, Table, Tabs, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import type { RpgModuleDetail, RpgModuleSummary } from "./types";

const { Text } = Typography;

function JsonCell({ value }: { value: unknown }): React.JSX.Element {
  return <pre>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>;
}

function ModuleEntityTable({ rows, columns }: { rows: Record<string, unknown>[]; columns: ColumnsType<Record<string, unknown>> }): React.JSX.Element {
  return rows.length === 0 ? <Empty description="暂无数据" /> : <Table<Record<string, unknown>> rowKey={(row) => String(row.id ?? JSON.stringify(row))} size="small" pagination={{ pageSize: 8, showSizeChanger: false }} dataSource={rows} columns={columns} />;
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
      <Card size="small" className="section-row" title="模组说明"><pre>{detail.description || "暂无说明"}</pre><pre>{detail.opening || "暂无开场"}</pre></Card>
      <Tabs items={[
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
    { title: "操作", width: 90, render: (_, row) => <a onClick={() => setSelectedId(row.id)}><EyeOutlined /> 详情</a> },
  ];
  return <>
    <PageHeader title="跑团模组库" subtitle="查看已加载模组的场景、实体、线索、推论与结局（管理员只读视角）" />
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
