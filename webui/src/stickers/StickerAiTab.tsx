// 「AI 标注与偷图」标签：识图模型状态、标注覆盖率、批量重新标注（进度/取消/逐张结果）、
// 试一张（不入库）、Bot 偷来的表情包（来源、发图的人、一键审核/删除）。
import { CloseCircleOutlined, ExperimentOutlined, RobotOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Drawer, Empty, Popconfirm, Progress, Radio, Statistic, Table, Tag, Tooltip } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { previewLabel, stickerImageUrl, stickersApi } from "./stickers-api";
import { confidenceText, describeJob, describeOrigin, describeSteal, formatEta, jobPercent, LABEL_MODE_HINTS, LABEL_MODE_LABELS } from "./stickers-meta";
import type { LabelJobDetail, LabelJobSummary, LabelMode, LabelScope, StickerLabel, StickerOverview } from "./types";

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

const POLL_MS = 1500;

/** 跟踪批量任务：有任务在跑就每 1.5 秒拉一次，跑完通知页面刷新。 */
export function useLabelJobs(initial: LabelJobSummary[], onFinished: () => void): {
  jobs: LabelJobSummary[];
  running: LabelJobSummary | null;
  track: (job: LabelJobSummary) => void;
  cancel: (id: string) => Promise<void>;
} {
  const [jobs, setJobs] = useState<LabelJobSummary[]>(initial);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;
  useEffect(() => {
    setJobs((current) => {
      const known = new Map(current.map((job) => [job.id, job]));
      return initial.map((job) => (known.get(job.id)?.status === "running" && job.status === "running" ? known.get(job.id)! : job));
    });
  }, [initial]);
  const running = jobs.find((job) => job.status === "running") ?? null;
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => {
      void stickersApi
        .job(running.id)
        .then((detail) => {
          setJobs((current) => current.map((job) => (job.id === detail.id ? detail : job)));
          if (detail.status !== "running") finishedRef.current();
        })
        .catch(() => undefined);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [running?.id]);
  const track = useCallback((job: LabelJobSummary) => setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]), []);
  const cancel = useCallback(async (id: string) => {
    const job = await stickersApi.cancelJob(id);
    setJobs((current) => current.map((item) => (item.id === id ? { ...item, ...job } : item)));
  }, []);
  return { jobs, running, track, cancel };
}

export function JobBanner({ job, onCancel }: { job: LabelJobSummary; onCancel: () => void }): React.JSX.Element {
  return (
    <div className="stk-job" role="status" aria-label="批量 AI 标注进度">
      <RobotOutlined spin={job.status === "running"} />
      <div className="stk-job-main">
        <strong>{describeJob(job)}{job.status === "running" && formatEta(job.etaMs) ? `，${formatEta(job.etaMs)}` : ""}</strong>
        <Progress percent={jobPercent(job)} size="small" status={job.status === "running" ? "active" : job.failed ? "exception" : "success"} showInfo={false} />
      </div>
      {job.status === "running" && <Button size="small" icon={<CloseCircleOutlined />} onClick={onCancel}>取消</Button>}
    </div>
  );
}

function VisionCard({ overview }: { overview: StickerOverview }): React.JSX.Element {
  const { vision, settings } = overview;
  const stats = vision.stats;
  return (
    <Card size="small" title={<span><RobotOutlined /> 识图模型</span>} className="sbx-card-wide">
      {vision.ready ? (
        <p className="stk-ai-model">
          <Tag color="green">可用</Tag>
          <code>{vision.model || "（未命名模型）"}</code>
          {vision.provider && vision.provider !== "default" && <span className="sbx-muted"> · {vision.provider}</span>}
        </p>
      ) : (
        <Alert type="warning" showIcon message="没有能看图的模型" description="在「环境配置」里填 AI_VISION_MODEL（或用能看图的对话模型），AI 标注和偷图起名才会工作；没有它时上传要手填名字，偷图要模型自己起名。" />
      )}
      {!settings.aiLabelEnabled && <Alert type="info" showIcon message="AI 标注在「策略与频率」里关掉了" />}
      <div className="stk-stats stk-stats-compact">
        <Statistic title="本次启动请求" value={stats.requests} />
        <Statistic title="成功" value={stats.ok} />
        <Statistic title="命中缓存" value={stats.cached} />
        <Statistic title="失败" value={stats.failed} />
      </div>
      {stats.lastError && <p className="sbx-muted">最近一次失败：{stats.lastError.at} · {stats.lastError.error}</p>}
    </Card>
  );
}

