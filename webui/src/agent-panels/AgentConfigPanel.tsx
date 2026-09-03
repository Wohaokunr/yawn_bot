import { App as AntApp, Button, Form, InputNumber, Segmented, Select, Spin, Switch } from "antd";
import { useRef, useState } from "react";
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
import type { AgentConfig } from "../types";

type ParticipationIntensity = "restrained" | "balanced" | "active" | "custom";

const PARTICIPATION_PRESETS: Record<Exclude<ParticipationIntensity, "custom">, { warmup: number; interject: number }> = {
  restrained: { warmup: 0.18, interject: 0.10 },
  balanced: { warmup: 0.35, interject: 0.25 },
  active: { warmup: 0.55, interject: 0.45 },
};

function participationIntensity(warmup: number, interject: number): ParticipationIntensity {
  const entry = Object.entries(PARTICIPATION_PRESETS).find(([, preset]) =>
    Math.abs(preset.warmup - warmup) < 0.001 && Math.abs(preset.interject - interject) < 0.001,
  );
  return (entry?.[0] as ParticipationIntensity | undefined) ?? "custom";
}

export function AgentConfigPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [diffOpen, setDiffOpen] = useState(false);
  const baseVersion = useRef<string | undefined>(undefined);

  const query = useApiQuery({
    queryKey: ["agent-config", groupId],
    fetcher: (signal) => api<AgentConfig>(`/agent/groups/${groupId}/config`, { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_config"], scope: { groupId } },
  });
  const watchedProactiveEnabled = Form.useWatch("proactiveEnabled", form) as boolean | undefined;
  const watchedWarmupProbability = Form.useWatch("proactiveProbability", form) as number | undefined;
  const watchedInterjectProbability = Form.useWatch("proactiveActiveProbability", form) as number | undefined;
  useUnsavedChanges(dirty);

  const serverState = useDraftSafeServerData(query.data, dirty, (value) => {
    form.setFieldsValue(value);
    baseVersion.current = value.version;
    setDirty(false);
  });

  const save = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
      const result = await api<AgentConfig>(`/agent/groups/${groupId}/config`, {
        method: "PATCH",
        body: JSON.stringify({ ...values, version: baseVersion.current }),
      });
      serverState.acceptServerData(result.data);
      message.success("Agent 配置已保存");
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

  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;
  const intensity = participationIntensity(
    watchedWarmupProbability ?? data.proactiveProbability,
    watchedInterjectProbability ?? data.proactiveActiveProbability,
  );
  const setParticipationIntensity = (value: string | number): void => {
    const preset = PARTICIPATION_PRESETS[String(value) as Exclude<ParticipationIntensity, "custom">];
    if (!preset) return;
    form.setFieldsValue({
      proactiveProbability: preset.warmup,
      proactiveActiveProbability: preset.interject,
    });
    setDirty(true);
  };

  return (
    <Form
      form={form}
      layout="vertical"
      onFinish={save}
      onValuesChange={() => setDirty(true)}
      className="agent-config-form"
    >
      <div className="agent-config-page agent-studio-page agent-studio-runtime">
        {serverState.remoteUpdate && (
          <ServerDraftUpdateAlert
            onKeep={serverState.keepDraft}
            onCompare={() => setDiffOpen(true)}
            onReload={serverState.reloadRemote}
          />
        )}
        <DraftDiffModal
          open={diffOpen}
          draft={form.getFieldsValue(true)}
          server={serverState.remoteUpdate}
          onClose={() => setDiffOpen(false)}
        />

        <section className="agent-config-hero agent-studio-hero liquid-glass agent-config-floating">
          <div className="agent-config-hero-copy">
            <div className="agent-config-eyebrow">GROUP AGENT</div>
            <div className="agent-config-title-row">
              <div>
                <h2>群级运行配置</h2>
                <p>控制 Agent 在这个群里的响应方式、主动行为、记忆边界与管理能力。</p>
              </div>
              <SaveStatus dirty={dirty} saving={saving} />
            </div>
            <div className="agent-config-metrics">
              <div className="agent-config-metric">
                <span>今日主动发言</span>
                <strong>{data.proactiveToday}</strong>
              </div>
              <div className="agent-config-metric">
                <span>今日管理工具</span>
                <strong>{data.adminToolsToday}</strong>
              </div>
              <div className="agent-config-metric">
                <span>今日高风险工具</span>
                <strong>{data.criticalToolsToday}</strong>
              </div>
              <div className="agent-config-metric agent-config-metric-wide">
                <span>配置范围</span>
                <strong>仅当前群</strong>
              </div>
            </div>
          </div>

          <div className="agent-master-card">
            <div className="agent-master-copy">
              <div className="agent-master-label">总开关</div>
              <div className="agent-master-title">启用 Agent</div>
              <div className="agent-master-description">
                关闭后停止群聊响应、主动发言、短会话以及自动消息采集和记忆整理；子配置会保留。
              </div>
            </div>
            <Form.Item name="enabled" valuePropName="checked" noStyle>
              <Switch size="default" />
            </Form.Item>
          </div>
        </section>

        <div className="agent-config-layout">
          <div className="agent-config-main">
            <section className="agent-config-section liquid-glass agent-config-floating">
              <div className="agent-config-section-head">
                <div>
                  <div className="agent-config-section-kicker">CONVERSATION</div>
                  <h3>聊天参与</h3>
                  <p>@ Agent 始终会回应；这里只控制额外的自然交互能力。</p>
                </div>
              </div>
              <div className="agent-config-grid agent-config-grid-2">
                <div className="agent-config-toggle-card">
                  <div>
                    <div className="agent-config-toggle-title">叫名字也回应</div>
                    <div className="agent-config-toggle-help">不必 @，直接叫 Agent 名字或常用唤醒词也可以开始对话。</div>
                  </div>
                  <Form.Item name="explicitWakeupEnabled" valuePropName="checked" noStyle>
                    <Switch />
                  </Form.Item>
                </div>
                <div className="agent-config-toggle-card">
                  <div>
                    <div className="agent-config-toggle-title">自然续聊</div>
                    <div className="agent-config-toggle-help">Bot 回复后，可在同一话题中继续自然接话。</div>
                  </div>
                  <Form.Item name="shortConversationEnabled" valuePropName="checked" noStyle>
                    <Switch />
                  </Form.Item>
                </div>
                <div className="agent-config-toggle-card agent-config-toggle-card-featured">
                  <div>
                    <div className="agent-config-toggle-title">主动参与群聊</div>
                    <div className="agent-config-toggle-help">没人直接叫它时，也允许根据群聊上下文适时暖场或加入话题。</div>
                  </div>
                  <Form.Item name="proactiveEnabled" valuePropName="checked" noStyle>
                    <Switch />
                  </Form.Item>
                </div>
              </div>
            </section>

            {(watchedProactiveEnabled ?? data.proactiveEnabled) && <section className="agent-config-section liquid-glass agent-config-floating">
              <div className="agent-config-section-head">
                <div>
                  <div className="agent-config-section-kicker">PARTICIPATION</div>
                  <h3>主动参与策略</h3>
                  <p>Agent 自动根据群聊状态选择冷场暖场或加入正在进行的话题；普通配置只需要控制参与强度和边界。</p>
                </div>
              </div>
              <div className="agent-config-toggle-card agent-config-toggle-card-featured">
                <div>
                  <div className="agent-config-toggle-title">参与强度</div>
                  <div className="agent-config-toggle-help">同时调整暖场和加入话题的积极程度，不需要分别理解两套概率。</div>
                </div>
                <Segmented
                  value={intensity}
                  onChange={setParticipationIntensity}
                  options={[
                    { value: "restrained", label: "克制" },
                    { value: "balanced", label: "平衡" },
                    { value: "active", label: "活跃" },
                    ...(intensity === "custom" ? [{ value: "custom", label: "自定义", disabled: true }] : []),
                  ]}
                />
              </div>
              <div className="agent-config-toggle-card">
                <div>
                  <div className="agent-config-toggle-title">群聊活跃时也允许加入</div>
                  <div className="agent-config-toggle-help">关闭后仍可在冷场时自然暖场，但不会加入群友正在进行的聊天。</div>
                </div>
                <Form.Item name="proactiveActiveEnabled" valuePropName="checked" noStyle>
                  <Switch />
                </Form.Item>
              </div>
              <div className="agent-config-grid agent-config-grid-3">
                <Form.Item name="idleThresholdMinutes" label="冷场判定" extra="安静多久后才考虑主动暖场（分钟）">
                  <InputNumber min={1} max={10080} />
                </Form.Item>
                <Form.Item name="cooldownMinutes" label="参与冷却" extra="两次主动参与之间至少间隔多少分钟">
                  <InputNumber min={0} max={10080} />
                </Form.Item>
                <Form.Item name="dailyLimit" label="每日参与上限" extra="达到后当天停止自动参与">
                  <InputNumber min={0} max={1000} />
                </Form.Item>
              </div>
              <details className="agent-debug-details">
                <summary>精细调节（高级）</summary>
                <div className="agent-config-grid agent-config-grid-3 section-row">
                  <Form.Item name="proactiveProbability" label="暖场基础概率" extra="满足冷场条件后的基础概率">
                    <InputNumber min={0} max={1} step={0.05} />
                  </Form.Item>
                  <Form.Item name="proactiveActiveProbability" label="加入话题概率" extra="活跃聊天进入候选后的参与概率">
                    <InputNumber min={0} max={1} step={0.02} />
                  </Form.Item>
                  <Form.Item name="proactiveActiveWindowMinutes" label="活跃话题窗口" extra="最近多少分钟的真人消息算作活跃话题">
                    <InputNumber min={1} max={1440} />
                  </Form.Item>
                </div>
              </details>
            </section>}

            <section className="agent-config-section liquid-glass agent-config-floating">
              <div className="agent-config-section-head">
                <div>
                  <div className="agent-config-section-kicker">MEMORY & MEDIA</div>
                  <h3>记忆与媒体</h3>
                  <p>设置原始消息保留周期、跨群记忆范围和媒体缓存策略。</p>
                </div>
              </div>
              <div className="agent-config-grid agent-config-grid-2">
                <Form.Item name="rawRetentionDays" label="原始消息保留" extra="到期后按记忆治理策略清理（天）">
                  <InputNumber min={1} max={365} />
                </Form.Item>
                <Form.Item name="crossGroupVisibility" label="跨群记忆">
                  <Select
                    options={[
                      { value: "isolated", label: "群隔离" },
                      { value: "public_summary", label: "共享低风险公开摘要" },
                    ]}
                  />
                </Form.Item>
              </div>
              <div className="agent-config-toggle-card">
                <div>
                  <div className="agent-config-toggle-title">媒体缓存</div>
                  <div className="agent-config-toggle-help">缓存图片理解结果，减少重复识图调用；关闭不会影响普通文字对话。</div>
                </div>
                <Form.Item name="mediaCacheEnabled" valuePropName="checked" noStyle>
                  <Switch />
                </Form.Item>
              </div>
            </section>

            <section className="agent-config-section liquid-glass agent-config-floating">
              <div className="agent-config-section-head">
                <div>
                  <div className="agent-config-section-kicker">TOOLS</div>
                  <h3>特权工具权限</h3>
                  <p>限制 Agent 可以调用的高副作用能力，以及每天的调用额度。读取、记忆写入和普通消息发送不占此额度。</p>
                </div>
              </div>
              <div className="agent-config-grid agent-config-grid-2">
                <Form.Item name="adminToolDailyLimit" label="每日特权工具上限" extra="群管理操作和群文件发送共用此额度">
                  <InputNumber min={1} max={1000} />
                </Form.Item>
                <Form.Item name="criticalToolDailyLimit" label="每日高风险工具上限" extra="踢人、管理员变更、全员禁言和破坏性群文件操作单独计数">
                  <InputNumber min={1} max={100} />
                </Form.Item>
                <Form.Item name="toolAllowlist" label="允许的特权工具">
                  <Select
                    mode="multiple"
                    placeholder="未选择时不允许调用特权工具"
                    options={[
                      { value: "mute_member", label: "禁言成员" },
                      { value: "create_group_announcement", label: "发布群公告" },
                      { value: "set_essence_message", label: "设置精华消息" },
                      { value: "remove_essence_message", label: "移出精华消息" },
                      { value: "delete_group_notice", label: "删除群公告" },
                      { value: "set_group_card", label: "修改群名片" },
                      { value: "set_special_title", label: "设置专属头衔" },
                      { value: "set_group_name", label: "修改群名称" },
                      { value: "create_group_folder", label: "创建群文件夹" },
                      { value: "send_file", label: "发送群文件" },
                      { value: "kick_member", label: "高风险 · 踢出成员" },
                      { value: "set_whole_group_mute", label: "高风险 · 全员禁言" },
                      { value: "set_group_admin", label: "高风险 · 设置管理员" },
                      { value: "delete_group_file", label: "高风险 · 删除群文件" },
                      { value: "move_group_file", label: "高风险 · 移动群文件" },
                      { value: "rename_group_file", label: "高风险 · 重命名群文件" },
                      { value: "delete_group_folder", label: "高风险 · 删除群文件夹" },
                    ]}
                  />
                </Form.Item>
              </div>
            </section>
          </div>

          <aside className="agent-config-aside">
            <div className="agent-config-note-card liquid-glass agent-config-floating">
              <div className="agent-config-note-title">配置说明</div>
              <p>总开关只控制运行状态，不会清空这里的参数、已有记忆或人设。</p>
              <p>因此你可以先关闭 Agent，再安全调整各项策略，最后重新开启。</p>
            </div>
            <div className="agent-config-note-card agent-config-note-soft liquid-glass agent-config-floating">
              <div className="agent-config-note-title">推荐顺序</div>
              <ol>
                <li>先选择需要的聊天参与能力</li>
                <li>需要时再调整主动参与频率</li>
                <li>确认记忆边界</li>
                <li>最后开放管理工具</li>
              </ol>
            </div>
          </aside>
        </div>

        <div className="agent-config-savebar liquid-glass agent-config-floating">
          <div>
            <strong>{dirty ? "有未保存的修改" : "配置已同步"}</strong>
            <span>{dirty ? "保存后立即按新策略运行" : "修改任意选项后可统一保存"}</span>
          </div>
          <Button type="primary" htmlType="submit" size="large" loading={saving} disabled={!dirty}>
            保存配置
          </Button>
        </div>
      </div>
    </Form>
  );
}
