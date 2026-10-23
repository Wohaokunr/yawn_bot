// 关系边编辑器：新增与编辑共用一个抽屉。
// 新增：成员搜索选择、两端互换、类型色块快选、置信度滑杆与档位、一句话预览、重复检测、可选同时建立反向边。
// 编辑：后端只允许改备注/置信度；改类型或反转方向时由前端“先建新边、再删旧边”完成，失败不会丢旧边。
import { SwapOutlined } from "@ant-design/icons";
import {
  Alert, App as AntApp, AutoComplete, Button, Checkbox, Drawer, Form, Input, InputNumber,
  Segmented, Slider, Space, Tag, Typography,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { RELATION_TYPE_PRESETS } from "../agent-meta";
import { relationTypeColor } from "../relation-meta";
import type { AgentRelationItem, AgentRelationListItem } from "../types";
import { MemberPicker } from "./MemberPicker";
import { CONFIDENCE_TIER_LABEL, confidenceTier, relationSentence, SYMMETRIC_RELATION_TYPES } from "./relation-analysis";
import type { RelationCreatePrefill } from "./relations-context";

const { Text } = Typography;

const CONFIDENCE_PRESETS = [
  { value: 0.95, label: "非常确定" },
  { value: 0.8, label: "确定" },
  { value: 0.6, label: "较可信" },
  { value: 0.3, label: "存疑" },
];

interface CreateValues {
  subjectUserId?: string;
  objectUserId?: string;
  type?: string;
  note?: string;
  confidence: number;
  alsoReverse: boolean;
}

interface EditValues {
  type: string;
  note: string;
  confidence: number;
  reversed: boolean;
}

function ConfidenceField({ value, onChange }: { value?: number; onChange?: (value: number) => void }): React.JSX.Element {
  const current = value ?? 0.8;
  const tier = confidenceTier(current);
  return (
    <div className="rel-confidence-field">
      <div className="rel-confidence-row">
        <Slider
          min={0}
          max={1}
          step={0.05}
          value={current}
          onChange={(next) => onChange?.(Number(next))}
          tooltip={{ formatter: (next) => `${Math.round(Number(next) * 100)}%` }}
          className={`rel-confidence-slider is-${tier}`}
        />
        <InputNumber min={0} max={1} step={0.05} value={current} onChange={(next) => onChange?.(Number(next ?? 0))} style={{ width: 84 }} aria-label="置信度数值" />
      </div>
      <Space size={6} wrap>
        {CONFIDENCE_PRESETS.map((preset) => (
          <Tag.CheckableTag key={preset.value} checked={Math.abs(current - preset.value) < 0.001} onChange={() => onChange?.(preset.value)}>
            {preset.label} {Math.round(preset.value * 100)}%
          </Tag.CheckableTag>
        ))}
        {!CONFIDENCE_PRESETS.some((preset) => Math.abs(current - preset.value) < 0.001) && (
          <Text type="secondary" className="rel-small">自定义 · {CONFIDENCE_TIER_LABEL[tier]}</Text>
        )}
      </Space>
    </div>
  );
}

function TypeQuickPick({ value, onPick }: { value?: string; onPick: (type: string) => void }): React.JSX.Element {
  return (
    <div className="rel-type-picks" role="group" aria-label="常用关系类型">
      {RELATION_TYPE_PRESETS.map((type) => (
        <button
          key={type}
          type="button"
          className={`rel-type-pick${value === type ? " is-active" : ""}`}
          style={{ "--rel-type-color": relationTypeColor(type) } as React.CSSProperties}
          onClick={() => onPick(type)}
          aria-pressed={value === type}
        >
          <span className="rel-type-dot" />{type}
        </button>
      ))}
    </div>
  );
}

// 编辑器作为受控抽屉：mode=create 时 prefill 生效，mode=edit 时 editing 为当前边。
export function RelationEditorDrawer({
  groupId,
  mode,
  open,
  prefill,
  editing,
  typeOptions,
  nameOf,
  onClose,
  onSaved,
}: {
  groupId: string;
  mode: "create" | "edit";
  open: boolean;
  prefill?: RelationCreatePrefill;
  editing: AgentRelationItem | null;
  typeOptions: string[];
  nameOf: (userId: string) => string;
  onClose: () => void;
  onSaved: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [createForm] = Form.useForm<CreateValues>();
  const [editForm] = Form.useForm<EditValues>();
  const [saving, setSaving] = useState(false);
  const [duplicate, setDuplicate] = useState<AgentRelationListItem | null>(null);
  const [keepOpen, setKeepOpen] = useState(false);
  const subject = Form.useWatch("subjectUserId", createForm);
  const object = Form.useWatch("objectUserId", createForm);
  const type = Form.useWatch("type", createForm);
  const editType = Form.useWatch("type", editForm);
  const editReversed = Form.useWatch("reversed", editForm);
  const autoOptions = useMemo(
    () => Array.from(new Set([...RELATION_TYPE_PRESETS, ...typeOptions])).map((value) => ({ value, label: value })),
    [typeOptions],
  );

  useEffect(() => {
    if (!open) return;
    if (mode === "create") {
      createForm.resetFields();
      createForm.setFieldsValue({
        subjectUserId: prefill?.subjectUserId,
        objectUserId: prefill?.objectUserId,
        type: prefill?.type,
        note: prefill?.note ?? "",
        confidence: prefill?.confidence ?? 0.8,
        alsoReverse: prefill?.type ? SYMMETRIC_RELATION_TYPES.has(prefill.type) : false,
      });
      setDuplicate(null);
    } else if (editing) {
      editForm.setFieldsValue({ type: editing.type, note: editing.note, confidence: editing.confidence, reversed: false });
    }
  }, [open, mode, prefill, editing, createForm, editForm]);

  // 选好两端与类型后实时查重，提前提示 409 冲突而不是等提交失败。
  useEffect(() => {
    if (mode !== "create" || !open || !subject || !object || !type) {
      setDuplicate(null);
      return undefined;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      api<AgentRelationListItem[]>(
        `/agent/groups/${groupId}/relations?page=1&pageSize=100&member=${encodeURIComponent(subject)}&type=${encodeURIComponent(type.trim())}`,
        { signal: controller.signal },
      )
        .then((response) => setDuplicate(response.data.find((row) => row.subjectUserId === subject && row.objectUserId === object) ?? null))
        .catch(() => undefined);
    }, 250);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [mode, open, groupId, subject, object, type]);

  const post = (body: { subjectUserId: number; objectUserId: number; type: string; note: string; confidence: number }) =>
    api<AgentRelationItem>(`/agent/groups/${groupId}/relations`, { method: "POST", body: JSON.stringify(body) });

  const submitCreate = async (values: CreateValues) => {
    if (!values.subjectUserId || !values.objectUserId || !values.type) return;
    setSaving(true);
    const body = {
      subjectUserId: Number(values.subjectUserId),
      objectUserId: Number(values.objectUserId),
      type: values.type.trim(),
      note: (values.note ?? "").trim(),
      confidence: values.confidence,
    };
    try {
      await post(body);
      let reverseNote = "";
      if (values.alsoReverse) {
        try {
          await post({ ...body, subjectUserId: body.objectUserId, objectUserId: body.subjectUserId });
          reverseNote = "，反向关系也已建立";
        } catch (error) {
          reverseNote = error instanceof ApiError && error.status === 409 ? "，反向关系原本就存在" : `，但反向关系失败：${(error as Error).message}`;
        }
      }
      message.success(`关系边已新增${reverseNote}`);
      onSaved();
      if (keepOpen) {
        // 连续录入：保留主体与类型，清空客体与备注，焦点留在抽屉里。
        createForm.setFieldsValue({ objectUserId: undefined, note: "" });
        setDuplicate(null);
      } else {
        onClose();
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) message.warning(error.message);
      else message.error((error as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const submitEdit = async (values: EditValues) => {
    if (!editing) return;
    setSaving(true);
    const nextType = values.type.trim();
    const identityChanged = nextType !== editing.type || values.reversed;
    try {
      if (identityChanged) {
        const subjectId = values.reversed ? editing.objectUserId : editing.subjectUserId;
        const objectId = values.reversed ? editing.subjectUserId : editing.objectUserId;
        await post({ subjectUserId: Number(subjectId), objectUserId: Number(objectId), type: nextType, note: values.note.trim(), confidence: values.confidence });
        await api(`/agent/groups/${groupId}/relations/${editing.id}`, { method: "DELETE" });
        message.success("关系边已按新类型/方向重建");
      } else {
        await api<AgentRelationItem>(`/agent/groups/${groupId}/relations/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify({ note: values.note, confidence: values.confidence }),
        });
        message.success("关系边已更新");
      }
      onSaved();
      onClose();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) message.warning("目标关系边已存在，原关系未改动");
      else message.error((error as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const submitOnCtrlEnter = (event: React.KeyboardEvent) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      (mode === "create" ? createForm : editForm).submit();
    }
  };

  const previewSubject = subject ? nameOf(subject) : "主体";
  const previewObject = object ? nameOf(object) : "客体";
  const editSubject = editing ? (editReversed ? editing.objectUserId : editing.subjectUserId) : "";
  const editObject = editing ? (editReversed ? editing.subjectUserId : editing.objectUserId) : "";
  const editIdentityChanged = !!editing && ((editType ?? editing.type).trim() !== editing.type || !!editReversed);

  return (
    <Drawer
      className={`relations-editor-drawer ${mode === "create" ? "relations-create-drawer" : "relations-edit-drawer"}`}
      open={open}
      width={520}
      title={mode === "create" ? "新增关系边" : `编辑关系边 · ${editing?.type ?? ""}`}
      onClose={onClose}
      destroyOnHidden={false}
      afterOpenChange={(visible) => {
        if (!visible) return;
        window.requestAnimationFrame(() => {
          const selector = mode === "create" ? ".relations-create-drawer" : ".relations-edit-drawer";
          document.querySelector<HTMLElement>(`${selector} input, ${selector} textarea`)?.focus();
        });
      }}
      footer={
        <div className="rel-drawer-footer">
          <Text type="secondary" className="rel-small">Ctrl + Enter 保存</Text>
          <Space>
            {mode === "create" && <Checkbox checked={keepOpen} onChange={(event) => setKeepOpen(event.target.checked)}>连续新增</Checkbox>}
            <Button onClick={onClose}>取消</Button>
            <Button
              type="primary"
              loading={saving}
              onClick={() => (mode === "create" ? createForm : editForm).submit()}
            >
              {mode === "create" ? "新增" : editIdentityChanged ? "重建关系" : "保存"}
            </Button>
          </Space>
        </div>
      }
    >
      {mode === "create" ? (
        <Form form={createForm} layout="vertical" onFinish={submitCreate} onKeyDown={submitOnCtrlEnter} initialValues={{ confidence: 0.8, alsoReverse: false }}>
          <div className="rel-endpoint-row">
            <Form.Item name="subjectUserId" label="主体" rules={[{ required: true, message: "请选择主体成员" }]}>
              <MemberPicker groupId={groupId} placeholder="昵称或 QQ" exclude={object} nameOf={nameOf} />
            </Form.Item>
            <Button
              className="rel-swap-btn"
              icon={<SwapOutlined />}
              aria-label="互换主体与客体"
              title="互换主体与客体"
              onClick={() => createForm.setFieldsValue({ subjectUserId: object, objectUserId: subject })}
            />
            <Form.Item name="objectUserId" label="客体" rules={[{ required: true, message: "请选择客体成员" }]}>
              <MemberPicker groupId={groupId} placeholder="昵称或 QQ" exclude={subject} nameOf={nameOf} />
            </Form.Item>
          </div>
          <Form.Item name="type" label="类型" rules={[{ required: true, whitespace: true, message: "请选择或输入类型" }]} extra={<TypeQuickPick value={type} onPick={(picked) => createForm.setFieldsValue({ type: picked, alsoReverse: SYMMETRIC_RELATION_TYPES.has(picked) })} />}>
            <AutoComplete
              options={autoOptions}
              placeholder="点下方色块，或输入自定义类型"
              filterOption={(input, option) => String(option?.value ?? "").includes(input)}
              maxLength={32}
            />
          </Form.Item>
          <Form.Item name="confidence" label="置信度" rules={[{ required: true }]}>
            <ConfidenceField />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={200} showCount placeholder="一句话关系背景（可选），例如：大学室友 / 一起开黑" />
          </Form.Item>
          <Form.Item name="alsoReverse" valuePropName="checked" extra="好友、同学等通常是双向的；勾选后会再建一条方向相反、内容相同的边。">
            <Checkbox>同时建立反向关系（{previewObject} → {previewSubject}）</Checkbox>
          </Form.Item>
          <div className="rel-preview" aria-live="polite">
            <span className="rel-preview-label">预览</span>
            <span className="rel-preview-text">{relationSentence(previewSubject, type?.trim() || "（类型）", previewObject)}</span>
          </div>
          {duplicate && (
            <Alert
              className="section-alert"
              type="warning"
              showIcon
              message="这条关系已存在"
              description={`现有记录置信度 ${Math.round(duplicate.confidence * 100)}%${duplicate.note ? `，备注“${duplicate.note}”` : ""}。如需调整请改用编辑。`}
            />
          )}
        </Form>
      ) : editing ? (
        <Form form={editForm} layout="vertical" onFinish={submitEdit} onKeyDown={submitOnCtrlEnter}>
          <div className="rel-edit-identity">
            <span className="rel-endpoint-chip">{nameOf(editSubject)}</span>
            <Tag color={relationTypeColor((editType ?? editing.type).trim() || editing.type)}>{(editType ?? editing.type).trim() || editing.type}</Tag>
            <span className="rel-arrow" aria-hidden="true">→</span>
            <span className="rel-endpoint-chip">{nameOf(editObject)}</span>
          </div>
          <Form.Item name="type" label="类型" rules={[{ required: true, whitespace: true, message: "类型不能为空" }]}>
            <AutoComplete options={autoOptions} maxLength={32} filterOption={(input, option) => String(option?.value ?? "").includes(input)} />
          </Form.Item>
          <Form.Item name="reversed" label="方向">
            <Segmented
              options={[
                { value: false, label: "保持原方向" },
                { value: true, label: "反转方向" },
              ] as Array<{ value: boolean; label: string }>}
            />
          </Form.Item>
          {editIdentityChanged && (
            <Alert className="section-alert" type="info" showIcon message="类型和方向是关系边的身份，保存时会新建一条边并删除原边；证据计数从 1 重新开始。" />
          )}
          <Form.Item name="confidence" label="置信度" rules={[{ required: true }]}>
            <ConfidenceField />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={200} showCount />
          </Form.Item>
        </Form>
      ) : null}
    </Drawer>
  );
}