function CoverageCard({ overview, onStart, busy }: { overview: StickerOverview; onStart: (scope: LabelScope, mode: LabelMode) => void; busy: boolean }): React.JSX.Element {
  const [mode, setMode] = useState<LabelMode>("fill");
  const { counts } = overview;
  const percent = counts.total ? Math.round((counts.aiLabeled / counts.total) * 100) : 0;
  const disabled = busy || !overview.vision.ready || !overview.settings.aiLabelEnabled;
  const placeholders = overview.stickers.filter((item) => item.placeholderName).length;
  const unsure = overview.stickers.filter((item) => item.ai && !item.ai.undone && typeof item.ai.confidence === "number" && item.ai.confidence < 0.5).length;
  return (
    <Card size="small" title={<span><ThunderboltOutlined /> 批量重新标注</span>}>
      <div className="stk-coverage">
        <Progress type="circle" size={72} percent={percent} format={() => `${counts.aiLabeled}/${counts.total}`} />
        <div>
          <strong>{counts.unlabeled > 0 ? `还有 ${counts.unlabeled} 张没被 AI 看过` : "全部都被 AI 看过了"}</strong>
          <span className="sbx-muted">识图模型逐张看图起名字、打标签、写什么时候发、认出图上的字（图上的字能在库里搜到）。走后台车道，不和群聊抢模型。</span>
        </div>
      </div>
      <Radio.Group value={mode} onChange={(event) => setMode(event.target.value as LabelMode)} optionType="button" options={(Object.keys(LABEL_MODE_LABELS) as LabelMode[]).map((key) => ({ value: key, label: LABEL_MODE_LABELS[key] }))} />
      <p className="sbx-muted">{LABEL_MODE_HINTS[mode]}。每张都留了标注前的样子，可以在编辑框或批量操作里撤销。</p>
      <div className="stk-actions">
        <Button type="primary" icon={<RobotOutlined />} disabled={disabled || counts.unlabeled === 0} onClick={() => onStart("unlabeled", mode)}>标注没标过的 {counts.unlabeled} 张</Button>
        <Popconfirm title={`让 AI 重新看全部 ${counts.total} 张？`} description={mode === "replace" ? "名字和标签都会换成 AI 起的（可撤销）。" : LABEL_MODE_HINTS[mode]} onConfirm={() => onStart("all", mode)} disabled={disabled || counts.total === 0}>
          <Button disabled={disabled || counts.total === 0}>全部重新标注</Button>
        </Popconfirm>
      </div>
      <div className="stk-actions">
        <Tooltip title="名字还是 IMG_2033 这种文件名的，模型不知道什么时候发">
          <Button size="small" disabled={disabled || placeholders === 0} onClick={() => onStart("placeholder", mode)}>只标名字是文件名的 {placeholders} 张</Button>
        </Tooltip>
        <Tooltip title="上次 AI 自己说没把握（低于 50%）的，再看一遍">
          <Button size="small" disabled={disabled || unsure === 0} onClick={() => onStart("low_confidence", mode)}>重标 AI 没把握的 {unsure} 张</Button>
        </Tooltip>
      </div>
    </Card>
  );
}

