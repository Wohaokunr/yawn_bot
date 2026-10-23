import {
  App as AntApp, Button, Checkbox, Empty, Input, Popconfirm, Space, Spin, Tag, Typography,
} from "antd";
import { useState } from "react";
import { api, ApiError } from "../api";
import { QueryErrorAlert, useApiQuery } from "../shared";
import type { Persona, PersonaCard, PersonaTemplate } from "../types";

const { Text } = Typography;

// 人设库与群无关：所有群共享同一份模板列表，其中最多一个是全局默认人设。
export function PersonaLibrary({
  groupId,
  draft,
  version,
  dirty,
  onApplied,
}: {
  groupId: string;
  draft: () => PersonaCard;
  version: () => string | undefined;
  dirty: boolean;
  onApplied: (persona: Persona) => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [name, setName] = useState("");
  const [makeGlobal, setMakeGlobal] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const query = useApiQuery({
    queryKey: ["agent-persona-library"],
    fetcher: (signal) => api<PersonaTemplate[]>("/agent/persona-library", { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_persona_library"] },
  });

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    try {
      await action();
      query.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        message.warning(error.message);
        query.reload();
      } else message.error((error as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const saveDraft = () => run("create", async () => {
    const cleaned = name.trim();
    if (!cleaned) {
      message.warning("先给这张人设起个名字");
      return;
    }
    await api<PersonaTemplate>("/agent/persona-library", {
      method: "POST",
      body: JSON.stringify({ name: cleaned, card: draft(), isGlobal: makeGlobal }),
    });
    message.success(makeGlobal ? `已存入人设库，并设为全局人设「${cleaned}」` : `已存入人设库「${cleaned}」`);
    setName("");
    setMakeGlobal(false);
  });

  const setGlobal = (item: PersonaTemplate, isGlobal: boolean) => run(`global-${item.id}`, async () => {
    await api<PersonaTemplate>(`/agent/persona-library/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({ version: item.version, isGlobal }),
    });
    message.success(isGlobal ? `「${item.name}」已成为全局人设` : "已取消全局人设，跟随全局的群回到内置默认");
  });

  const overwrite = (item: PersonaTemplate) => run(`overwrite-${item.id}`, async () => {
    await api<PersonaTemplate>(`/agent/persona-library/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({ version: item.version, card: draft() }),
    });
    message.success(`已用当前草稿覆盖「${item.name}」`);
  });

  const apply = (item: PersonaTemplate) => run(`apply-${item.id}`, async () => {
    const result = await api<Persona>(`/agent/groups/${groupId}/persona/apply-template`, {
      method: "POST",
      body: JSON.stringify({ templateId: Number(item.id), version: version() ?? null }),
    });
    onApplied(result.data);
    message.success(`本群已改用「${item.name}」`);
  });

  const remove = (item: PersonaTemplate) => run(`delete-${item.id}`, async () => {
    await api(`/agent/persona-library/${item.id}`, {
      method: "DELETE",
      headers: { "If-Match": item.version },
    });
    message.success(item.isGlobal ? `已删除「${item.name}」，全局人设回到内置默认` : `已删除「${item.name}」`);
  });

  const needle = keyword.trim().toLowerCase();
  const items = query.data?.filter((item) => !needle || `${item.name} ${item.summary}`.toLowerCase().includes(needle));
  return (
    <div className="persona-library">
      <div className="persona-library-create">
        <Input
          value={name}
          onChange={(event) => setName(event.target.value)}
          maxLength={32}
          placeholder="给当前草稿起个名字，例如：傲娇猫娘"
          onPressEnter={saveDraft}
        />
        <Checkbox checked={makeGlobal} onChange={(event) => setMakeGlobal(event.target.checked)}>设为全局人设</Checkbox>
        <Button type="primary" ghost loading={busy === "create"} onClick={saveDraft}>存入人设库</Button>
      </div>
      {(query.data?.length ?? 0) > 4 && (
        <Input.Search allowClear value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索人设库" />
      )}
      {!items
        ? (query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />)
        : items.length === 0
          ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={needle ? "没有匹配的人设" : "人设库还是空的。把调好的草稿存进来，其它群就能一键套用。"} />
          : (
            <div className="persona-library-list">
              {items.map((item) => (
                <div key={item.id} className={`persona-library-item${item.isGlobal ? " is-global" : ""}`}>
                  <div className="persona-library-copy">
                    <Space size={6} wrap>
                      <strong>{item.name}</strong>
                      {item.isGlobal && <Tag color="magenta">全局人设</Tag>}
                    </Space>
                    <Text type="secondary">{item.summary}</Text>
                  </div>
                  <Space size={4} wrap className="persona-library-actions">
                    {dirty
                      ? (
                        <Popconfirm title={`用「${item.name}」替换本群人设？`} description="本群还有未保存的草稿，会被覆盖。" okText="替换" cancelText="取消" onConfirm={() => apply(item)}>
                          <Button size="small" type="primary" loading={busy === `apply-${item.id}`}>用于本群</Button>
                        </Popconfirm>
                      )
                      : <Button size="small" type="primary" loading={busy === `apply-${item.id}`} onClick={() => apply(item)}>用于本群</Button>}
                    <Button size="small" loading={busy === `global-${item.id}`} onClick={() => setGlobal(item, !item.isGlobal)}>
                      {item.isGlobal ? "取消全局" : "设为全局"}
                    </Button>
                    <Popconfirm title={`用当前草稿覆盖「${item.name}」？`} okText="覆盖" cancelText="取消" onConfirm={() => overwrite(item)}>
                      <Button size="small" loading={busy === `overwrite-${item.id}`}>用草稿覆盖</Button>
                    </Popconfirm>
                    <Popconfirm
                      title={`删除「${item.name}」？`}
                      description={item.isGlobal ? "它是全局人设，删除后跟随全局的群会回到内置默认。" : "已经套用到群里的人设不受影响。"}
                      okText="删除"
                      okButtonProps={{ danger: true }}
                      cancelText="取消"
                      onConfirm={() => remove(item)}
                    >
                      <Button size="small" danger loading={busy === `delete-${item.id}`}>删除</Button>
                    </Popconfirm>
                  </Space>
                </div>
              ))}
            </div>
          )}
    </div>
  );
}
