import { CopyOutlined, DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Modal, Select, Space } from "antd";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { AgentConfig, GroupSummary } from "../types";
import { downloadText, parseConfigImport, pickPortableConfig, type PortableAgentConfig, serializeConfigExport } from "./config-transfer";

// 运行配置的导出 / 导入 / 从其它群复制：都只填进表单草稿，管理员确认后再保存。
export function ConfigTransferBar({
  groupId,
  getValues,
  onApply,
}: {
  groupId: string;
  getValues: () => Record<string, unknown>;
  onApply: (values: PortableAgentConfig, source: string) => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const fileRef = useRef<HTMLInputElement>(null);
  const [copyOpen, setCopyOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [groups, setGroups] = useState<GroupSummary[]>([]);
  const [selected, setSelected] = useState<string | undefined>();
  const [copying, setCopying] = useState(false);

  useEffect(() => {
    if (!copyOpen) return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      api<GroupSummary[]>(`/groups?page=1&pageSize=20&search=${encodeURIComponent(search)}`, { signal: controller.signal })
        .then((response) => setGroups(response.data.filter((row) => row.groupId !== groupId)))
        .catch(() => undefined);
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [copyOpen, groupId, search]);

  const exportConfig = () => {
    downloadText(`agent-config-${groupId}.json`, serializeConfigExport(groupId, getValues()));
  };

  const importFile = async (file: File | undefined) => {
    if (!file) return;
    try {
      onApply(parseConfigImport(await file.text()), `文件 ${file.name}`);
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const copyFromGroup = async () => {
    if (!selected) return;
    setCopying(true);
    try {
      const response = await api<AgentConfig>(`/agent/groups/${selected}/config`);
      onApply(pickPortableConfig(response.data as unknown as Record<string, unknown>), `群 ${selected}`);
      setCopyOpen(false);
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setCopying(false);
    }
  };

  return (
    <>
      <Space wrap size={6} className="agent-config-transfer">
        <Button size="small" icon={<DownloadOutlined />} onClick={exportConfig}>导出</Button>
        <Button size="small" icon={<UploadOutlined />} onClick={() => fileRef.current?.click()}>导入</Button>
        <Button size="small" icon={<CopyOutlined />} onClick={() => setCopyOpen(true)}>从其它群复制</Button>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          style={{ display: "none" }}
          aria-label="导入配置文件"
          onChange={(event) => void importFile(event.target.files?.[0])}
        />
      </Space>
      <Modal
        open={copyOpen}
        title="从其它群复制运行配置"
        okText="填入草稿"
        okButtonProps={{ disabled: !selected, loading: copying }}
        onOk={() => void copyFromGroup()}
        onCancel={() => setCopyOpen(false)}
        destroyOnHidden
      >
        <p className="agent-config-transfer-help">复制触发方式、主动参与、记忆边界与工具权限；总开关保持不变，填入后需要点“保存配置”才会生效。</p>
        <Select
          showSearch
          style={{ width: "100%" }}
          placeholder="搜索群名或群号"
          filterOption={false}
          onSearch={setSearch}
          value={selected}
          onChange={setSelected}
          options={groups.map((row) => ({ value: row.groupId, label: `${row.groupName || "未命名群"}（${row.groupId}）` }))}
        />
      </Modal>
    </>
  );
}
