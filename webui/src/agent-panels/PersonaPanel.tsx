import {
  Alert, App as AntApp, Button, Card, Col, Descriptions, Form, Input, Popconfirm,
  Progress, Row, Segmented, Select, Space, Spin, Switch, Tag, Typography,
} from "antd";
import { MinusCircleOutlined, PlusOutlined } from "@ant-design/icons";
import { useRef, useState } from "react";
import { TraceCompareView } from "../agent-debug/ResultDiff";
import { api, ApiError } from "../api";
import {
  DraftDiffModal,
  QueryErrorAlert,
  SaveStatus,
  ServerDraftUpdateAlert,
  useApiQuery,
  useDraftSafeServerData,
  useEntityRefresh,
  useUnsavedChanges,
} from "../shared";
import type { AgentDebugResponse, Persona, PersonaCard } from "../types";
import {
  PERSONA_LIMITS,
  PERSONA_SOCIAL_TRAITS,
  PERSONA_TRAIT_META,
  PERSONA_TRIAL_SCENARIOS,
  PERSONA_VOICE_TRAITS,
  cleanPersonaCard,
  mergePersonaPreset,
  personaBehaviorPreview,
  personaDraftSummary,
  personaEmotionExpressionPreview,
  personaTraitLabel,
} from "../agent-meta";
import type { PersonaFormValues, PersonaTraitKey } from "../agent-meta";
import { PersonaLibrary } from "./PersonaLibrary";

const { Text, Paragraph } = Typography;