function PlaygroundCard({ ready }: { ready: boolean }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [result, setResult] = useState<{ label: StickerLabel; duplicate: { id: string; name: string } | null } | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview);
  }, [preview]);
  const run = async (file: File) => {
    setPreview(URL.createObjectURL(file));
    setLoading(true);
    setResult(null);
    try {
      setResult(await previewLabel(file, file.name));
    } catch (error) {
      message.error(errorText(error, "识别失败"));
    } finally {
      setLoading(false);
    }
  };
  const label = result?.label;
  return (
    <Card size="small" title={<span><ExperimentOutlined /> 试一张（不入库）</span>}>
      <p className="sbx-muted">挑一张图看看 AI 会怎么起名，用来调「命名要求」或换模型后验收。</p>
      <Button icon={<ExperimentOutlined />} disabled={!ready} loading={loading} onClick={() => inputRef.current?.click()}>选一张图</Button>
      <input
        ref={inputRef}
        type="file"
        hidden
        accept="image/png,image/jpeg,image/gif,image/webp"
        aria-label="试标注的图片"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void run(file);
          event.target.value = "";
        }}
      />
      {preview && (
        <div className="stk-play">
          <img src={preview} alt="" />
          {label && (
            <dl className="stk-play-result">
              {label.ok ? (
                <>
                  <dt>名字</dt>
                  <dd><strong>{label.name}</strong> {label.cached && <Tag>缓存</Tag>}</dd>
                  <dt>标签</dt>
                  <dd>{label.tags.length ? label.tags.map((tag) => <Tag key={tag}>{tag}</Tag>) : "—"}</dd>
                  <dt>什么时候发</dt>
                  <dd>{label.description || "—"}</dd>
                  <dt>图上的字</dt>
                  <dd>{label.text || "—"}</dd>
                  <dt>判断</dt>
                  <dd>
                    {label.kindLabel} · {confidenceText(label.confidence)} · {label.safe ? <Tag color="green">适合当表情包</Tag> : <Tag color="red">可能不合适</Tag>}
                  </dd>
                  <dt>耗时</dt>
                  <dd>{(label.durationMs / 1000).toFixed(1)} 秒 · {label.model}</dd>
                </>
              ) : (
                <>
                  <dt>没认出来</dt>
                  <dd>{label.reasonText ?? label.reason}</dd>
                </>
              )}
              {result?.duplicate && (
                <>
                  <dt>库里</dt>
                  <dd>已经有这张了（{result.duplicate.name}）</dd>
                </>
              )}
            </dl>
          )}
        </div>
      )}
    </Card>
  );
}

function StolenCard({ overview, onChanged }: { overview: StickerOverview; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const stolen = useMemo(() => overview.stickers.filter((item) => item.source === "stolen").sort((a, b) => (b.createdAt ?? "").localeCompare(a.createdAt ?? "")), [overview.stickers]);
  const act = async (action: () => Promise<unknown>, done: string) => {
    try {
      await action();
      message.success(done);
      onChanged();
    } catch (error) {
      message.error(errorText(error, "操作失败"));
    }
  };
  return (
    <Card size="small" title={`Bot 偷来的表情包（${stolen.length}）`} className="sbx-card-wide">
      <p className="sbx-muted">{describeSteal(overview.settings)}</p>
      {stolen.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没偷过：群友发了好玩的表情包，Bot 觉得以后用得上就会自己存下来" />
      ) : (
        <Table
          size="small"
          rowKey="id"
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          dataSource={stolen}
          scroll={{ x: 640 }}
          columns={[
            { title: "图", dataIndex: "id", width: 64, render: (_: unknown, row) => <img className="stk-mini" src={stickerImageUrl(row)} alt={row.name} loading="lazy" /> },
            { title: "名字", dataIndex: "name", render: (name: string, row) => <span><strong>{name}</strong> {row.tags.slice(0, 2).map((tag) => <Tag key={tag}>{tag}</Tag>)}</span> },
            { title: "来源", key: "origin", render: (_: unknown, row) => <span className="sbx-muted">{describeOrigin(row, overview.groupNames) || "—"}</span> },
            { title: "时间", dataIndex: "createdAt", width: 150, render: (value: string | null) => <span className="sbx-muted">{value ?? "—"}</span> },
            {
              title: "",
              key: "actions",
              width: 150,
              render: (_: unknown, row) => (
                <span className="stk-actions">
                  {row.status === "pending" && <Button size="small" onClick={() => void act(() => stickersApi.update(row.id, { status: "active", enabled: true }), "已审核通过")}>通过</Button>}
                  <Popconfirm title="删掉这张偷来的图？" onConfirm={() => void act(() => stickersApi.remove(row.id), "已删除")}>
                    <Button size="small" danger>删除</Button>
                  </Popconfirm>
                </span>
              ),
            },
          ]}
        />
      )}
    </Card>
  );
}

