import {
  AppstoreOutlined,
  CloseOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  SaveOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  App as AntApp,
  AutoComplete,
  Button,
  Input,
  Popover,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { AdminEmpty, formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentDebugMode, AgentDebugResponse, AgentMessageItem, Member } from "../types";
import { useLocalState } from "./debug-storage";
import { AGENT_DEBUG_MODES, agentDebugModeLabel, debugMessageLabel, traceOutcomeLabel } from "./debug-utils";
import { ResultView } from "./ResultView";
import { formatDuration, promptStats, relativeTime } from "./trace-analysis";
import type { DebugRun, DebugRunInput, DebugRunsState } from "./useDebugRuns";

const { Text, Paragraph } = Typography;

export const MESSAGE_TEMPLATES: Array<{ label: string; text: string }> = [
  { label: "打招呼", text: "在吗？今天过得怎么样呀" },
  { label: "提问", text: "有人知道这周末天气怎么样吗？" },
  { label: "总结群聊", text: "帮我总结一下今天群里都聊了些什么" },
  { label: "查记忆", text: "你还记得我上次说过喜欢什么游戏吗" },
  { label: "设精华", text: "把刚才那条公告设成精华消息吧" },
  { label: "禁言", text: "刷屏那个人禁言十分钟" },
  { label: "发表情", text: "来个表情包活跃一下气氛" },
  { label: "长文本", text: "我想认真问一个问题：".padEnd(400, "，这里是一段很长的描述") },
];

const MODE_HINTS: Record<AgentDebugMode, string> = {
  dialogue: "被 @ / 回复 / 叫名时的回答",
  active: "群里聊得正热时插一句",
  warmup: "冷场一段时间后暖场",
  followup: "短会话里的自然续聊",
};

export interface LabPreset {
  id: string;
  name: string;
  mode: AgentDebugMode;
  source: "history" | "simulation";
  messageId?: string;
  actorUserId?: string;
  text?: string;
  runModel: boolean;
}

export interface LabReplayRequest {
  nonce: number;
  mode: AgentDebugMode;
  messageId?: string;
  actorUserId?: string;
  text?: string;
}

function isDebugMode(value: string): value is AgentDebugMode {
  return AGENT_DEBUG_MODES.some((item) => item.value === value);
}

export function normalizeMode(value: string): AgentDebugMode {
  return isDebugMode(value) ? value : "dialogue";
}

function memberName(member: Member): string {
  return member.groupNickname?.trim() || member.nickname?.trim() || member.userId;
}

function MemberPicker({ groupId, value, onChange }: { groupId: string; value: string; onChange: (value: string) => void }): React.JSX.Element {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);
  const query = useApiQuery({
    queryKey: ["agent-debug-members", groupId, debounced],
    fetcher: (signal) => api<Member[]>(`/groups/${groupId}/members?page=1&pageSize=20&search=${encodeURIComponent(debounced)}`, { signal }).then((response) => response.data),
  });
  const options = (query.data ?? []).map((member) => ({
    value: member.userId,
    label: <div className="dbg-member-option">
      <span>{memberName(member)}</span>
      <Text type="secondary">{member.userId}</Text>
      {member.role !== "member" && <Tag>{member.role}</Tag>}
    </div>,
  }));
  const invalid = value !== "" && !/^\d{5,12}$/.test(value);
  return <div>
    <AutoComplete
      value={value}
      options={options}
      onSearch={(text) => {
        setSearch(text);
        onChange(text.trim());
      }}
      onSelect={(selected: string) => onChange(selected)}
      status={invalid ? "warning" : undefined}
      style={{ width: "100%" }}
      aria-label="模拟发言人"
    >
      <Input placeholder="输入昵称或 QQ 号搜索群成员" allowClear onClear={() => onChange("")} />
    </AutoComplete>
    {invalid && <Text type="warning" className="dbg-small">QQ 号应为 5–12 位数字，可以从下拉里直接选成员</Text>}
  </div>;
}

