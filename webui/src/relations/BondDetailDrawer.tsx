// Bot 与某位成员的关系详情：互动统计、共同经历、证据，以及管理员修改/锁定/重置。
import { DeleteOutlined, EditOutlined, LockOutlined, PlusOutlined, TeamOutlined, UnlockOutlined } from "@ant-design/icons";
import {
  App as AntApp, Button, Descriptions, Drawer, Empty, Form, Input, Popconfirm, Select, Slider, Space, Spin, Tag,
  Timeline, Tooltip, Typography,
} from "antd";
import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentBondDetail } from "../types";
import { Meter } from "./RelationCharts";
import { affinityWord, BOND_FIELD_LABELS, BOND_LOCK_KEYS, initiativeRatio, isLocked, stageMeta, trustWord } from "./bond-meta";
import { relativeTime } from "./relation-analysis";

const { Text, Paragraph } = Typography;

interface BondForm {
  label: string;
  memberCallsBot: string;
  botCallsMember: string;
  tone: string;
  note: string;
  insideJokes: string[];
  boundaries: string[];
  affinity: number;
  trust: number;
}

const SOURCE_LABEL: Record<string, string> = { auto: "自动沉淀", agent: "成员本人", manual: "管理员" };

function formFrom(bond: AgentBondDetail): BondForm {
  return {
    label: bond.label ?? "",
    memberCallsBot: bond.memberCallsBot ?? "",
    botCallsMember: bond.botCallsMember ?? "",
    tone: bond.tone ?? "",
    note: bond.note ?? "",
    insideJokes: bond.insideJokes ?? [],
    boundaries: bond.boundaries ?? [],
    affinity: bond.affinity ?? 0,
    trust: bond.trust ?? 0.3,
  };
}

/** 只提交真正改过的字段：后端会把提交的字段锁定，没改的不应被顺带锁住。 */
export function changedBondFields(before: BondForm, after: BondForm): Partial<BondForm> {
  const patch: Partial<BondForm> = {};
  for (const key of Object.keys(before) as Array<keyof BondForm>) {
    const left = before[key];
    const right = after[key];
    const same = Array.isArray(left) && Array.isArray(right)
      ? left.length === right.length && left.every((item, index) => item === right[index])
      : left === right;
    if (!same) (patch as Record<string, unknown>)[key] = right;
  }
  return patch;
}

