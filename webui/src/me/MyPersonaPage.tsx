import { LockOutlined, ReloadOutlined, SaveOutlined, UndoOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Empty, Popconfirm, Select, Skeleton, Space, Tag, Timeline, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { PersonaCardForm } from "../private-agent/PersonaTab";
import { PERSONA_MODE_META, personaActionLabel } from "../private-agent/private-meta";
import { PageHeader, QueryErrorAlert, useApiQuery, useUnsavedChanges } from "../shared";
import type { PersonaCard } from "../types";
import { meApi, type MyPersonaState, type PersonaWriteResult } from "./me-api";
import { formatQqTime, personaDiff } from "./me-meta";
import "./me.css";

const { Text, Paragraph } = Typography;

function errorText(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

export function MyPersonaPage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery({ queryKey: ["me", "persona"], fetcher: () => meApi.persona() });
  const [state, setState] = useState<MyPersonaState | null>(null);
  const [draft, setDraft] = useState<PersonaCard | null>(null);
  const [busy, setBusy] = useState("");

  useEffect(() => {
    if (!query.data) return;
    setState(query.data);
    setDraft(query.data.effectiveCard);
  }, [query.data]);

  const diff = useMemo(
    () => (state && draft ? personaDiff(state.effectiveCard, draft, state.options.fields) : null),
    [state, draft],
  );
  const dirty = !!diff && (Object.keys(diff.fields).length > 0 || diff.blocked.length > 0);

  // 有没保存的改动时，切页面会先确认
  useUnsavedChanges(dirty);

  const run = async (key: string, action: () => Promise<PersonaWriteResult>, done: (result: PersonaWriteResult) => string): Promise<void> => {
    setBusy(key);
    try {
      const result = await action();
      setState(result.state);
      setDraft(result.state.effectiveCard);
      message.success(done(result));
    } catch (reason) {
      message.error(errorText(reason));
    } finally {
      setBusy("");
    }
  };

  if (query.error && !state) return <div className="me-page"><QueryErrorAlert error={query.error} onRetry={query.reload} /></div>;
  if (!state || !draft) return <div className="me-page"><Skeleton active paragraph={{ rows: 8 }} /></div>;

  const mode = PERSONA_MODE_META[state.mode];
  const editable = state.canEdit;
  const options = state.options;

  return <div className="me-page">
    <PageHeader
      title="私聊人设"
      subtitle="Bot 和你一对一私聊时用的人设，只对你生效，群里照旧"
      status={<Tag color={mode?.color}>{state.sourceLabel}</Tag>}
      onRefresh={query.reload}
      refreshing={query.refreshing}
    />
    {!editable && (
      <Alert
        className="me-banner"
        type="warning"
        showIcon
        icon={<LockOutlined />}
        message={state.blockedReason ?? "现在不能自己改"}
        description="可以看当前人设；需要调整请联系管理员。"
      />
    )}

    <div className="me-persona-grid">
      <Card
        size="small"
        title="编辑人设"
        className="me-persona-editor"
        extra={editable && <Text type="secondary" className="me-hint">今天还能改 {state.editsLeftToday}/{state.selfEditDaily} 次</Text>}
      >
        {editable && (options.presets.length > 0 || options.library.length > 0) && (
          <div className="me-quick-pick">
            {options.presets.length > 0 && (
              <Select
                placeholder="套用内置模板"
                aria-label="套用内置模板"
                value={null}
                loading={busy === "preset"}
                onChange={(id: string) => void run("preset", () => meApi.usePreset(id), () => "已套用模板，名字保持不变")}
                options={options.presets.map((item) => ({ value: item.id, label: item.label, title: item.description }))}
                style={{ minWidth: 180 }}
              />
            )}
            {options.library.length > 0 && (
              <Select
                placeholder="用人设库里的人设"
                aria-label="用人设库里的人设"
                value={state.mode === "library" ? state.templateId : null}
                loading={busy === "template"}
                onChange={(id: string) => void run("template", () => meApi.useTemplate(id), (result) => `已改为跟随「${result.state.templateName ?? "模板"}」`)}
                options={options.library.map((item) => ({ value: item.id, label: item.name, title: item.summary }))}
                style={{ minWidth: 200 }}
              />
            )}
          </div>
        )}
        <fieldset disabled={!editable} className="me-fieldset">
          <PersonaCardForm value={draft} onChange={setDraft} />
        </fieldset>
        {diff && diff.blocked.length > 0 && (
          <Alert type="info" showIcon message={`管理员没开放这些字段给你改：${diff.blocked.join("、")}，保存时会被拒绝`} />
        )}
        {editable && (
          <div className="me-actions">
            <Space wrap>
              <Button
                type="primary"
                icon={<SaveOutlined />}
                disabled={!dirty || (diff?.blocked.length ?? 0) > 0}
                loading={busy === "save"}
                onClick={() => diff && void run("save", () => meApi.patchPersona(diff.fields), (result) => (result.changed.length ? `已保存：${result.changed.join("、")}` : "已保存"))}
              >保存（下一句就生效）</Button>
              <Button disabled={!dirty} onClick={() => setDraft(state.effectiveCard)}>放弃改动</Button>
            </Space>
            <Space wrap>
              <Button icon={<UndoOutlined />} disabled={state.history.length === 0} loading={busy === "undo"} onClick={() => void run("undo", () => meApi.undoPersona(), () => "已撤销上一次修改")}>撤销上一次</Button>
              <Popconfirm title="回到默认人设？" description="当前的设置会留在历史里，可以撤销。" onConfirm={() => run("reset", () => meApi.resetPersona(), () => "已回到默认人设")}>
                <Button icon={<ReloadOutlined />} loading={busy === "reset"} disabled={state.mode === "follow"}>回到默认</Button>
              </Popconfirm>
            </Space>
          </div>
        )}
      </Card>

      <div className="me-persona-side">
        <Card size="small" title="现在的样子">
          <Paragraph className="me-summary">{state.summary}</Paragraph>
          <Text type="secondary" className="me-hint">
            {mode?.hint}
            {state.callThem && <> · 称呼你“{state.callThem}”{state.callThemSource === "chat_prefs" ? "（来自你在聊天里说的偏好）" : ""}</>}
          </Text>
          <details className="me-compiled">
            <summary>模型实际看到的人设（约 {state.promptTokens}/{state.promptTokenLimit} Token）</summary>
            <pre>{state.compiled}</pre>
          </details>
        </Card>
        <Card size="small" title="最近的修改">
          {state.history.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没改过" /> : (
            <Timeline
              items={state.history.slice(0, 8).map((item) => ({
                children: <div className="me-history">
                  <Space size={6} wrap>
                    <strong>{personaActionLabel(item.action)}</strong>
                    <Tag>{item.by === "self" ? "我自己" : "管理员"}</Tag>
                    <Text type="secondary" className="me-hint">{formatQqTime(item.at)}</Text>
                  </Space>
                  {item.changed.length > 0 && <Text type="secondary" className="me-hint">改了：{item.changed.join("、")}</Text>}
                </div>,
              }))}
            />
          )}
          <Text type="secondary" className="me-hint">在私聊里对 Bot 说“以后自称本喵”这类话，也会记在这里。</Text>
        </Card>
      </div>
    </div>
  </div>;
}
