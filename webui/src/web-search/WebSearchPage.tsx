import {
  ClearOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  GlobalOutlined,
  HistoryOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
  SearchOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  Popconfirm,
  Radio,
  Select,
  Skeleton,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, SaveStatus, useApiQuery, useUnsavedChanges } from "../shared";
import { formatEpoch, relativeEpoch, updateSearchParams } from "../sandbox/sandbox-meta";
import { webSearchApi } from "./web-search-api";
import {
  cacheHitRate,
  describeWebAccess,
  ERROR_LABELS,
  formatYuan,
  monthlyEstimate,
  parseDomainList,
  parseGroupIds,
  RECENT_STATUS_META,
  scoreLabel,
  validateWebSettings,
  WEB_SETTING_RANGES,
  WEB_STATE_META,
  webSettingsEqual,
} from "./web-search-meta";
import type { WebNumericKey } from "./web-search-meta";
import type {
  WebFetchTestResult,
  WebSearchRecentCall,
  WebSearchSettings,
  WebSearchStatus,
  WebSearchTestResult,
  WebSearchUsageRow,
} from "./types";

type WebTab = "overview" | "recent" | "test" | "policy";
const TABS: WebTab[] = ["overview", "recent", "test", "policy"];

function parseTab(value: string | null): WebTab {
  return TABS.includes(value as WebTab) ? (value as WebTab) : "overview";
}

export function WebSearchStatusBar({ status, onReload, reloading }: { status: WebSearchStatus; onReload: () => void; reloading: boolean }): React.JSX.Element {
  const meta = WEB_STATE_META[status.state];
  const usage = status.usage;
  const detail = status.state === "ready" ? describeWebAccess(status.settings) : meta.hint;
  return (
    <section className={`sbx-status is-${meta.tone}`} aria-label="网页搜索状态">
      <div className="sbx-status-main">
        <span className={`sbx-dot is-${meta.tone}`} aria-hidden="true" />
        <div className="sbx-status-copy">
          <strong>网页搜索 · {meta.label}</strong>
          <span title={detail}>{detail}</span>
        </div>
      </div>
      <div className="sbx-status-chips">
        <Tooltip title={status.keySource ? `密钥来自 ${status.keySource}` : "没有找到可用的 DeepSeek 密钥"}>
          <Tag color={status.configured ? "green" : "red"}>{status.configured ? `密钥 ${status.keyMasked}` : "无密钥"}</Tag>
        </Tooltip>
        <Tooltip title="今日计费请求 / 全站每日预算">
          <Tag color={usage.globalLeft === 0 ? "red" : undefined}>今日 {usage.billed} / {usage.globalLimit} 次</Tag>
        </Tooltip>
        <Tooltip title={`按 ¥${usage.pricePerCallYuan} / 次成功请求估算；失败、无结果、命中缓存不计费`}>
          <Tag color="gold">约 {formatYuan(usage.costYuan)}</Tag>
        </Tooltip>
        {status.upstream.failures > 0 && status.upstream.lastError && (
          <Tooltip title={`${status.upstream.lastError}（${relativeEpoch(status.upstream.lastErrorAt)}）`}>
            <Tag color="orange">最近失败 {status.upstream.failures} 次</Tag>
          </Tooltip>
        )}
      </div>
      <div className="sbx-status-actions">
        <Button size="small" icon={<ReloadOutlined />} loading={reloading} onClick={onReload}>刷新</Button>
      </div>
    </section>
  );
}

function UsageTable({ title, rows, limit, member }: { title: string; rows: WebSearchUsageRow[]; limit: number; member?: boolean }): React.JSX.Element {
  return (
    <Table
      size="small"
      rowKey={(row) => `${row.groupId}:${row.userId ?? ""}`}
      title={() => title}
      dataSource={rows}
      pagination={false}
      scroll={{ x: member ? 420 : 320 }}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="今天还没有人搜过" /> }}
      columns={[
        ...(member ? [{ title: "成员", dataIndex: "userId" }] : []),
        { title: "群", dataIndex: "groupId", render: (value: string, row: WebSearchUsageRow) => (row.groupName ? `${row.groupName}（${value}）` : value) },
        {
          title: "次数",
          dataIndex: "calls",
          width: 120,
          render: (value: number) => <Tag color={limit && value >= limit ? "red" : undefined}>{value} / {limit}</Tag>,
        },
      ]}
    />
  );
}

