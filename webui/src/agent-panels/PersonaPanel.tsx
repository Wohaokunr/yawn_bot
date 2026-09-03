import {
  Alert, App as AntApp, Button, Card, Col, Descriptions, Form, Input, Popconfirm,
  Progress, Row, Segmented, Select, Space, Spin, Switch, Tag, Typography,
} from "antd";
import { useRef, useState } from "react";
import { TraceCompareView } from "../agent-debug/TraceWorkspace";
import { api, ApiError } from "../api";
import {
  DraftDiffModal,
  QueryErrorAlert,
  SaveStatus,
  ServerDraftUpdateAlert,
  useApiQuery,
  useDraftSafeServerData,
  useUnsavedChanges,
} from "../shared";
import type { AgentDebugResponse, Persona, PersonaProfile } from "../types";
import {
  PERSONA_SOCIAL_TRAITS,
  PERSONA_STYLE_TRAITS,
  PERSONA_TRAIT_META,
  PERSONA_TRIAL_SCENARIOS,
  mergePersonaPreset,
  personaBehaviorPreview,
  personaDraftSummary,
  personaEmotionExpressionPreview,
} from "../agent-meta";
import type { PersonaFormValues, PersonaTraitKey } from "../agent-meta";

const { Text, Paragraph } = Typography;