export function PersonaPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message, modal } = AntApp.useApp();
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
  const watchedCard = Form.useWatch("card", form) as PersonaCard | undefined;
  const query = useApiQuery({
    queryKey: ["agent-persona", groupId],
    fetcher: (signal) => api<Persona>(`/agent/groups/${groupId}/persona`, { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_persona"], scope: { groupId } },
  });
  // 全局人设在人设库里变化时，跟随全局的群实际生效的人设也随之变化。
  useEntityRefresh(query.reload, ["agent_persona_library"]);
  useUnsavedChanges(dirty);

  const serverState = useDraftSafeServerData(query.data, dirty, (value) => {
    form.setFieldsValue({
      mode: value.enabled ? "custom" : "inherit",
      card: value.card,
    });
    baseVersion.current = value.version ?? undefined;
    setDirty(false);
    setTrialResult(null);
    setTrialError(null);
  });

  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;

  const card = { ...data.card, ...watchedCard };
  const mode = watchedMode ?? (data.enabled ? "custom" : "inherit");
  const draftSummary = personaDraftSummary(card, data.presets);
  const draftBehavior = personaBehaviorPreview(card);
  const draftEmotionExpression = personaEmotionExpressionPreview(data.emotion, card.expressiveness);
  const selectedPreset = data.presets.find((item) => item.id === card.presetId);
  const draftCard = (): PersonaCard => cleanPersonaCard({ ...data.card, ...(form.getFieldValue("card") as PersonaCard) });

  const applyPreset = (presetId: string) => {
    const preset = data.presets.find((item) => item.id === presetId);
    if (!preset) return;
    const apply = () => {
      form.setFieldsValue({ card: mergePersonaPreset(draftCard(), preset) });
      setDirty(true);
      setTrialResult(null);
    };
    // 模板会替换性格、口癖和档位；已有未保存修改时先确认，避免一下点没了。
    if (!dirty) {
      apply();
      return;
    }
    modal.confirm({
      title: `套用「${preset.label}」？`,
      content: "会替换当前草稿里的性格、说话方式、口癖和档位（名字保留）。",
      okText: "套用",
      cancelText: "取消",
      onOk: apply,
    });
  };

  const save = async (values: PersonaFormValues) => {
    setSaving(true);
    try {
      const result = await api<Persona>(`/agent/groups/${groupId}/persona`, {
        method: "PUT",
        body: JSON.stringify({
          version: baseVersion.current ?? null,
          enabled: values.mode === "custom",
          card: cleanPersonaCard({ ...data.card, ...values.card }),
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
    const draft = draftCard();
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
    const value = Number(card[key] ?? 0);
    return (
      <div className="persona-trait-card" key={key}>
        <div className="persona-trait-head">
          <div>
            <strong>{meta.label}</strong>
            <span>{meta.help}</span>
          </div>
          <Tag>{value}/4 · {personaTraitLabel(key, value)}</Tag>
        </div>
        <Form.Item name={["card", key]} noStyle>
          <Segmented
            block
            options={meta.levels.map((label, level) => ({ value: level, label: <span title={label}>{level}</span> }))}
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
            card: form.getFieldValue("card"),
          }}
          server={serverState.remoteUpdate}
          onClose={() => setDiffOpen(false)}
        />

        <section className="persona-config-hero agent-studio-hero liquid-glass agent-config-floating">
          <div className="persona-config-hero-copy">
            <div className="persona-config-eyebrow">PERSONA STUDIO · V3</div>
            <div className="persona-config-title-row">
              <div>
                <h2>当前群人设</h2>
                <p>选一个模板或从人设库套用，再改名字、自称、口癖和几个档位。事实、隐私、权限与工具安全不属于人设，始终由系统策略强制执行。</p>
              </div>
              <SaveStatus dirty={dirty} saving={saving} />
            </div>
            <div className="persona-config-metrics">
              <div className="persona-config-metric"><span>草稿模板</span><strong>{selectedPreset?.label ?? "自定义"}</strong></div>
              <div className="persona-config-metric"><span>正在生效</span><strong>{data.sourceLabel}</strong></div>
              <div className="persona-config-metric"><span>人设 Prompt</span><strong>{data.promptTokens}/{data.promptTokenLimit} tokens</strong></div>
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
            message={`当前群正在使用：${data.globalLabel}（${data.globalSummary}）`}
            description="你仍然可以编辑和试演下面的草稿；只有切换到“当前群自定义”并保存后，草稿才会参与真实群聊。全局人设在下方人设库里设置。"
          />
        )}

        <div className="persona-config-layout">
          <div className="persona-config-main">
            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">PRESET</div><h3>角色模板</h3><p>模板给出一套完整起点（性格、说话方式、口癖和档位），套用后保留名字，其余都能继续改。</p></div>
                <Tag>{data.presets.length} 个模板</Tag>
              </div>
              <div className="persona-preset-grid">
                {data.presets.map((preset) => (
                  <button
                    type="button"
                    key={preset.id}
                    className={`persona-preset-card${card.presetId === preset.id ? " is-selected" : ""}`}
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
                <div><div className="persona-config-section-kicker">IDENTITY</div><h3>它是谁</h3><p>名字、自称和对群友的称呼会直接进入人设；留空就用自然说法。</p></div>
              </div>
              {/* presetId 由预设卡片写入；必须注册字段，否则 onFinish 会丢弃它。 */}
              <Form.Item name={["card", "presetId"]} hidden><Input /></Form.Item>
              <Row gutter={[16, 0]}>
                <Col xs={24} md={8}><Form.Item name={["card", "name"]} label="名字" rules={[{ required: true, whitespace: true, message: "请输入名字" }]}><Input maxLength={PERSONA_LIMITS.name} showCount /></Form.Item></Col>
                <Col xs={12} md={8}><Form.Item name={["card", "selfName"]} label="自称" tooltip="例如“本喵”；留空则正常用“我”"><Input maxLength={PERSONA_LIMITS.alias} showCount placeholder="我" /></Form.Item></Col>
                <Col xs={12} md={8}><Form.Item name={["card", "userTitle"]} label="称呼群友" tooltip="例如“杂鱼”；留空则叫群名片"><Input maxLength={PERSONA_LIMITS.alias} showCount placeholder="群名片" /></Form.Item></Col>
              </Row>
              <Form.Item name={["card", "identity"]} label="一句话身份"><Input maxLength={PERSONA_LIMITS.identity} showCount placeholder="例如：嘴硬心软、很护食的猫娘群友" /></Form.Item>
              <Form.Item name={["card", "personality"]} label="性格"><Input.TextArea maxLength={PERSONA_LIMITS.longText} showCount autoSize={{ minRows: 2, maxRows: 6 }} placeholder="例如：表面嫌弃，其实很在意大家；被夸会害羞" /></Form.Item>
              <Form.Item name={["card", "speakingStyle"]} label="说话方式"><Input.TextArea maxLength={PERSONA_LIMITS.longText} showCount autoSize={{ minRows: 2, maxRows: 6 }} placeholder="例如：短句口语，偶尔句尾带“喵”，不用列表" /></Form.Item>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">FLAVOR</div><h3>口癖与话题</h3><p>回车添加一条。口癖会被提示“偶尔用”，不会每句都带。</p></div>
              </div>
              <Row gutter={[16, 0]}>
                <Col xs={24} md={8}><Form.Item name={["card", "catchphrases"]} label={`口癖（最多 ${PERSONA_LIMITS.catchphrases} 条）`}><Select mode="tags" open={false} maxCount={PERSONA_LIMITS.catchphrases} tokenSeparators={["，", ",", "、"]} placeholder="喵、哼" /></Form.Item></Col>
                <Col xs={24} md={8}><Form.Item name={["card", "likes"]} label={`喜欢的话题（最多 ${PERSONA_LIMITS.topics} 条）`}><Select mode="tags" open={false} maxCount={PERSONA_LIMITS.topics} tokenSeparators={["，", ",", "、"]} placeholder="小鱼干、游戏" /></Form.Item></Col>
                <Col xs={24} md={8}><Form.Item name={["card", "avoidTopics"]} label={`避开的话题（最多 ${PERSONA_LIMITS.topics} 条）`}><Select mode="tags" open={false} maxCount={PERSONA_LIMITS.topics} tokenSeparators={["，", ",", "、"]} placeholder="政治" /></Form.Item></Col>
              </Row>
              <Form.List name={["card", "examples"]}>
                {(fields, { add, remove }) => (
                  <div className="persona-example-list">
                    <Text strong>示例台词</Text>
                    <Text type="secondary"> · 给模型看语气用，不会被原样复读</Text>
                    {fields.map((field) => (
                      <div className="persona-example-row" key={field.key}>
                        <Form.Item name={field.name} noStyle><Input maxLength={PERSONA_LIMITS.exampleLength} placeholder="哼，本喵才不是特意等你的" /></Form.Item>
                        <Button type="text" aria-label="删除这句" icon={<MinusCircleOutlined />} onClick={() => remove(field.name)} />
                      </div>
                    ))}
                    {fields.length < PERSONA_LIMITS.examples && (
                      <Button type="dashed" icon={<PlusOutlined />} onClick={() => add("")} block>添加示例台词</Button>
                    )}
                  </div>
                )}
              </Form.List>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">VOICE</div><h3>怎么说话</h3><p>用 0–4 档位微调常见表达特征。</p></div>
              </div>
              <div className="persona-trait-grid">{PERSONA_VOICE_TRAITS.map(renderTrait)}</div>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">SOCIAL</div><h3>怎么参与群聊</h3><p>活跃度同时决定主动候选和自动续聊次数；“运行设置”的开关、概率、冷却和每日上限始终是硬边界。</p></div>
              </div>
              <div className="persona-trait-grid">{PERSONA_SOCIAL_TRAITS.map(renderTrait)}</div>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">LIBRARY</div><h3>人设库</h3><p>把调好的人设存起来给其它群一键套用；设为全局的那张会被所有“跟随全局”的群使用。</p></div>
              </div>
              <PersonaLibrary
                groupId={groupId}
                draft={draftCard}
                version={() => baseVersion.current}
                dirty={dirty}
                onApplied={(persona) => { serverState.acceptServerData(persona); query.reload(); }}
              />
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
                    { key: "saved", label: "当前生效", children: `${trialResult.persona.persistedSource ? `${trialResult.persona.persistedSource} · ` : ""}${trialResult.persona.persistedSummary}` },
                    { key: "draft", label: "本次草稿", children: personaDraftSummary(trialResult.persona.appliedCard, data.presets) },
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
              {card.identity && <p>{card.identity}</p>}
              <div className="persona-summary-tags">
                <Tag>{personaTraitLabel("expressiveness", card.expressiveness)}</Tag>
                <Tag>{personaTraitLabel("reactionTendency", card.reactionTendency)}</Tag>
                {(card.catchphrases ?? []).filter(Boolean).slice(0, 3).map((item) => <Tag key={item} color="pink">{item}</Tag>)}
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
              <div className="persona-note-title">正在生效 · {data.sourceLabel}</div>
              <p>{data.summary}</p>
              {dirty && <Tag color="orange">草稿尚未保存</Tag>}
              {data.trimmed && <Tag color="gold">内容过长，已自动精简</Tag>}
              <details className="persona-compiled-preview">
                <summary>查看模型实际看到的人设（{data.promptTokens} tokens）</summary>
                <pre>{data.compiled}</pre>
              </details>
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
              description="会真正清空当前群的自定义人设，改为跟随全局人设。"
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