function OverviewTab({ status }: { status: WebSearchStatus }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const usage = status.usage;
  const totals = usage.totals;
  const [clearing, setClearing] = useState(false);
  return (
    <div className="web-overview">
      <div className="web-stats">
        <Card size="small"><Statistic title="今日搜索" value={totals.search ?? 0} suffix="次" /></Card>
        <Card size="small"><Statistic title="今日读网页" value={totals.fetch ?? 0} suffix="次" /></Card>
        <Card size="small"><Statistic title="计费请求" value={usage.billed} suffix={`/ ${usage.globalLimit}`} /></Card>
        <Card size="small"><Statistic title="今日费用（估算）" value={formatYuan(usage.costYuan)} /></Card>
        <Card size="small"><Statistic title="无结果 / 失败" value={`${totals.empty ?? 0} / ${totals.failed ?? 0}`} /></Card>
        <Card size="small"><Statistic title="缓存命中" value={totals.cached ?? 0} suffix={`次 · ${cacheHitRate(status.cache.search)}`} /></Card>
      </div>
      <Card size="small" title="服务" className="web-service">
        <dl className="web-dl">
          <dt>提供方</dt><dd>DeepSeek 官方 Search / Fetch API（¥{usage.pricePerCallYuan} / 次成功请求）</dd>
          <dt>接口地址</dt><dd><code>{status.baseUrl}</code></dd>
          <dt>密钥来源</dt><dd>{status.keySource ?? "未配置"}</dd>
          <dt>上游调用</dt><dd>{status.upstream.calls} 次，平均 {status.upstream.avgMs}ms，失败 {status.upstream.failures} 次{status.upstream.lastOkAt ? `，最近成功 ${relativeEpoch(status.upstream.lastOkAt)}` : ""}</dd>
          <dt>缓存</dt><dd>搜索 {status.cache.search.size} 条 · 网页 {status.cache.fetch.size} 页</dd>
        </dl>
        <Button
          size="small"
          icon={<ClearOutlined />}
          loading={clearing}
          onClick={async () => {
            setClearing(true);
            try {
              const result = await webSearchApi.clearCache();
              message.success(`已清空 ${result.cleared} 条缓存`);
            } catch (error) {
              message.error(error instanceof Error ? error.message : "清空失败");
            } finally {
              setClearing(false);
            }
          }}
        >清空缓存</Button>
      </Card>
      <div className="sbx-usage">
        <UsageTable title={`今日各群用量（${usage.day ?? "—"}）`} rows={usage.groups} limit={usage.groupLimit} />
        <UsageTable title="今日成员用量（前 50）" rows={usage.members} limit={usage.memberLimit} member />
      </div>
    </div>
  );
}

