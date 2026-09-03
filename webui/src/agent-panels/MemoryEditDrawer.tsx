import { Button, Col, Drawer, Form, Input, InputNumber, Row, Space } from "antd";
import { useEffect } from "react";
import type { MemoryItem } from "../types";

export interface MemoryFormValues {
  content: string;
  salience: number;
  confidence: number;
  expiresInDays: number | null;
}

export function MemoryEditDrawer({
  memory,
  saving,
  onClose,
  onSave,
}: {
  memory: MemoryItem | null;
  saving: boolean;
  onClose: () => void;
  onSave: (values: MemoryFormValues) => void;
}): React.JSX.Element {
  const [form] = Form.useForm();
  useEffect(() => {
    if (memory) {
      form.setFieldsValue({
        content: memory.content,
        salience: memory.salience,
        confidence: memory.confidence,
        expiresInDays: memory.expiresAt
          ? Math.max(1, Math.ceil((new Date(memory.expiresAt).getTime() - Date.now()) / 86400000))
          : null,
      });
    }
  }, [form, memory]);

  return <Drawer open={!!memory} width={520} title={`编辑记忆 · ${memory?.key ?? ""}`} onClose={onClose}>
    <Form form={form} layout="vertical" onFinish={(values) => onSave(values as MemoryFormValues)}>
      <Form.Item name="content" label="内容" rules={[{ required: true, message: "请输入内容" }]}>
        <Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} maxLength={2000} showCount />
      </Form.Item>
      <Row gutter={16}>
        <Col span={12}>
          <Form.Item name="salience" label="显著度（注入优先级）" rules={[{ required: true }]}>
            <InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item name="confidence" label="置信度" rules={[{ required: true }]}>
            <InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} />
          </Form.Item>
        </Col>
      </Row>
      <Form.Item name="expiresInDays" label="有效期（天）">
        <InputNumber min={1} max={3650} placeholder="清空则永久有效" style={{ width: "100%" }} />
      </Form.Item>
      <Space>
        <Button type="primary" htmlType="submit" loading={saving}>保存</Button>
        <Button onClick={onClose}>取消</Button>
      </Space>
    </Form>
  </Drawer>;
}