function JobDrawer({ jobId, onClose }: { jobId: string | null; onClose: () => void }): React.JSX.Element {
  const [detail, setDetail] = useState<LabelJobDetail | null>(null);
  useEffect(() => {
    setDetail(null);
    if (!jobId) return undefined;
    let alive = true;
    const load = () => void stickersApi.job(jobId).then((job) => alive && setDetail(job)).catch(() => undefined);
    load();
    const timer = window.setInterval(load, POLL_MS * 2);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [jobId]);
  return (
    <Drawer open={Boolean(jobId)} onClose={onClose} title={detail ? describeJob(detail) : "标注结果"} width={560}>
      {detail && (
        <Table
          size="small"
          rowKey="id"
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          dataSource={detail.results}
          columns={[
            { title: "表情包", key: "name", render: (_: unknown, row) => row.after?.name ?? row.name ?? row.id },
            {
              title: "结果",
              key: "result",
              render: (_: unknown, row) =>
                !row.ok ? (
                  <Tag color="red">{row.reasonText ?? "失败"}</Tag>
                ) : row.changed && row.changed.length ? (
                  <Tooltip title={`原来：${row.before?.name} · ${row.before?.tags.join("、") || "无标签"}`}>
                    <Tag color="blue">改了 {row.changed.map((field) => ({ name: "名字", tags: "标签", description: "说明" })[field] ?? field).join("、")}</Tag>
                  </Tooltip>
                ) : (
                  <Tag>没变</Tag>
                ),
            },
            { title: "备注", key: "note", render: (_: unknown, row) => <span className="sbx-muted">{[row.held ? "转待审核" : "", row.text ? `图上：${row.text.slice(0, 16)}` : "", row.confidence !== undefined ? confidenceText(row.confidence) : ""].filter(Boolean).join(" · ")}</span> },
          ]}
        />
      )}
    </Drawer>
  );
}

export function AiTab({ overview, onChanged }: { overview: StickerOverview; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const { jobs, running, track, cancel } = useLabelJobs(overview.labelJobs, onChanged);
  const [openJob, setOpenJob] = useState<string | null>(null);
  const start = async (scope: LabelScope, mode: LabelMode, retryOf?: string) => {
    try {
      const job = await stickersApi.startJob(retryOf ? { scope, mode, retryOf } : { scope, mode });
      track(job);
      message.success(`开始标注 ${job.total} 张`);
    } catch (error) {
      message.error(errorText(error, "没能开始"));
    }
  };
  return (
    <div className="sbx-grid stk-ai-tab">
      <VisionCard overview={overview} />
      {running && <div className="sbx-card-wide"><JobBanner job={running} onCancel={() => void cancel(running.id)} /></div>}
      <CoverageCard overview={overview} onStart={(scope, mode) => void start(scope, mode)} busy={Boolean(running)} />
      <PlaygroundCard ready={overview.vision.ready && overview.settings.aiLabelEnabled} />
      <Card size="small" title="最近的批量标注" className="sbx-card-wide">
        {jobs.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没跑过批量标注（重启后清空）" />
        ) : (
          <ul className="stk-job-list">
            {jobs.map((job) => (
              <li key={job.id}>
                <span>{describeJob(job)}</span>
                <span className="sbx-muted">{job.scopeLabel ? `${job.scopeLabel} · ` : ""}{job.modeLabel} · {new Date(job.createdAt).toLocaleTimeString()}{job.retried ? ` · ${job.retried} 张重试过` : ""}</span>
                {job.status !== "running" && job.failed > 0 && <Button size="small" type="link" disabled={Boolean(running)} onClick={() => void start("retry_failed", job.mode, job.id)}>重试失败的 {job.failed} 张</Button>}
                <Button size="small" type="link" onClick={() => setOpenJob(job.id)}>逐张结果</Button>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <StolenCard overview={overview} onChanged={onChanged} />
      <JobDrawer jobId={openJob} onClose={() => setOpenJob(null)} />
    </div>
  );
}