export function PersonaPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<PersonaFormValues>();
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [diffOpen, setDiffOpen] = useState(false);
  const baseVersion = useRef<string | undefined>(undefined);
  const [trialActorUserId, setTrialActorUserId] = useState("");
  const [trialScenario, setTrialScenario] = useState("ordinary");
  const [trialCustomText, setTrialCustomText] = useState("");
  const [trialRunModel, setTrialRunModel] = useState(false);
  const [trialRunning, setTrialRunning] = useState(false);
  const [trialResult, setTrialResult] = useState<AgentDebugResponse | null>(null);
  const [trialBaseline, setTrialBaseline] = useState<AgentDebugResponse | null>(null);
  const [trialError, setTrialError] = useState<string | null>(null);
  const watchedMode = Form.useWatch("mode", form) as PersonaFormValues["mode"] | undefined;
  const watchedProfile = Form.useWatch("profile", form) as PersonaProfile | undefined;
  const query = useApiQuery({
    queryKey: ["agent-persona", groupId],
    fetcher: (signal) => api<Persona>(`/agent/groups/${groupId}/persona`, { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_persona"], scope: { groupId } },
  });
  useUnsavedChanges(dirty);

  const serverState = useDraftSafeServerData(query.data, dirty, (value) => {
    form.setFieldsValue({
      mode: value.enabled ? "custom" : "inherit",
      profile: value.profile,
    });
    baseVersion.current = value.version;
    setDirty(false);
    setTrialResult(null);
    setTrialError(null);
  });

  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;

  const profile = watchedProfile ?? data.profile;
  const mode = watchedMode ?? (data.enabled ? "custom" : "inherit");
  const draftSummary = personaDraftSummary(profile, data.presets);
  const draftBehavior = personaBehaviorPreview(profile);
  const draftEmotionExpression = personaEmotionExpressionPreview(data.emotion, profile.expressiveness);
  const selectedPreset = data.presets.find((item) => item.id === profile.presetId) ?? data.presets[0];

  const applyPreset = (presetId: string) => {
    const preset = data.presets.find((item) => item.id === presetId);
    if (!preset) return;
    const current = form.getFieldValue("profile") as PersonaProfile;
    form.setFieldsValue({ profile: mergePersonaPreset(current, preset) });
    setDirty(true);
    setTrialResult(null);
  };

  const save = async (values: PersonaFormValues) => {
    setSaving(true);
    try {
      const result = await api<Persona>(`/agent/groups/${groupId}/persona`, {
        method: "PUT",
        body: JSON.stringify({
          version: baseVersion.current,
          enabled: values.mode === "custom",
          profile: values.profile,
        }),
      });
      serverState.acceptServerData(result.data);
      message.success(values.mode === "custom" ? "当前群人设已保存" : "已切换为跟随全局人设");
      query.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        message.warning(`${error.message}；你的草稿仍然保留，请比较服务器新版本后再决定。`);
        query.reload();
      } else message.error((error as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setResetting(true);
    try {
      const result = await api<Persona>(`/agent/groups/${groupId}/persona`, {
        method: "DELETE",
        headers: baseVersion.current ? { "If-Match": baseVersion.current } : {},
      });
      serverState.acceptServerData(result.data);
      message.success("已清除当前群自定义并恢复全局人设");
      query.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        message.warning(`${error.message}；当前草稿未被清除。`);
        query.reload();
      } else message.error((error as Error).message);
    } finally {
      setResetting(false);
    }
  };

  const runTrial = async () => {
    const actorId = Number(trialActorUserId.trim());
    if (!Number.isInteger(actorId) || actorId <= 0) {
      message.warning("请填写当前群中的成员 QQ 号作为试演发言人");
      return;
    }
    const scenario = PERSONA_TRIAL_SCENARIOS.find((item) => item.value === trialScenario);
    const trialText = (trialScenario === "custom" ? trialCustomText : scenario?.text ?? "").trim();
    if (!trialText) {
      message.warning("请输入试演消息");
      return;
    }
    const draft = form.getFieldValue("profile") as PersonaProfile;
    setTrialRunning(true);
    setTrialError(null);
    try {
      const response = await api<AgentDebugResponse>(`/agent/groups/${groupId}/debug/run`, {
        method: "POST",
        body: JSON.stringify({
          mode: scenario?.mode ?? "dialogue",
          actorUserId: actorId,
          text: trialText,
          runModel: trialRunModel,
          personaDraft: draft,
        }),
      });
      setTrialResult(response.data);
      message.success(trialRunModel ? "草稿真实模型试演完成，无副作用" : "草稿 Prompt 快照已生成");
    } catch (error) {
      setTrialError((error as Error).message);
    } finally {
      setTrialRunning(false);
    }
  };

  const renderTrait = (key: PersonaTraitKey) => {
    const meta = PERSONA_TRAIT_META[key];
    const value = Number(profile[key] ?? 0);
    return (
      <div className="persona-trait-card" key={key}>
        <div className="persona-trait-head">
          <div>
            <strong>{meta.label}</strong>
            <span>{meta.help}</span>
          </div>
          <Tag>{value}/4 · {meta.levels[value]}</Tag>
        </div>
        <Form.Item name={["profile", key]} noStyle>
          <Segmented
            block
            options={meta.levels.map((label, level) => ({ value: level, label: `${level} ${label}` }))}
          />
        </Form.Item>
      </div>
    );
  };

  return (
    <Form
      form={form}
      layout="vertical"
      onFinish={save}
      onValuesChange={() => { setDirty(true); setTrialResult(null); }}
      className="persona-config-form"
    >
      <div className="persona-config-page agent-studio-page agent-studio-persona">
        {serverState.remoteUpdate && (
          <ServerDraftUpdateAlert
            onKeep={serverState.keepDraft}
            onCompare={() => setDiffOpen(true)}
            onReload={serverState.reloadRemote}
          />
        )}
        <DraftDiffModal
          open={diffOpen}
          draft={{
            enabled: form.getFieldValue("mode") === "custom",
            profile: form.getFieldValue("profile"),
          }}
          server={serverState.remoteUpdate}
          onClose={() => setDiffOpen(false)}
        />

        <section className="persona-config-hero agent-studio-hero liquid-glass agent-config-floating">
          <div className="persona-config-hero-copy">
            <div className="persona-config-eyebrow">PERSONA STUDIO · V2</div>
            <div className="persona-config-title-row">
              <div>
                <h2>当前群人设</h2>
                <p>先选一个角色模板，再用少量可理解的特征微调。事实、隐私、权限与工具安全不属于人设，始终由系统策略强制执行。</p>
              </div>
              <SaveStatus dirty={dirty} saving={saving} />
            </div>
            <div className="persona-config-metrics">
              <div className="persona-config-metric"><span>当前模板</span><strong>{selectedPreset?.label ?? "自然群友"}</strong></div>
              <div className="persona-config-metric"><span>说话风格</span><strong>{PERSONA_TRAIT_META.warmth.levels[profile.warmth]}</strong></div>
              <div className="persona-config-metric"><span>社交倾向</span><strong>{PERSONA_TRAIT_META.sociability.levels[profile.sociability]}</strong></div>
              <div className="persona-config-metric persona-config-metric-wide"><span>模式</span><strong>{mode === "custom" ? "当前群自定义" : "跟随全局"}</strong></div>
            </div>
          </div>

          <div className="persona-master-card is-enabled">
            <div className="persona-master-copy">
              <div className="persona-master-label">生效模式</div>
              <div className="persona-master-title">{mode === "custom" ? "使用当前群自定义" : "跟随全局人设"}</div>
              <div className="persona-master-description">切换为“跟随全局”不会删除当前草稿；再次切回自定义时可以继续编辑。恢复默认会真正清空当前群人设。</div>
            </div>
            <Form.Item name="mode" noStyle>
              <Segmented options={[{ value: "inherit", label: "跟随全局" }, { value: "custom", label: "当前群自定义" }]} />
            </Form.Item>
          </div>
        </section>

        {mode === "inherit" && (
          <Alert
            className="section-alert"
            type="info"
            showIcon
            message="当前 Agent 正在跟随全局人设"
            description="你仍然可以编辑和试演下面的草稿；只有切换到“当前群自定义”并保存后，草稿才会参与真实群聊。"
          />
        )}

        <div className="persona-config-layout">
          <div className="persona-config-main">
            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">PRESET</div><h3>角色模板</h3><p>模板给出一套完整起点，选择后仍可继续微调；不会改变系统安全规则。</p></div>
                <Tag>{data.presets.length} 个模板</Tag>
              </div>
              <div className="persona-preset-grid">
                {data.presets.map((preset) => (
                  <button
                    type="button"
                    key={preset.id}
                    className={`persona-preset-card${profile.presetId === preset.id ? " is-selected" : ""}`}
                    onClick={() => applyPreset(preset.id)}
                  >
                    <strong>{preset.label}</strong>
                    <span>{preset.description}</span>
                  </button>
                ))}
              </div>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">IDENTITY</div><h3>它是谁</h3><p>这里只保留真正需要文字表达的身份信息，不要求你写 Prompt。</p></div>
              </div>
              <Row gutter={[16, 0]}>
                <Col xs={24} md={8}><Form.Item name={["profile", "name"]} label="名字" rules={[{ required: true, message: "请输入名字" }]}><Input maxLength={64} showCount /></Form.Item></Col>
                <Col xs={24} md={16}><Form.Item name={["profile", "groupRole"]} label="群内角色"><Input maxLength={240} showCount placeholder="例如：普通群友" /></Form.Item></Col>
              </Row>
              <Form.Item name={["profile", "identity"]} label="身份定位"><Input.TextArea maxLength={240} showCount autoSize={{ minRows: 2, maxRows: 5 }} placeholder="例如：熟悉群聊节奏、自然简洁的普通群友" /></Form.Item>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">VOICE</div><h3>怎么说话</h3><p>用 0–4 档位微调常见表达特征，避免自由文本互相打架。</p></div>
              </div>
              <div className="persona-trait-grid">{PERSONA_STYLE_TRAITS.map(renderTrait)}</div>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">SOCIAL</div><h3>怎么参与群聊</h3><p>这些倾向会直接参与主动候选、短会话续聊和主动 reaction 决策；“运行设置”的开关、概率、冷却和每日上限始终是硬边界。</p></div>
              </div>
              <div className="persona-trait-grid">{PERSONA_SOCIAL_TRAITS.map(renderTrait)}</div>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">NOTES</div><h3>自定义补充</h3><p>只写模板和档位无法表达的角色细节。不要在这里重复隐私、知识或工具安全规则。</p></div>
              </div>
              <Form.Item name={["profile", "customNotes"]}>
                <Input.TextArea maxLength={240} showCount autoSize={{ minRows: 3, maxRows: 6 }} placeholder="例如：喜欢偶尔用“好困”自嘲，但不要每条消息都提。" />
              </Form.Item>
            </section>

            <section className="persona-config-section persona-trial-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">TRY IT</div><h3>未保存草稿试演</h3><p>直接用当前表单草稿调用同一套 Agent Debug。不会保存人设、不会执行工具、不会发 QQ、不会写记忆或主动状态。</p></div>
                <Tag color="blue">personaDraft</Tag>
              </div>
              <Row gutter={[16, 16]}>
                <Col xs={24} md={8}>
                  <Space orientation="vertical" size={6} style={{ width: "100%" }}>
                    <Text strong>试演成员 QQ 号</Text>
                    <Input value={trialActorUserId} onChange={(event) => setTrialActorUserId(event.target.value)} placeholder="必须是当前群成员" />
                  </Space>
                </Col>
                <Col xs={24} md={16}>
                  <Space orientation="vertical" size={6} style={{ width: "100%" }}>
                    <Text strong>场景</Text>
                    <Select value={trialScenario} onChange={setTrialScenario} options={PERSONA_TRIAL_SCENARIOS.map((item) => ({ value: item.value, label: item.label }))} style={{ width: "100%" }} />
                  </Space>
                </Col>
              </Row>
              {trialScenario === "custom" && <Input.TextArea value={trialCustomText} onChange={(event) => setTrialCustomText(event.target.value)} maxLength={4000} showCount autoSize={{ minRows: 3, maxRows: 7 }} placeholder="输入自定义试演消息" style={{ marginTop: 16 }} />}
              {trialScenario !== "custom" && <Alert style={{ marginTop: 16 }} type="info" showIcon message={PERSONA_TRIAL_SCENARIOS.find((item) => item.value === trialScenario)?.text} />}
              <div className="agent-debug-run-row persona-trial-run-row">
                <Space wrap>
                  <Switch checked={trialRunModel} onChange={setTrialRunModel} />
                  <div><Text strong>{trialRunModel ? "调用真实模型" : "仅构建 Prompt"}</Text><br /><Text type="secondary">真实模型也仍然是 dry-run，不执行任何工具或发送动作。</Text></div>
                </Space>
                <Button type="primary" onClick={runTrial} loading={trialRunning}>{trialRunModel ? "试演草稿" : "生成草稿快照"}</Button>
              </div>
              {trialError && <QueryErrorAlert error={trialError} onRetry={runTrial} />}
              {trialResult && (
                  <Card size="small" className="persona-trial-result" title="试演结果" extra={<Space wrap>
                    <Tag color="purple">{trialResult.persona.source === "draft" ? "未保存草稿" : "已保存人设"}</Tag>
                    {trialBaseline ? <Tag color="blue">已固定基准</Tag> : <Button size="small" onClick={() => setTrialBaseline(trialResult)}>固定当前为基准</Button>}
                    {trialBaseline && <Button size="small" onClick={() => setTrialBaseline(null)}>清除基准</Button>}
                  </Space>}>
                  <Descriptions size="small" column={{ xs: 1, md: 2 }} items={[
                    { key: "saved", label: "当前已保存", children: trialResult.persona.persistedSummary },
                    { key: "draft", label: "本次草稿", children: personaDraftSummary(trialResult.persona.appliedProfile, data.presets) },
                    { key: "behavior", label: "本次行为", children: `主动候选 ×${trialResult.persona.appliedBehavior.activeProbabilityScale.toFixed(2)} · 自动续聊 ${Math.max(0, trialResult.persona.appliedBehavior.maxFollowupBotTurns - 1)} 次 · 主动 reaction ${trialResult.persona.appliedBehavior.allowSpontaneousReaction ? "允许" : "关闭"}` },
                    { key: "emotion", label: "动态情绪", children: `${trialResult.persona.appliedEmotion.displayLabel} · 状态强度 ${Math.round(trialResult.persona.appliedEmotion.intensity * 100)}% · 表达 ${Math.round(trialResult.persona.appliedEmotion.expressionIntensity * 100)}%` },
                    { key: "prompt", label: "Prompt", children: `${trialResult.promptVersion} · ${trialResult.promptMessages.length} 条消息` },
                    { key: "sideEffect", label: "副作用", children: "0（工具、发送、状态写入均跳过）" },
                  ]} />
                  <Paragraph style={{ marginTop: 14, marginBottom: 0, whiteSpace: "pre-wrap" }}>
                    {trialResult.result?.text || (trialRunModel ? "（模型没有返回文本，可能只产生了工具意图）" : "已生成 Prompt 快照；开启“调用真实模型”可查看实际回答。")}
                  </Paragraph>
                  {trialBaseline && <div style={{ marginTop: 16 }}><TraceCompareView baseline={trialBaseline} current={trialResult} /></div>}
                </Card>
              )}
            </section>
          </div>

          <aside className="persona-config-aside">
            <div className="persona-note-card liquid-glass agent-config-floating">
              <div className="persona-note-title">当前草稿</div>
              <strong className="persona-draft-summary">{draftSummary}</strong>
              <p>{profile.identity}</p>
              <div className="persona-summary-tags">
                <Tag>{PERSONA_TRAIT_META.directness.levels[profile.directness]}</Tag>
                <Tag>{PERSONA_TRAIT_META.expressiveness.levels[profile.expressiveness]}</Tag>
                <Tag>{PERSONA_TRAIT_META.followupTendency.levels[profile.followupTendency]}</Tag>
                <Tag>{PERSONA_TRAIT_META.reactionTendency.levels[profile.reactionTendency]}</Tag>
              </div>
            </div>
            <div className="persona-note-card persona-behavior-card liquid-glass agent-config-floating">
              <div className="persona-note-title">实际行为影响</div>
              <p>主动候选：现有运行策略 × {draftBehavior.activeProbabilityScale.toFixed(2)}。Persona 只能收窄，不能突破运行配置。</p>
              <p>自动续聊：{draftBehavior.maxFollowupBotTurns <= 1 ? "首轮回复后结束" : `最多再续 ${draftBehavior.maxFollowupBotTurns - 1} 次`}。</p>
              <p>主动 reaction：{draftBehavior.allowSpontaneousReaction ? `允许 · ${draftBehavior.reactionMode}` : "关闭（明确用户请求不受影响）"}。</p>
            </div>
            <div className="persona-note-card persona-emotion-card liquid-glass agent-config-floating">
              <div className="persona-note-title">动态情绪</div>
              <Space size={8} wrap>
                <Tag>{data.emotion.displayLabel}</Tag>
                <Text type="secondary">状态强度 {Math.round(data.emotion.intensity * 100)}%</Text>
              </Space>
              <Progress percent={Math.round(draftEmotionExpression * 100)} size="small" showInfo={false} />
              <p>{data.emotion.reason || "近期没有明显情绪事件，保持 Persona 的基础气质。"}</p>
              <p>{data.emotion.expressionHint}</p>
              {data.emotion.updatedAt && (
                <Text type="secondary">约 {data.emotion.ageMinutesBucket} 分钟前更新 · 会自动衰减回平静</Text>
              )}
            </div>
            <div className="persona-note-card persona-note-soft liquid-glass agent-config-floating">
              <div className="persona-note-title">已保存版本</div>
              <p>{data.summary}</p>
              {dirty && <Tag color="orange">草稿尚未保存</Tag>}
            </div>
            <div className="persona-note-card persona-note-soft liquid-glass agent-config-floating">
              <div className="persona-note-title">系统策略不可覆盖</div>
              <p>事实性、隐私、权限、工具能力和 Prompt 注入防护永远不由人设控制。人设只决定“像谁、怎么说、倾向怎么参与”。</p>
            </div>
          </aside>
        </div>

        <div className="persona-config-savebar liquid-glass agent-config-floating">
          <div className="persona-save-state">
            <strong>{dirty ? "当前人设有未保存草稿" : "人设配置已同步"}</strong>
            <span>{dirty ? draftSummary : data.summary}</span>
          </div>
          <Space>
            <Popconfirm
              title="恢复全局人设？"
              description="会真正清空当前群 Persona v2 自定义资料并恢复全局默认。"
              okText="恢复默认"
              cancelText="取消"
              onConfirm={reset}
            >
              <Button loading={resetting}>恢复全局</Button>
            </Popconfirm>
            <Button type="primary" htmlType="submit" size="large" loading={saving} disabled={!dirty || resetting}>
              保存人设
            </Button>
          </Space>
        </div>
      </div>
    </Form>
  );
}
