import {
  KeyOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntdApp,
  Button,
  Card,
  Flex,
  Input,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import { useCallback, useState } from "react";
import { api } from "./api";
import { formatTime, PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import type { GroupSummary } from "./types";

const { Paragraph, Text, Title } = Typography;

interface GuestAccessPolicy {
  enabled: boolean;
  credentialConfigured: boolean;
  credentialVersion: number;
  authorizedGroupCount: number;
  updatedAt?: string | null;
}

interface GuestAccessCredential extends GuestAccessPolicy {
  credential: string;
}

interface GuestAccessGroup extends GroupSummary {
  guestAllowed: boolean;
}

export function GuestAccessPage(): React.JSX.Element {
  const { message } = AntdApp.useApp();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [policySaving, setPolicySaving] = useState(false);
  const [credentialSaving, setCredentialSaving] = useState(false);
  const [savingGroup, setSavingGroup] = useState("");
  const [oneTimeCredential, setOneTimeCredential] = useState("");

  const loadPolicy = useCallback(() => api<GuestAccessPolicy>("/guest-access").then((r) => r.data), []);
  const policy = useApiQuery(loadPolicy, { resources: ["guest_access"] });

  const loadGroups = useCallback(
    () => api<GuestAccessGroup[]>(
      `/guest-access/groups?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`,
    ).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })),
    [page, search],
  );
  const groups = useApiQuery(loadGroups, { resources: ["guest_access"] });

  const setEnabled = async (enabled: boolean) => {
    setPolicySaving(true);
    try {
      await api<GuestAccessPolicy>("/guest-access", {
        method: "PATCH",
        body: JSON.stringify({ enabled }),
      });
      void message.success(enabled ? "访客登录已开启" : "访客登录已关闭，旧会话已失效");
      policy.reload();
    } catch (reason) {
      void message.error(reason instanceof Error ? reason.message : "保存访客策略失败");
    } finally {
      setPolicySaving(false);
    }
  };

  const rotateCredential = async () => {
    setCredentialSaving(true);
    try {
      const { data } = await api<GuestAccessCredential>("/guest-access/credential", { method: "POST" });
      setOneTimeCredential(data.credential);
      policy.reload();
      void message.success(data.credentialVersion > 1 ? "访问码已轮换，旧访客会话已失效" : "访客访问码已生成");
    } catch (reason) {
      void message.error(reason instanceof Error ? reason.message : "生成访问码失败");
    } finally {
      setCredentialSaving(false);
    }
  };

  const setGroupAllowed = async (groupId: string, allowed: boolean) => {
    setSavingGroup(groupId);
    try {
      await api(`/guest-access/groups/${groupId}`, {
        method: "PATCH",
        body: JSON.stringify({ allowed }),
      });
      groups.reload();
      policy.reload();
    } catch (reason) {
      void message.error(reason instanceof Error ? reason.message : "修改群授权失败");
    } finally {
      setSavingGroup("");
    }
  };

  const current = policy.data;

  return <>
    <PageHeader
      title="访客访问"
      subtitle="用独立访问码开放指定群聊的只读数据；不会复用或暴露运维 Token"
      onRefresh={() => { policy.reload(); groups.reload(); }}
      refreshing={policy.refreshing || groups.refreshing}
      status={current && (
        <Tag color={current.enabled ? "green" : "default"}>
          {current.enabled ? "访客登录已开启" : "访客登录已关闭"}
        </Tag>
      )}
    />

    {policy.error && !policy.data
      ? <QueryErrorAlert error={policy.error} onRetry={policy.reload} />
      : <div className="guest-access-grid">
        <Card className="guest-access-policy-card">
          <Flex justify="space-between" align="flex-start" gap={20} wrap>
            <Space direction="vertical" size={6}>
              <Space>
                <SafetyCertificateOutlined />
                <Title level={4} style={{ margin: 0 }}>访问总开关</Title>
              </Space>
              <Text type="secondary">
                关闭会立即撤销所有访客 Session；再次开启时，关闭前的 Session 也不会恢复。
              </Text>
              <Space wrap>
                <Tag>凭据版本 v{current?.credentialVersion ?? 0}</Tag>
                <Tag color="blue">已开放 {current?.authorizedGroupCount ?? 0} 个群</Tag>
                <Text type="secondary">最近修改 {formatTime(current?.updatedAt)}</Text>
              </Space>
            </Space>
            {current?.enabled ? (
              <Popconfirm
                title="关闭访客登录？"
                description="现有访客会话会立即失效。"
                okText="关闭并撤销会话"
                cancelText="取消"
                onConfirm={() => void setEnabled(false)}
              >
                <Switch checked loading={policySaving} checkedChildren="开启" unCheckedChildren="关闭" />
              </Popconfirm>
            ) : (
              <Switch
                checked={false}
                loading={policySaving}
                checkedChildren="开启"
                unCheckedChildren="关闭"
                onChange={() => void setEnabled(true)}
              />
            )}
          </Flex>
        </Card>

        <Card className="guest-access-credential-card">
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <Space>
              <KeyOutlined />
              <Title level={4} style={{ margin: 0 }}>访客访问码</Title>
              <Tag color={current?.credentialConfigured ? "green" : "orange"}>
                {current?.credentialConfigured ? "已配置" : "尚未生成"}
              </Tag>
            </Space>
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              访问码由高熵随机数生成，数据库只保存 SHA-256 摘要。明文只在生成或轮换成功后显示一次。
            </Paragraph>
            {current?.credentialConfigured ? (
              <Popconfirm
                title="轮换访客访问码？"
                description="旧访问码不能再登录，现有访客 Session 也会立即失效。"
                okText="确认轮换"
                cancelText="取消"
                onConfirm={() => void rotateCredential()}
              >
                <Button icon={<KeyOutlined />} loading={credentialSaving}>轮换访问码</Button>
              </Popconfirm>
            ) : (
              <Button type="primary" icon={<KeyOutlined />} loading={credentialSaving} onClick={() => void rotateCredential()}>
                生成访问码
              </Button>
            )}
          </Space>
        </Card>
      </div>}

    <Card
      title={<Space><TeamOutlined />开放群聊</Space>}
      extra={(
        <Input.Search
          placeholder="搜索群名或群号"
          allowClear
          onSearch={(value) => {
            setSearch(value);
            setPage(1);
          }}
        />
      )}
    >
      {groups.error && !groups.data
        ? <QueryErrorAlert error={groups.error} onRetry={groups.reload} />
        : <Table
          rowKey="groupId"
          loading={groups.loading}
          dataSource={groups.data?.rows ?? []}
          pagination={{
            current: page,
            pageSize: 20,
            total: groups.data?.total ?? 0,
            showSizeChanger: false,
            onChange: setPage,
          }}
          columns={[
            {
              title: "群聊",
              render: (_, row: GuestAccessGroup) => <>
                <Text strong>{row.groupName || "未命名群"}</Text>
                <br />
                <Text type="secondary" copyable>{row.groupId}</Text>
              </>,
            },
            { title: "成员", dataIndex: "memberCount", width: 100 },
            { title: "最近活跃", dataIndex: "lastActiveAt", render: formatTime, width: 190 },
            {
              title: "允许访客查看",
              width: 150,
              render: (_, row: GuestAccessGroup) => (
                <Switch
                  checked={row.guestAllowed}
                  loading={savingGroup === row.groupId}
                  onChange={(checked) => void setGroupAllowed(row.groupId, checked)}
                />
              ),
            },
          ]}
        />}
    </Card>

    <Modal
      open={Boolean(oneTimeCredential)}
      title="访客访问码已生成"
      okText="我已保存"
      cancelButtonProps={{ style: { display: "none" } }}
      closable={false}
      maskClosable={false}
      onOk={() => setOneTimeCredential("")}
    >
      <Alert
        type="warning"
        showIcon
        message="此访问码只显示这一次"
        description="关闭窗口后无法从数据库恢复明文；遗失时请直接轮换新访问码。"
      />
      <Paragraph className="guest-access-one-time-code" copyable={{ text: oneTimeCredential }}>
        {oneTimeCredential}
      </Paragraph>
    </Modal>
  </>;
}