export function BondDetailDrawer({
  groupId,
  userId,
  readOnly,
  onClose,
  onChanged,
  onOpenMember,
}: {
  groupId: string;
  userId: string | null;
  readOnly: boolean;
  onClose: () => void;
  onChanged: () => void;
  onOpenMember: (userId: string) => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [momentDraft, setMomentDraft] = useState("");
  const [form] = Form.useForm<BondForm>();
  const detail = useApiQuery({
    queryKey: ["agent-bond", groupId, userId],
    fetcher: (signal) => (userId
      ? api<AgentBondDetail>(`/agent/groups/${groupId}/bonds/${userId}`, { signal }).then((response) => response.data)
      : Promise.resolve(null)),
    invalidation: { resources: ["agent_bond"], scope: { groupId } },
  });
  const bond = detail.data;

  useEffect(() => {
    setEditing(false);
    setMomentDraft("");
  }, [userId]);

  const fail = (error: unknown) => message.error(error instanceof ApiError ? error.message : `操作失败：${(error as Error).message}`);

  const save = async () => {
    if (!bond || !userId) return;
    const values = await form.validateFields();
    const patch = changedBondFields(formFrom(bond), values);
    if (Object.keys(patch).length === 0) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await api(`/agent/groups/${groupId}/bonds/${userId}`, { method: "PATCH", body: JSON.stringify(patch) });
      message.success("已保存，改过的字段已锁定，自动整理不会再覆盖");
      setEditing(false);
      detail.reload();
      onChanged();
    } catch (error) {
      fail(error);
    } finally {
      setSaving(false);
    }
  };

  const unlock = async (field: string) => {
    if (!userId) return;
    try {
      await api(`/agent/groups/${groupId}/bonds/${userId}`, { method: "PATCH", body: JSON.stringify({ unlock: [BOND_LOCK_KEYS[field] ?? field] }) });
      message.success(`「${BOND_FIELD_LABELS[field] ?? field}」已交还给自动整理`);
      detail.reload();
      onChanged();
    } catch (error) {
      fail(error);
    }
  };

  const reset = async () => {
    if (!userId) return;
    try {
      await api(`/agent/groups/${groupId}/bonds/${userId}`, { method: "DELETE" });
      message.success("关系已重置，之后会重新积累");
      onChanged();
      onClose();
    } catch (error) {
      fail(error);
    }
  };

  const addMoment = async () => {
    if (!userId || !momentDraft.trim()) return;
    try {
      await api(`/agent/groups/${groupId}/bonds/${userId}/moments`, { method: "POST", body: JSON.stringify({ content: momentDraft.trim() }) });
      setMomentDraft("");
      message.success("已记下这段经历");
      detail.reload();
      onChanged();
    } catch (error) {
      fail(error);
    }
  };

  const lockIcon = (field: string) => (bond && isLocked(bond, field) ? (
    <Tooltip title={readOnly ? "已锁定" : "已锁定，点击交还给自动整理"}>
      {readOnly
        ? <LockOutlined className="rel-bond-lock" />
        : <Button type="text" size="small" icon={<LockOutlined />} aria-label={`解锁${BOND_FIELD_LABELS[field]}`} onClick={() => void unlock(field)} />}
    </Tooltip>
  ) : null);

  const meta = bond ? stageMeta(bond.stage) : null;
  const ratio = bond ? initiativeRatio(bond) : null;

  return (
    <Drawer
      open={userId !== null}
      onClose={onClose}
      width={520}
      destroyOnHidden
      title={bond ? <span>Bot 和 {bond.name} 的关系</span> : "Bot 与成员的关系"}
      extra={bond && !readOnly && (
        <Space>
          {editing
            ? <><Button onClick={() => setEditing(false)}>取消</Button><Button type="primary" loading={saving} onClick={() => void save()}>保存</Button></>
            : <Button icon={<EditOutlined />} onClick={() => { form.setFieldsValue(formFrom(bond)); setEditing(true); }}>修改</Button>}
        </Space>
      )}
    >
      {detail.error && !bond ? <QueryErrorAlert error={detail.error} onRetry={detail.reload} /> : null}
      {!bond && !detail.error ? <div className="rg-loading-wrap"><Spin /></div> : null}
      {bond && meta && (
        <div className="rel-bond-detail">
          <div className="rel-bond-hero">
            <span className="rel-bond-stage is-large" style={{ "--rel-type-color": meta.color } as React.CSSProperties}>{meta.label}</span>
            <div className="rel-bond-hero-text">
              <Text strong>{bond.label || "还没有关系标签"}</Text>
              <Text type="secondary" className="rel-small">{meta.hint}</Text>
            </div>
            <Tooltip title="在成员关系里查看他"><Button size="small" icon={<TeamOutlined />} onClick={() => onOpenMember(bond.userId)} /></Tooltip>
          </div>
          <div className="rel-bond-meters">
            <div><Text className="rel-small">熟悉度 {Math.round(bond.familiarity * 100)}%</Text><Meter value={bond.familiarity} color={meta.color} label="熟悉度" /></div>
            {bond.affinity !== undefined && (
              <div><Text className="rel-small">好感 · {affinityWord(bond.affinity)} {lockIcon("affinity")}</Text><Meter value={(bond.affinity + 1) / 2} color="#f08a5d" label="好感" /></div>
            )}
            {bond.trust !== undefined && (
              <div><Text className="rel-small">信任 · {trustWord(bond.trust)} {lockIcon("trust")}</Text><Meter value={bond.trust} color="#4f9dde" label="信任" /></div>
            )}
          </div>

          {editing ? (
            <Form form={form} layout="vertical" className="rel-bond-form" requiredMark={false}>
              <Form.Item name="label" label="关系标签" extra="一句话，16 字以内，例如“爱逗我的老朋友”"><Input maxLength={16} allowClear /></Form.Item>
              <Space.Compact block>
                <Form.Item name="memberCallsBot" label="他怎么叫 Bot" className="rel-grow"><Input maxLength={12} allowClear /></Form.Item>
                <Form.Item name="botCallsMember" label="Bot 怎么叫他" className="rel-grow"><Input maxLength={12} allowClear /></Form.Item>
              </Space.Compact>
              <Form.Item name="tone" label="说话分寸" extra="第三人称描述，例如“可以互相吐槽”；像指令的内容会被拒绝"><Input maxLength={40} allowClear /></Form.Item>
              <Form.Item name="note" label="背景"><Input.TextArea maxLength={120} autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
              <Form.Item name="insideJokes" label="梗（最多 5 条）"><Select mode="tags" maxCount={5} tokenSeparators={["，", ","]} open={false} /></Form.Item>
              <Form.Item name="boundaries" label="边界：他不希望 Bot 做的事（最多 5 条）"><Select mode="tags" maxCount={5} tokenSeparators={["，", ","]} open={false} /></Form.Item>
              <Form.Item name="affinity" label="好感"><Slider min={-1} max={1} step={0.05} marks={{ [-1]: "反感", 0: "中性", 1: "亲近" }} /></Form.Item>
              <Form.Item name="trust" label="信任"><Slider min={0} max={1} step={0.05} marks={{ 0: "戒备", 1: "很信任" }} /></Form.Item>
              <Text type="secondary" className="rel-small">保存后改过的字段会锁定，自动整理只更新互动统计和没锁的字段。</Text>
            </Form>
          ) : (
            <Descriptions column={1} size="small" className="rel-bond-desc">
              <Descriptions.Item label={<>他怎么叫 Bot {lockIcon("memberCallsBot")}</>}>{bond.memberCallsBot || <Text type="secondary">—</Text>}</Descriptions.Item>
              <Descriptions.Item label={<>Bot 怎么叫他 {lockIcon("botCallsMember")}</>}>{bond.botCallsMember || <Text type="secondary">—</Text>}</Descriptions.Item>
              <Descriptions.Item label={<>说话分寸 {lockIcon("tone")}</>}>{bond.tone || <Text type="secondary">—</Text>}</Descriptions.Item>
              <Descriptions.Item label={<>背景 {lockIcon("note")}</>}>{bond.note || <Text type="secondary">—</Text>}</Descriptions.Item>
              <Descriptions.Item label={<>梗 {lockIcon("insideJokes")}</>}>
                {(bond.insideJokes ?? []).length ? (bond.insideJokes ?? []).map((item) => <Tag key={item}>{item}</Tag>) : <Text type="secondary">—</Text>}
              </Descriptions.Item>
              <Descriptions.Item label={<>边界 {lockIcon("boundaries")}</>}>
                {(bond.boundaries ?? []).length ? (bond.boundaries ?? []).map((item) => <Tag key={item} color="orange">{item}</Tag>) : <Text type="secondary">—</Text>}
              </Descriptions.Item>
            </Descriptions>
          )}

          {bond.memberToBotCount !== undefined && (
            <section className="rel-bond-section">
              <Text strong>互动统计</Text>
              <div className="rel-member-card-stats rel-bond-stats">
                <span><strong>{bond.memberToBotCount}</strong> 次找 Bot</span>
                <span><strong>{bond.botToMemberCount}</strong> 次 Bot 回应</span>
                <span><strong>{bond.exchangeCount}</strong> 轮来回</span>
                <span><strong>{bond.activeDays}</strong> 天来往</span>
                {ratio !== null && <span><strong>{Math.round(ratio * 100)}%</strong> 由他发起</span>}
                <span><strong>{bond.positiveCount}</strong> 次愉快 / <strong>{bond.negativeCount}</strong> 次不快</span>
              </div>
              <Text type="secondary" className="rel-small">
                认识 {bond.knownDays ?? "—"} 天 · 最近一次 {relativeTime(bond.lastInteractionAt)} · 来源 {SOURCE_LABEL[bond.sourceKind ?? ""] ?? bond.sourceKind}
                {bond.lastModelUpdateAt ? ` · 模型最近更新 ${formatTime(bond.lastModelUpdateAt)}` : ""}
              </Text>
            </section>
          )}

          {bond.moments !== undefined && (
            <section className="rel-bond-section">
              <Text strong>共同经历</Text>
              {bond.moments.length === 0
                ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有值得记住的事" />
                : (
                  <Timeline
                    className="rel-bond-timeline"
                    items={bond.moments.map((moment) => ({
                      key: moment.id,
                      color: moment.sourceKind === "auto" ? "gray" : "pink",
                      children: (
                        <div>
                          <Paragraph className="rel-bond-moment">{moment.content}</Paragraph>
                          <Text type="secondary" className="rel-small">
                            {formatTime(moment.updatedAt)} · {SOURCE_LABEL[moment.sourceKind] ?? moment.sourceKind}
                            {moment.accessCount ? ` · 被想起 ${moment.accessCount} 次` : ""}
                          </Text>
                        </div>
                      ),
                    }))}
                  />
                )}
              {!readOnly && (
                <Space.Compact block>
                  <Input placeholder="手动补一段经历" maxLength={120} value={momentDraft} onChange={(event) => setMomentDraft(event.target.value)} onPressEnter={() => void addMoment()} />
                  <Button icon={<PlusOutlined />} disabled={!momentDraft.trim()} onClick={() => void addMoment()}>记下</Button>
                </Space.Compact>
              )}
            </section>
          )}

          {bond.evidence !== undefined && (
            <section className="rel-bond-section">
              <Text strong>最近的互动证据</Text>
              {bond.evidence.length === 0
                ? <Text type="secondary" className="rel-small">原始消息已过保留期</Text>
                : bond.evidence.map((item) => (
                  <div key={item.id} className="rel-bond-evidence">
                    <Text type="secondary" className="rel-small">{formatTime(item.receivedAt)}</Text>
                    <span className="rel-ellipsis">{item.text}</span>
                  </div>
                ))}
              {bond.evidenceMissing ? <Text type="secondary" className="rel-small">另有 {bond.evidenceMissing} 条原文已过保留期</Text> : null}
            </section>
          )}

          {!readOnly && (
            <section className="rel-bond-section rel-bond-danger">
              <Popconfirm
                title="重置 Bot 和他的关系？"
                description="会删除关系记录和自动、本人写的共同经历，之后从零开始积累。"
                okText="重置"
                okButtonProps={{ danger: true }}
                onConfirm={() => void reset()}
              >
                <Button danger icon={<DeleteOutlined />}>重置关系</Button>
              </Popconfirm>
              {(bond.lockedFields ?? []).length > 0 && (
                <Text type="secondary" className="rel-small"><UnlockOutlined /> 点锁形图标可以把单个字段交还给自动整理</Text>
              )}
            </section>
          )}
        </div>
      )}
    </Drawer>
  );
}
