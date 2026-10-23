// 列表视图：服务端分页 + 排序 + 多条件筛选；可勾选批量删除、展开看详情、点行开抽屉。
import { DeleteOutlined, EditOutlined, MoreOutlined, RetweetOutlined } from "@ant-design/icons";
import { Button, Dropdown, Empty, Popconfirm, Segmented, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { SortOrder, SorterResult, TableRowSelection } from "antd/es/table/interface";
import { useEffect, useState } from "react";
import { RELATION_SOURCE_META } from "../agent-meta";
import { relationTypeColor } from "../relation-meta";
import { QueryErrorAlert } from "../shared";
import type { AgentRelationListItem } from "../types";
import { ConfidencePill, FreshnessText, MemberChip } from "./RelationDetailDrawers";
import { relationSentence } from "./relation-analysis";
import type { RelationsContext } from "./relations-context";

const { Text } = Typography;

export type RelationSortKey = "confidence" | "lastSeen" | "evidence" | "type";
export interface RelationTableState {
  page: number;
  pageSize: number;
  sort: RelationSortKey;
  order: "asc" | "desc";
}

const DENSITY_KEY = "yawnbot.relations.density";

function readDensity(): "middle" | "small" {
  try {
    return window.localStorage.getItem(DENSITY_KEY) === "small" ? "small" : "middle";
  } catch {
    return "middle";
  }
}

// 点击行内交互元素时不触发整行打开详情。
function fromInteractive(event: React.MouseEvent): boolean {
  return !!(event.target as HTMLElement).closest("button, a, input, label, .ant-checkbox-wrapper, .ant-typography-copy, .ant-dropdown-trigger, .ant-table-row-expand-icon");
}

export function RelationTableView({
  ctx,
  rows,
  total,
  loading,
  error,
  reload,
  state,
  onStateChange,
  hasFilters,
  onResetFilters,
}: {
  ctx: RelationsContext;
  rows: AgentRelationListItem[];
  total: number;
  loading: boolean;
  error: string;
  reload: () => void;
  state: RelationTableState;
  onStateChange: (patch: Partial<RelationTableState>) => void;
  hasFilters: boolean;
  onResetFilters: () => void;
}): React.JSX.Element {
  const [selected, setSelected] = useState<string[]>([]);
  const [density, setDensity] = useState(readDensity);
  const writable = !ctx.readOnly;

  // 翻页/筛选后清掉看不见的勾选，避免误删不在当前页的边。
  useEffect(() => {
    setSelected((current) => current.filter((id) => rows.some((row) => row.id === id)));
  }, [rows]);

  if (error && rows.length === 0) return <QueryErrorAlert error={error} onRetry={reload} />;

  const sortOrderOf = (key: RelationSortKey): SortOrder => (state.sort === key ? (state.order === "asc" ? "ascend" : "descend") : null);
  const selection: TableRowSelection<AgentRelationListItem> | undefined = writable ? {
    selectedRowKeys: selected,
    onChange: (keys) => setSelected(keys.map(String)),
    getCheckboxProps: () => ({ disabled: ctx.locked }),
  } : undefined;
  const selectedRows = rows.filter((row) => selected.includes(row.id));
  const reversible = selectedRows.filter((row) => !row.mutual);

  const columns: ColumnsType<AgentRelationListItem> = [
    {
      title: "关系",
      key: "relation",
      width: 340,
      render: (_: unknown, row: AgentRelationListItem) => (
        <div className="rel-cell-relation">
          <MemberChip userId={row.subjectUserId} name={row.subjectName || ctx.nameOf(row.subjectUserId)} onClick={() => ctx.openMember(row.subjectUserId)} />
          <span className="rel-cell-link">
            <Tag color={relationTypeColor(row.type)} className="rel-cell-type">{row.type}</Tag>
            <Tooltip title={row.mutual ? "互相成立：对方也有同类型关系指向 TA" : "单向关系"}>
              <span className={`rel-dir${row.mutual ? " is-mutual" : ""}`} aria-label={row.mutual ? "互相" : "单向"}>{row.mutual ? "⇄" : "→"}</span>
            </Tooltip>
          </span>
          <MemberChip userId={row.objectUserId} name={row.objectName || ctx.nameOf(row.objectUserId)} onClick={() => ctx.openMember(row.objectUserId)} />
        </div>
      ),
    },
    {
      title: "备注",
      dataIndex: "note",
      key: "note",
      ellipsis: { showTitle: false },
      render: (value: string) => value ? <Tooltip title={value} placement="topLeft"><span>{value}</span></Tooltip> : <Text type="secondary">—</Text>,
    },
    ...(writable ? [{
      title: "来源",
      dataIndex: "sourceKind",
      key: "source",
      width: 84,
      render: (value: string) => <Tag color={RELATION_SOURCE_META[value]?.color}>{RELATION_SOURCE_META[value]?.label ?? value}</Tag>,
    }] : []),
    {
      title: "置信度",
      dataIndex: "confidence",
      key: "confidence",
      width: 130,
      sorter: true,
      sortOrder: sortOrderOf("confidence"),
      render: (value: number) => <ConfidencePill value={value} />,
    },
    ...(writable ? [{
      title: "证据",
      dataIndex: "evidenceCount",
      key: "evidence",
      width: 76,
      sorter: true,
      sortOrder: sortOrderOf("evidence"),
      render: (value: number) => <span className="rel-num">{value}</span>,
    }] : []),
    {
      title: "最后见到",
      dataIndex: "lastSeenAt",
      key: "lastSeen",
      width: 118,
      sorter: true,
      sortOrder: sortOrderOf("lastSeen"),
      render: (value: string | null) => <FreshnessText iso={value} />,
    },
    ...(writable ? [{
      title: "",
      key: "actions",
      width: 92,
      render: (_: unknown, row: AgentRelationListItem) => (
        <Space size={0} className="rel-row-actions">
          <Tooltip title="编辑"><Button type="text" size="small" icon={<EditOutlined />} aria-label="编辑" disabled={ctx.locked} onClick={() => ctx.openEdit(row)} /></Tooltip>
          <Popconfirm title="删除这条关系边？" okButtonProps={{ danger: true }} onConfirm={() => ctx.removeEdges([row.id])}>
            <Tooltip title="删除"><Button type="text" size="small" danger icon={<DeleteOutlined />} aria-label="删除" disabled={ctx.locked} /></Tooltip>
          </Popconfirm>
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                { key: "reverse", icon: <RetweetOutlined />, label: "补全反向关系", disabled: !!row.mutual || ctx.locked },
                { key: "graph", label: "在图谱中定位" },
                { key: "member", label: `只看 ${row.subjectName || ctx.nameOf(row.subjectUserId)}` },
              ],
              onClick: ({ key }) => {
                if (key === "reverse") void ctx.createReverse([row]);
                if (key === "graph") ctx.focusInGraph(row.subjectUserId);
                if (key === "member") ctx.patchFilters({ member: row.subjectUserId });
              },
            }}
          >
            <Button type="text" size="small" icon={<MoreOutlined />} aria-label="更多操作" />
          </Dropdown>
        </Space>
      ),
    }] : []),
  ];

  return (
    <div className="rel-table-view">
      <div className="rel-table-bar">
        {writable && selected.length > 0 ? (
          <div className="rel-batch-bar" role="status">
            <Text strong>已选 {selected.length} 条</Text>
            <Popconfirm
              title={`删除选中的 ${selected.length} 条关系边？`}
              description="删除后不可恢复；自动来源的边可能在之后的整理中再次出现。"
              okButtonProps={{ danger: true }}
              onConfirm={async () => { if (await ctx.removeEdges(selected)) setSelected([]); }}
            >
              <Button size="small" danger icon={<DeleteOutlined />} disabled={ctx.locked}>批量删除</Button>
            </Popconfirm>
            {reversible.length > 0 && (
              <Button size="small" icon={<RetweetOutlined />} disabled={ctx.locked} onClick={() => { void ctx.createReverse(reversible).then(() => setSelected([])); }}>
                补全反向（{reversible.length}）
              </Button>
            )}
            <Button size="small" type="link" onClick={() => setSelected([])}>取消选择</Button>
          </div>
        ) : (
          <Text type="secondary" className="rel-small">
            共 {total} 条{hasFilters ? "（已筛选）" : ""} · 点击行查看详情，点击成员查看档案
          </Text>
        )}
        <Segmented
          size="small"
          value={density}
          onChange={(value) => {
            const next = value as "middle" | "small";
            setDensity(next);
            try { window.localStorage.setItem(DENSITY_KEY, next); } catch { /* 本机偏好写不进去就算了 */ }
          }}
          options={[{ value: "middle", label: "舒适" }, { value: "small", label: "紧凑" }]}
          aria-label="表格密度"
        />
      </div>
      <Table<AgentRelationListItem>
        className="rel-table"
        rowKey="id"
        size={density}
        loading={loading}
        dataSource={rows}
        rowSelection={selection}
        columns={columns}
        scroll={{ x: 900 }}
        rowClassName={(row) => [
          row.confidence < 0.4 ? "is-low" : "",
          selected.includes(row.id) ? "is-selected" : "",
        ].filter(Boolean).join(" ")}
        onRow={(row) => ({
          onClick: (event) => { if (!fromInteractive(event)) ctx.openEdge(row); },
          onKeyDown: (event) => { if (event.key === "Enter" && event.target === event.currentTarget) ctx.openEdge(row); },
          tabIndex: 0,
          className: "rel-row-clickable",
        })}
        expandable={{
          expandedRowRender: (row) => (
            <div className="rel-expanded">
              <Text>{relationSentence(row.subjectName || ctx.nameOf(row.subjectUserId), row.type, row.objectName || ctx.nameOf(row.objectUserId))}{row.mutual ? "，而且是互相的。" : "。"}</Text>
              {row.note && <Text type="secondary">备注：{row.note}</Text>}
              <Text type="secondary" className="rel-mono">{row.subjectUserId} → {row.objectUserId}{writable ? ` · 证据 ${row.evidenceCount} 次` : ""}</Text>
            </div>
          ),
          columnWidth: 36,
        }}
        locale={{
          emptyText: hasFilters
            ? <Empty description="没有符合筛选条件的关系"><Button onClick={onResetFilters}>清除筛选</Button></Empty>
            : <Empty description="暂无关系记忆">{writable && <Button type="primary" disabled={ctx.locked} onClick={() => ctx.openCreate()}>新增第一条关系</Button>}</Empty>,
        }}
        pagination={{
          current: state.page,
          pageSize: state.pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: [20, 50, 100],
          showTotal: (count, range) => `${range[0]}–${range[1]} / ${count}`,
          onChange: (page, pageSize) => onStateChange({ page: pageSize !== state.pageSize ? 1 : page, pageSize }),
        }}
        onChange={(_pagination, _filters, sorter) => {
          const single = (Array.isArray(sorter) ? sorter[0] : sorter) as SorterResult<AgentRelationListItem>;
          const key = single?.columnKey as RelationSortKey | undefined;
          if (!single?.order || !key) {
            if (state.sort !== "confidence" || state.order !== "desc") onStateChange({ sort: "confidence", order: "desc", page: 1 });
            return;
          }
          const order = single.order === "ascend" ? "asc" : "desc";
          if (key !== state.sort || order !== state.order) onStateChange({ sort: key, order, page: 1 });
        }}
      />
    </div>
  );
}