export function SimulationLab({
  groupId,
  runs,
  replay,
  active,
}: {
  groupId: string;
  runs: DebugRunsState;
  replay: LabReplayRequest | null;
  active: boolean;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedMessageId = searchParams.get("debug.messageId") ?? "";
  const [mode, setMode] = useState<AgentDebugMode>("dialogue");
  const [source, setSource] = useState<"history" | "simulation">(linkedMessageId ? "history" : "simulation");
  const [messageId, setMessageId] = useState(linkedMessageId);
  const [actorUserId, setActorUserId] = useState("");
  const [text, setText] = useState("");
  const [runModel, setRunModel] = useState(false);
  const [running, setRunning] = useState<"" | "single" | "matrix">("");
  const [progress, setProgress] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState(0);
  const [tick, setTick] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const [presetsByGroup, setPresetsByGroup] = useLocalState<Record<string, LabPreset[]>>("presets", {});
  const presets = presetsByGroup[groupId] ?? [];

  const messagesQuery = useApiQuery({
    queryKey: ["agent-debug-messages", groupId],
    fetcher: (signal) => api<AgentMessageItem[]>(`/agent/groups/${groupId}/messages?page=1&pageSize=100`, { signal }).then((response) => response.data),
    invalidation: { resources: ["agent_group_data", "agent_privacy"], scope: { groupId } },
  });
  const linkedMessageQuery = useApiQuery({
    queryKey: ["agent-debug-linked-message", groupId, linkedMessageId],
    fetcher: (signal) => linkedMessageId
      ? api<AgentMessageItem[]>(`/agent/groups/${groupId}/messages?page=1&pageSize=1&messageId=${encodeURIComponent(linkedMessageId)}`, { signal }).then((response) => response.data)
      : Promise.resolve([]),
    invalidation: { resources: ["agent_group_data", "agent_privacy"], scope: { groupId } },
  });

  const availableMessages = useMemo(() => {
    const rows = [...(messagesQuery.data ?? [])];
    for (const linked of linkedMessageQuery.data ?? []) {
      if (!rows.some((row) => row.messageId === linked.messageId)) rows.unshift(linked);
    }
    return rows;
  }, [linkedMessageQuery.data, messagesQuery.data]);

  useEffect(() => {
    if (linkedMessageId) {
      setSource("history");
      setMessageId(linkedMessageId);
    }
  }, [linkedMessageId]);

  // 从 Trace 回放：带入场景和消息，交给用户确认后再运行。
  useEffect(() => {
    if (!replay) return;
    setMode(replay.mode);
    if (replay.messageId) {
      setSource("history");
      setMessageId(replay.messageId);
    } else {
      setSource("simulation");
      setActorUserId(replay.actorUserId ?? "");
      setText(replay.text ?? "");
    }
    setError(null);
  }, [replay]);

  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => setTick((value) => value + 1), 250);
    return () => window.clearInterval(timer);
  }, [running]);

  const selectMessage = (value: string): void => {
    setMessageId(value);
    const next = new URLSearchParams(searchParams);
    if (value) next.set("debug.messageId", value); else next.delete("debug.messageId");
    setSearchParams(next, { replace: true });
  };

  const messageOptions = useMemo(
    () => availableMessages.filter((row) => row.role !== "bot").map((row) => ({ value: row.messageId, label: debugMessageLabel(row) })),
    [availableMessages],
  );
  const selectedMessage = useMemo(() => availableMessages.find((row) => row.messageId === messageId) ?? null, [availableMessages, messageId]);
  const lastRoute = runs.runs.find((run) => run.input.mode === mode)?.result.route ?? null;
  const modelUnavailable = lastRoute !== null && !lastRoute.configured;

  const validate = (): string | null => {
    if (source === "history" && !messageId) return "请先选择一条历史消息";
    if (source === "simulation") {
      if (!/^\d{5,12}$/.test(actorUserId)) return "请填写有效的模拟发言人 QQ 号";
      if (!text.trim()) return "请填写模拟消息正文";
    }
    return null;
  };

  const request = useCallback(async (input: DebugRunInput, signal: AbortSignal): Promise<AgentDebugResponse> => {
    const body = input.source === "history"
      ? { mode: input.mode, messageId: Number(input.messageId), runModel: input.runModel }
      : { mode: input.mode, actorUserId: Number(input.actorUserId), text: (input.text ?? "").trim(), runModel: input.runModel };
    const response = await api<AgentDebugResponse>(`/agent/groups/${groupId}/debug/run`, { method: "POST", body: JSON.stringify(body), signal });
    return response.data;
  }, [groupId]);

  const currentInput = (overrides: Partial<DebugRunInput> = {}): DebugRunInput => ({
    mode,
    source,
    messageId: source === "history" ? messageId : undefined,
    actorUserId: source === "simulation" ? actorUserId : undefined,
    text: source === "simulation" ? text : undefined,
    runModel: runModel && !modelUnavailable,
    ...overrides,
  });

  const run = async (): Promise<void> => {
    const problem = validate();
    if (problem) {
      message.warning(problem);
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    const input = currentInput();
    setRunning("single");
    setProgress(input.runModel ? "正在调用真实模型（最多 30 秒）…" : "正在构建提示词快照…");
    setError(null);
    const began = performance.now();
    setStartedAt(began);
    try {
      const result = await request(input, controller.signal);
      runs.add({ input, result, elapsedMs: performance.now() - began });
      message.success(input.runModel ? "真实模型试跑完成，未执行任何动作" : "提示词快照已生成");
    } catch (caught) {
      if ((caught as Error).name === "AbortError") message.info("已取消本次运行");
      else setError(caught instanceof ApiError || caught instanceof Error ? caught.message : String(caught));
    } finally {
      abortRef.current = null;
      setRunning("");
      setProgress("");
    }
  };

  const runMatrix = async (): Promise<void> => {
    const problem = validate();
    if (problem) {
      message.warning(problem);
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    const batchId = `batch-${Date.now().toString(36)}`;
    setRunning("matrix");
    setError(null);
    const began = performance.now();
    setStartedAt(began);
    let done = 0;
    try {
      for (const item of AGENT_DEBUG_MODES) {
        setProgress(`四场景矩阵：正在生成「${item.label}」（${done + 1}/4）`);
        const input = currentInput({ mode: item.value, runModel: false });
        const stepStarted = performance.now();
        const result = await request(input, controller.signal);
        runs.add({ input, result, elapsedMs: performance.now() - stepStarted, batchId });
        done += 1;
      }
      message.success("四个场景的快照都生成好了");
    } catch (caught) {
      if ((caught as Error).name === "AbortError") message.info(`已取消，完成了 ${done}/4 个场景`);
      else setError((caught as Error).message);
    } finally {
      abortRef.current = null;
      setRunning("");
      setProgress("");
    }
  };

  const cancel = (): void => abortRef.current?.abort();

  const savePreset = (): void => {
    const problem = validate();
    if (problem) {
      message.warning(problem);
      return;
    }
    const name = source === "history"
      ? `${agentDebugModeLabel(mode)} · 回放 ${selectedMessage ? debugMessageLabel(selectedMessage).slice(0, 18) : messageId}`
      : `${agentDebugModeLabel(mode)} · ${text.trim().slice(0, 18)}`;
    const preset: LabPreset = { id: `p-${Date.now().toString(36)}`, name, mode, source, messageId, actorUserId, text, runModel };
    setPresetsByGroup((current) => ({ ...current, [groupId]: [preset, ...(current[groupId] ?? [])].slice(0, 20) }));
    message.success("已保存为预设（仅保存在这台设备）");
  };

  const loadPreset = (preset: LabPreset): void => {
    setMode(preset.mode);
    setSource(preset.source);
    if (preset.source === "history") selectMessage(preset.messageId ?? ""); else {
      setActorUserId(preset.actorUserId ?? "");
      setText(preset.text ?? "");
    }
    setRunModel(preset.runModel);
  };

  const deletePreset = (id: string): void => {
    setPresetsByGroup((current) => ({ ...current, [groupId]: (current[groupId] ?? []).filter((item) => item.id !== id) }));
  };

  // Ctrl/⌘ + Enter 在实验室任意位置运行。
  useEffect(() => {
    if (!active) return undefined;
    const onKey = (event: KeyboardEvent): void => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !running) {
        event.preventDefault();
        void run();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const elapsed = running ? Math.max(0, performance.now() - startedAt) : 0;
  void tick;
  const activeRun = runs.active;
  const batchRuns = activeRun?.batchId ? runs.runs.filter((item) => item.batchId === activeRun.batchId) : [];

  return <div className="dbg-lab">
    <section className="dbg-panel dbg-lab-input" aria-label="实验输入">
      <header className="dbg-panel-head">
        <div className="dbg-panel-title"><ExperimentOutlined /><span>实验输入</span></div>
        <Popover
          trigger="click"
          placement="bottomRight"
          title="实验预设"
          content={<div className="dbg-presets">
            {presets.length === 0 ? <Text type="secondary">还没有预设，把当前输入保存一份吧</Text> : presets.map((preset) => <div key={preset.id} className="dbg-preset">
              <button type="button" className="dbg-preset-load" onClick={() => loadPreset(preset)}>{preset.name}</button>
              <Button size="small" type="text" aria-label={`删除预设 ${preset.name}`} icon={<DeleteOutlined />} onClick={() => deletePreset(preset.id)} />
            </div>)}
            <Button block size="small" icon={<SaveOutlined />} onClick={savePreset}>保存当前输入</Button>
          </div>}
        >
          <Button size="small" icon={<AppstoreOutlined />}>预设{presets.length > 0 ? ` ${presets.length}` : ""}</Button>
        </Popover>
      </header>

      <div className="dbg-field">
        <span className="dbg-field-label" id="dbg-mode-label">调试场景</span>
        <div className="dbg-mode-grid" role="radiogroup" aria-labelledby="dbg-mode-label">
          {AGENT_DEBUG_MODES.map((item) => <button
            key={item.value}
            type="button"
            role="radio"
            aria-checked={mode === item.value}
            className={`dbg-mode-card${mode === item.value ? " is-active" : ""}`}
            onClick={() => setMode(item.value)}
          >
            <strong>{item.label}</strong>
            <span>{MODE_HINTS[item.value]}</span>
          </button>)}
        </div>
      </div>
      <div className="dbg-field">
        <span className="dbg-field-label">消息来源</span>
        <Segmented block value={source} onChange={(value) => setSource(value as "history" | "simulation")} options={[{ value: "history", label: "历史消息回放" }, { value: "simulation", label: "模拟消息" }]} />
      </div>

      {source === "history" ? <div className="dbg-field">
        <span className="dbg-field-label">历史消息</span>
        <Select
          showSearch
          loading={messagesQuery.loading}
          value={messageId || undefined}
          onChange={selectMessage}
          options={messageOptions}
          optionFilterProp="label"
          placeholder="选择保留期内的一条成员消息"
          notFoundContent={messagesQuery.error ? "消息记录加载失败" : "没有可回放的成员消息"}
          aria-label="历史消息"
        />
        {selectedMessage && <div className="dbg-source-preview">
          <Space wrap size={8}>
            <Text strong>{selectedMessage.senderName || selectedMessage.userId}</Text>
            <Tag>{selectedMessage.role}</Tag>
            <Text type="secondary">{formatTime(selectedMessage.receivedAt)}</Text>
          </Space>
          <Paragraph style={{ marginBottom: 0 }} ellipsis={{ rows: 4, expandable: true, symbol: "展开" }}>{selectedMessage.text || "[媒体消息]"}</Paragraph>
        </div>}
        {messageId && !selectedMessage && !messagesQuery.loading && <Text type="warning" className="dbg-small">这条消息不在最近 100 条里，或已过保留期；运行时后端会再确认一次。</Text>}
      </div> : <>
        <div className="dbg-field">
          <span className="dbg-field-label">模拟发言人</span>
          <MemberPicker groupId={groupId} value={actorUserId} onChange={setActorUserId} />
        </div>
        <div className="dbg-field">
          <span className="dbg-field-label">消息正文</span>
          <Input.TextArea value={text} onChange={(event) => setText(event.target.value)} autoSize={{ minRows: 3, maxRows: 8 }} maxLength={4000} showCount placeholder="输入要模拟的当前群消息" aria-label="消息正文" />
          <div className="dbg-templates" aria-label="消息模板">
            {MESSAGE_TEMPLATES.map((template) => <button key={template.label} type="button" className="dbg-chip" onClick={() => setText(template.text)}>{template.label}</button>)}
          </div>
        </div>
      </>}

      <div className="dbg-run-mode">
        <Switch checked={runModel && !modelUnavailable} onChange={setRunModel} disabled={modelUnavailable} aria-label="调用真实模型" />
        <div>
          <Text strong>{runModel && !modelUnavailable ? "调用真实模型" : "仅构建提示词"}</Text>
          <Text type="secondary" className="dbg-small">
            {modelUnavailable
              ? "这个场景的模型路由未配置完整，暂时只能生成快照"
              : runModel ? "30 秒超时、全局并发 2；依旧不会执行工具、发送或写库" : "只检查上下文、Prompt 和可见工具，不产生模型调用和费用"}
          </Text>
        </div>
      </div>

      <div className="dbg-run-actions">
        {running
          ? <>
            <Button type="primary" loading block>{progress || "运行中…"} {formatDuration(elapsed)}</Button>
            <Button icon={<CloseOutlined />} onClick={cancel}>取消</Button>
          </>
          : <>
            <Tooltip title="Ctrl / ⌘ + Enter">
              <Button type="primary" block icon={<ThunderboltOutlined />} onClick={() => void run()}>{runModel && !modelUnavailable ? "开始真实试跑" : "生成调试快照"}</Button>
            </Tooltip>
            <Tooltip title="同一条输入依次生成四个场景的快照（不调模型），并汇总对比">
              <Button onClick={() => void runMatrix()}>四场景矩阵</Button>
            </Tooltip>
          </>}
      </div>
      {error && <QueryErrorAlert error={error} onRetry={() => void run()} />}
      {messagesQuery.error && !messagesQuery.data && source === "history" && <QueryErrorAlert error={messagesQuery.error} onRetry={messagesQuery.reload} />}
    </section>

    <div className="dbg-lab-output">
      <RunHistory runs={runs} />
      {batchRuns.length > 1 && <MatrixSummary runs={batchRuns} activeId={runs.activeId} onSelect={runs.setActive} />}
      {activeRun
        ? <section className="dbg-panel"><ResultView key={activeRun.id} run={activeRun} baseline={runs.baseline} onPickBaseline={() => runs.setBaseline(activeRun.id)} /></section>
        : <section className="dbg-panel dbg-lab-empty">
          <AdminEmpty description={<>还没有运行结果。<br />选好场景和消息，按 <Text keyboard>Ctrl</Text> + <Text keyboard>Enter</Text> 生成一次 dry-run 快照。</>} />
        </section>}
    </div>
  </div>;
}

function runTitle(run: DebugRun): string {
  if (run.input.source === "history") return `回放消息 ${run.input.messageId ?? ""}`;
  return (run.input.text ?? "").trim().slice(0, 28) || "模拟消息";
}

function RunHistory({ runs }: { runs: DebugRunsState }): React.JSX.Element | null {
  if (runs.runs.length === 0) return null;
  return <section className="dbg-panel dbg-history" aria-label="运行历史">
    <header className="dbg-panel-head">
      <div className="dbg-panel-title"><span>运行历史</span><Tag className="dbg-count-tag">{runs.runs.length}</Tag></div>
      <div className="dbg-panel-actions">
        {runs.baseline && <Text type="secondary" className="dbg-small">A = {agentDebugModeLabel(runs.baseline.input.mode)} · {relativeTime(new Date(runs.baseline.createdAt).toISOString())}</Text>}
        <Button size="small" type="text" onClick={runs.clear}>清空</Button>
      </div>
    </header>
    <div className="dbg-history-list" role="listbox" aria-label="运行历史">
      {runs.runs.map((run) => {
        const outcome = run.result.result ? traceOutcomeLabel(run.result.result.outcome) : "快照";
        const selected = run.id === runs.activeId;
        const isBaseline = run.id === runs.baselineId;
        return <div
          key={run.id}
          role="option"
          tabIndex={0}
          aria-selected={selected}
          className={`dbg-run-card${selected ? " is-selected" : ""}${isBaseline ? " is-baseline" : ""}`}
          onClick={() => runs.setActive(run.id)}
          onKeyDown={(event) => { if (event.key === "Enter") runs.setActive(run.id); }}
        >
          <div className="dbg-run-card-top">
            {isBaseline && <span className="dbg-compare-side is-a">A</span>}
            <Tag className="dbg-tag-soft">{agentDebugModeLabel(run.input.mode)}</Tag>
            <Tag color={run.result.result ? (run.result.result.outcome === "success" ? "green" : "orange") : "default"}>{outcome}</Tag>
            {run.batchId && <Tag>矩阵</Tag>}
          </div>
          <div className="dbg-run-card-title">{runTitle(run)}</div>
          <div className="dbg-run-card-meta">
            <span>{new Date(run.createdAt).toLocaleTimeString("zh-CN")}</span>
            <span>≈{promptStats(run.result.promptMessages).tokens} Token</span>
          </div>
          <div className="dbg-run-card-actions">
            <Button size="small" type={isBaseline ? "primary" : "default"} onClick={(event) => {
              event.stopPropagation();
              runs.setBaseline(isBaseline ? "" : run.id);
            }}>{isBaseline ? "取消基准" : "设为基准 A"}</Button>
            <Button size="small" type="text" aria-label="删除这次运行" icon={<DeleteOutlined />} onClick={(event) => {
              event.stopPropagation();
              runs.remove(run.id);
            }} />
          </div>
        </div>;
      })}
    </div>
  </section>;
}

function MatrixSummary({ runs, activeId, onSelect }: { runs: DebugRun[]; activeId: string; onSelect: (id: string) => void }): React.JSX.Element {
  const rows = [...runs].sort((left, right) => AGENT_DEBUG_MODES.findIndex((item) => item.value === left.input.mode) - AGENT_DEBUG_MODES.findIndex((item) => item.value === right.input.mode));
  return <section className="dbg-panel" aria-label="四场景矩阵">
    <header className="dbg-panel-head"><div className="dbg-panel-title"><span>四场景矩阵</span></div><Text type="secondary" className="dbg-small">同一输入在四个场景下的快照对比，点一行查看详情</Text></header>
    <Table<DebugRun>
      size="small"
      rowKey="id"
      pagination={false}
      scroll={{ x: 640 }}
      dataSource={rows}
      onRow={(row) => ({ onClick: () => onSelect(row.id), className: row.id === activeId ? "dbg-row-active" : "dbg-row-clickable" })}
      columns={[
        { title: "场景", render: (_, row) => <Text strong>{agentDebugModeLabel(row.input.mode)}</Text> },
        { title: "Prompt", render: (_, row) => `${row.result.promptMessages.length} 条` },
        { title: "估算 Token", render: (_, row) => promptStats(row.result.promptMessages).tokens.toLocaleString() },
        { title: "开放工具", render: (_, row) => row.result.toolPermissions.filter((item) => item.exposed).length },
        { title: "历史 / 记忆", render: (_, row) => `${row.result.context.messages?.length ?? 0} / ${row.result.context.memories?.length ?? 0}` },
        { title: "提示", render: (_, row) => row.result.warnings.length > 0 ? <Tag color="gold">{row.result.warnings.length}</Tag> : "—" },
        { title: "耗时", render: (_, row) => formatDuration(row.elapsedMs) },
      ]}
    />
  </section>;
}