function RecentTab(): React.JSX.Element {
  const [group, setGroup] = useState<string | null>(null);
  const query = useApiQuery<WebSearchRecentCall[]>({
    queryKey: ["web-search-recent", group ?? ""],
    fetcher: (signal) => webSearchApi.recent(group, signal),
    invalidation: { resources: ["web_search"] },
  });
  const rows = query.data ?? [];
  const groups = useMemo(() => {
    const seen = new Map<string, string>();
    for (const row of rows) if (row.groupId) seen.set(row.groupId, row.groupName ? `${row.groupName}（${row.groupId}）` : row.groupId);
    return [...seen.entries()].map(([value, label]) => ({ value, label }));
  }, [rows]);
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  return (
    <div className="web-recent">
      <div className="web-toolbar">
        <Select
          allowClear
          placeholder="按群筛选"
          style={{ minWidth: 220 }}
          value={group ?? undefined}
          options={groups}
          onChange={(value) => setGroup(value ?? null)}
        />
        <span className="sbx-muted">只保存在内存里（最多 200 条），重启后清空；成员退出 Agent 记忆时会一并抹掉他的搜索词。</span>
        <span className="sbx-toolbar-spacer" />
        <Button icon={<ReloadOutlined />} loading={query.refreshing} onClick={query.reload}>刷新</Button>
      </div>
      <Table
        size="small"
        rowKey={(row) => `${row.at}:${row.tool}:${row.query ?? row.host ?? ""}`}
        dataSource={rows}
        loading={!query.data}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        scroll={{ x: 720 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有调用记录" /> }}
        columns={[
          { title: "时间", dataIndex: "at", width: 110, render: (value: number) => <Tooltip title={formatEpoch(value)}>{relativeEpoch(value)}</Tooltip> },
          { title: "工具", dataIndex: "tool", width: 90, render: (value: string) => (value === "web_fetch" ? <Tag>读网页</Tag> : <Tag color="geekblue">搜索</Tag>) },
          { title: "内容", key: "content", render: (_: unknown, row: WebSearchRecentCall) => row.query ?? row.host ?? "—" },
          {
            title: "结果",
            dataIndex: "status",
            width: 150,
            render: (value: WebSearchRecentCall["status"], row: WebSearchRecentCall) => (
              <>
                <Tag color={RECENT_STATUS_META[value].color}>{RECENT_STATUS_META[value].label}</Tag>
                {value === "error" && row.error ? <span className="sbx-muted">{ERROR_LABELS[row.error] ?? row.error}</span> : null}
                {row.results != null && value !== "error" ? <span className="sbx-muted">{row.results} 条</span> : null}
                {row.chars != null ? <span className="sbx-muted">{row.chars} 字</span> : null}
              </>
            ),
          },
          { title: "耗时", dataIndex: "ms", width: 80, render: (value?: number | null) => (value != null ? `${(value / 1000).toFixed(1)}s` : "—") },
          { title: "群", dataIndex: "groupId", width: 160, render: (value: string | null, row: WebSearchRecentCall) => row.groupName ?? value ?? "—" },
          { title: "成员", dataIndex: "userId", width: 110 },
        ]}
      />
    </div>
  );
}

function TestTab({ status }: { status: WebSearchStatus }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [queryText, setQueryText] = useState("");
  const [count, setCount] = useState(5);
  const [site, setSite] = useState("");
  const [lang, setLang] = useState<string | undefined>(undefined);
  const [after, setAfter] = useState("");
  const [searching, setSearching] = useState(false);
  const [result, setResult] = useState<WebSearchTestResult | null>(null);
  const [url, setUrl] = useState("");
  const [fetching, setFetching] = useState(false);
  const [page, setPage] = useState<WebFetchTestResult | null>(null);
  const disabled = !status.configured;
  const runSearch = async () => {
    if (!queryText.trim()) return;
    setSearching(true);
    try {
      setResult(await webSearchApi.test({ query: queryText.trim(), count, site: site.trim() || undefined, lang, after: after.trim() || undefined }));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "搜索失败");
    } finally {
      setSearching(false);
    }
  };
  const runFetch = async (target?: string) => {
    const value = (target ?? url).trim();
    if (!value) return;
    setUrl(value);
    setFetching(true);
    try {
      setPage(await webSearchApi.fetchTest(value));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "抓取失败");
    } finally {
      setFetching(false);
    }
  };
  return (
    <div className="web-test">
      <Alert
        className="section-alert"
        type="info"
        showIcon
        message="试搜是真实请求"
        description="有结果的请求按 DeepSeek 价目计费，并计入全站当日预算；不占任何群或成员的次数。"
      />
      <Card size="small" title="试搜">
        <div className="web-form">
          <Space.Compact className="web-search-box">
            <Input
              placeholder="关键词，例如：DeepSeek V4 发布"
              value={queryText}
              maxLength={128}
              showCount
              disabled={disabled}
              onChange={(event) => setQueryText(event.target.value)}
              onPressEnter={() => void runSearch()}
            />
            <Button type="primary" icon={<SearchOutlined />} loading={searching} disabled={disabled || !queryText.trim()} onClick={() => void runSearch()}>搜索</Button>
          </Space.Compact>
          <div className="web-form-row">
            <InputNumber min={1} max={20} value={count} addonBefore="条数" onChange={(value) => setCount(typeof value === "number" ? value : 5)} />
            <Input placeholder="只搜某网站，如 bilibili.com" value={site} onChange={(event) => setSite(event.target.value)} style={{ maxWidth: 240 }} />
            <Select allowClear placeholder="语言" value={lang} onChange={setLang} style={{ width: 110 }} options={["zh", "en", "ja", "ko"].map((value) => ({ value, label: value }))} />
            <Input placeholder="某天之后 YYYY-MM-DD" value={after} onChange={(event) => setAfter(event.target.value)} style={{ maxWidth: 200 }} />
          </div>
        </div>
        {result && (
          <div className="web-results" aria-label="试搜结果">
            <p className="sbx-muted">
              实际查询：<code>{result.query}</code> · {result.results.length} 条 · {(result.durationMs / 1000).toFixed(1)}s{result.attempts > 1 ? ` · 重试 ${result.attempts - 1} 次` : ""}{result.billed ? "" : " · 无结果不计费"}
            </p>
            {result.results.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有搜到结果" />}
            <ol className="web-hit-list">
              {result.results.map((hit) => {
                const quality = scoreLabel(hit.score, hit.authority);
                return (
                  <li key={hit.url} className="web-hit">
                    <div className="web-hit-head">
                      <a href={hit.url} target="_blank" rel="noreferrer noopener">{hit.title}</a>
                      {quality && <Tooltip title={`rerank ${hit.score ?? "—"} · authority ${hit.authority ?? "—"}`}><Tag color={quality.color}>{quality.label}</Tag></Tooltip>}
                    </div>
                    <span className="sbx-muted">{hit.site}{hit.date ? ` · ${hit.date}` : ""}</span>
                    {hit.summary && <p>{hit.summary}</p>}
                    <Button size="small" type="link" disabled={!status.settings.fetchEnabled} onClick={() => void runFetch(hit.url)}>读正文</Button>
                  </li>
                );
              })}
            </ol>
          </div>
        )}
      </Card>
      <Card size="small" title="试读网页">
        <Space.Compact className="web-search-box">
          <Input
            placeholder="https://…"
            value={url}
            disabled={disabled || !status.settings.fetchEnabled}
            onChange={(event) => setUrl(event.target.value)}
            onPressEnter={() => void runFetch()}
          />
          <Button type="primary" loading={fetching} disabled={disabled || !status.settings.fetchEnabled || !url.trim()} onClick={() => void runFetch()}>读取</Button>
        </Space.Compact>
        {page && (
          <div className="web-page-preview" aria-label="网页正文预览">
            <p className="sbx-muted">{page.title ?? "（无标题）"} · {page.chars} 字 · {(page.durationMs / 1000).toFixed(1)}s{page.truncated ? " · 只显示前 4000 字" : ""}</p>
            <pre>{page.preview}</pre>
          </div>
        )}
      </Card>
    </div>
  );
}

function NumberField({ field, draft, server, errors, disabled, onChange }: {
  field: WebNumericKey;
  draft: WebSearchSettings;
  server: WebSearchSettings;
  errors: Record<string, string>;
  disabled?: boolean;
  onChange: (key: WebNumericKey, value: number) => void;
}): React.JSX.Element {
  const range = WEB_SETTING_RANGES[field];
  const changed = draft[field] !== server[field];
  return (
    <label className="sbx-field">
      <span className="sbx-field-label">
        {range.label}
        {changed && <Tag color="gold">原 {server[field]}</Tag>}
      </span>
      <InputNumber
        min={range.min}
        max={range.max}
        value={draft[field]}
        disabled={disabled}
        status={errors[field] ? "error" : undefined}
        onChange={(value) => onChange(field, typeof value === "number" ? Math.round(value) : range.min)}
        addonAfter={range.unit}
        style={{ width: "100%" }}
      />
      {errors[field] && <span className="sbx-field-error">{errors[field]}</span>}
    </label>
  );
}

function ListField({ label, value, placeholder, error, parse, onChange }: {
  label: string;
  value: string[];
  placeholder: string;
  error?: string;
  parse: (text: string) => { values: string[]; invalid: string[] };
  onChange: (values: string[]) => void;
}): React.JSX.Element {
  const [text, setText] = useState(value.join("\n"));
  const [invalid, setInvalid] = useState<string[]>([]);
  useEffect(() => {
    if (parse(text).values.join(",") !== value.join(",")) setText(value.join("\n"));
  }, [value]);
  return (
    <label className="sbx-field">
      <span className="sbx-field-label">{label}</span>
      <Input.TextArea
        value={text}
        autoSize={{ minRows: 2, maxRows: 6 }}
        placeholder={placeholder}
        status={error || invalid.length ? "error" : undefined}
        onChange={(event) => {
          setText(event.target.value);
          const parsed = parse(event.target.value);
          setInvalid(parsed.invalid);
          onChange(parsed.values);
        }}
      />
      {(error || invalid.length > 0) && (
        <span className="sbx-field-error">{invalid.length ? `忽略了不合法的项：${invalid.slice(0, 3).join("、")}` : error}</span>
      )}
    </label>
  );
}

const groupParser = (text: string) => {
  const parsed = parseGroupIds(text);
  return { values: parsed.ids, invalid: parsed.invalid };
};
const domainParser = (text: string) => {
  const parsed = parseDomainList(text);
  return { values: parsed.domains, invalid: parsed.invalid };
};

function PolicyTab({ status, onSaved }: { status: WebSearchStatus; onSaved: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const server = status.settings;
  const [draft, setDraft] = useState<WebSearchSettings>(server);
  const [saving, setSaving] = useState(false);
  const [base, setBase] = useState<WebSearchSettings>(server);
  useEffect(() => {
    // 服务器设置变了：本地没改过就跟随服务器，改过就保留草稿。
    if (webSettingsEqual(base, server)) return;
    setDraft((current) => (webSettingsEqual(current, base) ? server : current));
    setBase(server);
  }, [server]);
  const dirty = !webSettingsEqual(draft, server);
  useUnsavedChanges(dirty);
  const errors = useMemo(() => validateWebSettings(draft), [draft]);
  const hasErrors = Object.keys(errors).length > 0;
  const update = <K extends keyof WebSearchSettings>(key: K, value: WebSearchSettings[K]) => setDraft({ ...draft, [key]: value });
  const save = async () => {
    setSaving(true);
    try {
      const saved = await webSearchApi.saveSettings(draft);
      setDraft(saved);
      onSaved();
      message.success("网页搜索策略已保存，立即生效");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };
  const num = (key: WebNumericKey, disabled?: boolean) => (
    <NumberField key={key} field={key} draft={draft} server={server} errors={errors} disabled={disabled} onChange={(field, value) => update(field, value)} />
  );
  return (
    <div className="sbx-policy">
      <div className="sbx-policy-summary">
        <div>
          <strong>{describeWebAccess(draft)}</strong>
          <span className="sbx-muted">设置保存在 data/agent_web_search/settings.json，不需要重启 Bot。按满额估算每月最多约 {monthlyEstimate(draft.globalDailyCalls, status.usage.pricePerCallYuan)}。</span>
        </div>
        <span className="sbx-toolbar-spacer" />
        <SaveStatus dirty={dirty} saving={saving} />
        <Button icon={<UndoOutlined />} disabled={!dirty} onClick={() => setDraft(server)}>撤销修改</Button>
        <Popconfirm title="恢复为默认策略？" description="只改草稿，点保存后才生效。" onConfirm={() => setDraft(status.defaults)}>
          <Button>恢复默认</Button>
        </Popconfirm>
        <Button type="primary" icon={<SaveOutlined />} disabled={!dirty || hasErrors} loading={saving} onClick={() => void save()}>保存</Button>
      </div>
      <div className="sbx-grid">
        <Card size="small" title="开放范围" className="sbx-card-wide">
          <div className="sbx-switch-row">
            <Switch checked={draft.enabled} onChange={(value) => update("enabled", value)} aria-label="允许 Agent 联网搜索" />
            <div>
              <strong>允许 Agent 联网搜索</strong>
              <span className="sbx-muted">关闭后 Agent 发现不到搜索工具，会如实说明没法联网查。</span>
            </div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.adminOnly} onChange={(value) => update("adminOnly", value)} aria-label="仅群主和管理员可用" />
            <div>
              <strong>仅群主和管理员可用</strong>
              <span className="sbx-muted">打开后普通成员的搜索请求会被如实拒绝。</span>
            </div>
          </div>
          <Radio.Group
            className="sbx-gap"
            value={draft.groupMode}
            onChange={(event) => update("groupMode", event.target.value as WebSearchSettings["groupMode"])}
            optionType="button"
            options={[
              { value: "all", label: "所有群" },
              { value: "allowlist", label: "只开放白名单群" },
            ]}
          />
          <div className="sbx-field-grid">
            {draft.groupMode === "allowlist" && (
              <ListField label="白名单群号" value={draft.allowedGroups} parse={groupParser} error={errors.allowedGroups} placeholder="每行一个群号，也可以用逗号分隔" onChange={(values) => update("allowedGroups", values)} />
            )}
            <ListField label="禁用的群（优先于白名单）" value={draft.blockedGroups} parse={groupParser} placeholder="这些群永远不能联网搜索" onChange={(values) => update("blockedGroups", values)} />
          </div>
        </Card>
        <Card size="small" title="次数与预算">
          <p className="sbx-muted">按北京时间每天零点重置；失败、无结果和命中缓存都不扣次数（DeepSeek 也不收费）。</p>
          {num("memberDailyCalls")}
          {num("groupDailyCalls")}
          {num("globalDailyCalls")}
          {num("memberCooldownSeconds")}
          {num("maxCallsPerTurn")}
        </Card>
        <Card size="small" title="结果">
          {num("defaultCount")}
          {num("maxCount")}
          {num("cacheTtlMinutes")}
          <div className="sbx-switch-row">
            <Switch checked={draft.allowFullContent} onChange={(value) => update("allowFullContent", value)} aria-label="允许整页正文" />
            <div>
              <strong>允许搜索时带回整页正文</strong>
              <span className="sbx-muted">每条结果附正文会大幅增加上下文；一般让 Agent 用“读网页”打开单条就够了。</span>
            </div>
          </div>
        </Card>
        <Card size="small" title="读网页">
          <div className="sbx-switch-row">
            <Switch checked={draft.fetchEnabled} onChange={(value) => update("fetchEnabled", value)} aria-label="允许读网页" />
            <div>
              <strong>允许 Agent 打开网页读正文</strong>
              <span className="sbx-muted">也用于读成员发来的链接；内网和本机地址一律拒绝。</span>
            </div>
          </div>
          {num("maxFetchChars", !draft.fetchEnabled)}
        </Card>
        <Card size="small" title="屏蔽网站" className="sbx-card-wide">
          <ListField
            label="这些网站的结果会被过滤，也不能被打开（子域名一并屏蔽）"
            value={draft.blockedDomains}
            parse={domainParser}
            error={errors.blockedDomains}
            placeholder="每行一个域名，例如 example.com"
            onChange={(values) => update("blockedDomains", values)}
          />
        </Card>
      </div>
    </div>
  );
}

export function WebSearchPage(): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseTab(searchParams.get("web.tab"));
  const statusQuery = useApiQuery<WebSearchStatus>({
    queryKey: ["web-search-status"],
    fetcher: (signal) => webSearchApi.status(signal),
    invalidation: { resources: ["web_search"] },
  });
  const status = statusQuery.data;
  const setTab = (next: WebTab) => {
    setSearchParams(updateSearchParams(searchParams, { "web.tab": next === "overview" ? null : next }), { replace: true });
  };
  if (!status) {
    return (
      <div className="sbx-page">
        <h2 className="sbx-title">网页搜索</h2>
        {statusQuery.error ? <QueryErrorAlert error={statusQuery.error} onRetry={statusQuery.reload} /> : <Skeleton active />}
      </div>
    );
  }
  return (
    <div className="sbx-page">
      <div className="sbx-heading">
        <div>
          <h2 className="sbx-title"><GlobalOutlined /> 网页搜索</h2>
          <p>Agent 用 DeepSeek 官方搜索查最新消息、核对事实、打开网页；这里管开放范围、次数预算和费用</p>
        </div>
      </div>
      <WebSearchStatusBar status={status} reloading={statusQuery.refreshing} onReload={statusQuery.reload} />
      {!status.configured && (
        <Alert
          className="section-alert"
          type="info"
          showIcon
          message="还没有配置 DeepSeek 密钥"
          description="在「环境配置 → Agent 网页搜索」填写 AGENT_WEB_SEARCH_API_KEY；如果模型提供商本来就是 api.deepseek.com，会自动复用它的密钥。"
        />
      )}
      <Tabs
        className="sbx-tabs"
        activeKey={tab}
        onChange={(key) => setTab(key as WebTab)}
        destroyOnHidden
        items={[
          { key: "overview", label: <span><DashboardOutlined /> 概览</span>, children: <OverviewTab status={status} /> },
          { key: "recent", label: <span><HistoryOutlined /> 最近调用</span>, children: <RecentTab /> },
          { key: "test", label: <span><ExperimentOutlined /> 试搜</span>, children: <TestTab status={status} /> },
          { key: "policy", label: <span><SafetyCertificateOutlined /> 策略与限额</span>, children: <PolicyTab status={status} onSaved={statusQuery.reload} /> },
        ]}
      />
    </div>
  );
}
