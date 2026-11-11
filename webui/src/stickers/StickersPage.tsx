import { AppstoreOutlined, DeleteOutlined, EyeOutlined, LineChartOutlined, MedicineBoxOutlined, PictureOutlined, ReloadOutlined, RobotOutlined, SafetyCertificateOutlined, SmileOutlined } from "@ant-design/icons";
import { Button, Skeleton, Tabs, Tag, Tooltip } from "antd";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { AiTab } from "./StickerAiTab";
import { InsightsTab } from "./StickerInsightsTab";
import { LibraryTab } from "./StickerLibraryTab";
import { HealthTab, PacksTab, TrashTab } from "./StickerManageTabs";
import { PolicyTab } from "./StickerPolicyTab";
import { PreviewTab } from "./StickerPreviewTab";
import { stickersApi } from "./stickers-api";
import { describeQuiet, describeSteal, describeStickerAccess } from "./stickers-meta";
import type { StickerOverview } from "./types";

type StickerTab = "library" | "ai" | "policy" | "preview" | "insights" | "health" | "packs" | "trash";
const TABS: StickerTab[] = ["library", "ai", "policy", "preview", "insights", "health", "packs", "trash"];

function parseTab(value: string | null): StickerTab {
  return TABS.includes(value as StickerTab) ? (value as StickerTab) : "library";
}

export function StickerStatusBar({ overview, onReload, reloading }: { overview: StickerOverview; onReload: () => void; reloading: boolean }): React.JSX.Element {
  const { settings, counts, usage } = overview;
  const tone = !settings.enabled ? "muted" : counts.active === 0 ? "warn" : "ok";
  const label = !settings.enabled ? "已关闭" : counts.active === 0 ? "库里还没有可用的表情包" : `${overview.frequencyLabels[settings.frequency]}发`;
  return (
    <section className={`sbx-status is-${tone}`} aria-label="表情包状态">
      <div className="sbx-status-main">
        <span className={`sbx-dot is-${tone}`} aria-hidden="true" />
        <div className="sbx-status-copy">
          <strong>表情包 · {label}</strong>
          <span>{describeStickerAccess(settings)}</span>
        </div>
      </div>
      <div className="sbx-status-chips">
        <Tag color="green">可用 {counts.active}</Tag>
        {counts.pending > 0 && <Tag color="gold">待审核 {counts.pending}</Tag>}
        {counts.stolen > 0 && <Tooltip title={describeSteal(settings)}><Tag color="purple">偷来 {counts.stolen}</Tag></Tooltip>}
        <Tooltip title={overview.vision.ready ? `识图模型：${overview.vision.model}` : "没有能看图的模型，AI 标注用不了"}>
          <Tag icon={<RobotOutlined />} color={overview.vision.ready && settings.aiLabelEnabled ? "blue" : "default"}>AI 标注 {counts.aiLabeled}/{counts.total}</Tag>
        </Tooltip>
        <Tooltip title="今日真正发出去的表情包（全部群）">
          <Tag>今日发出 {usage.totals.sent ?? 0}</Tag>
        </Tooltip>
        {(usage.totals.private_sent ?? 0) > 0 && <Tag>私聊 {usage.totals.private_sent}</Tag>}
        {overview.counts.pinned > 0 && <Tag>置顶 {overview.counts.pinned}</Tag>}
        {usage.quietNow && <Tooltip title={describeQuiet(settings)}><Tag color="gold">安静时段中</Tag></Tooltip>}
        {Object.keys(settings.groupOverrides).length > 0 && <Tag>{Object.keys(settings.groupOverrides).length} 个群单独设置</Tag>}
        {overview.index.corrupt && <Tag color="red">索引坏过</Tag>}
      </div>
      <div className="sbx-status-actions">
        <Button size="small" icon={<ReloadOutlined />} loading={reloading} onClick={onReload}>刷新</Button>
      </div>
    </section>
  );
}

export function StickersPage(): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseTab(searchParams.get("stk.tab"));
  const query = useApiQuery<StickerOverview>({
    queryKey: ["stickers-overview"],
    fetcher: (signal) => stickersApi.overview(signal),
    invalidation: { resources: ["stickers"] },
  });
  const overview = query.data;
  const setTab = (next: StickerTab) => {
    setSearchParams(updateSearchParams(searchParams, { "stk.tab": next === "library" ? null : next }), { replace: true });
  };
  if (!overview) {
    return (
      <div className="sbx-page">
        <h2 className="sbx-title">表情包</h2>
        {query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Skeleton active />}
      </div>
    );
  }
  return (
    <div className="sbx-page">
      <div className="sbx-heading">
        <div>
          <h2 className="sbx-title"><SmileOutlined /> 表情包</h2>
          <p>Agent 在回复里写 [表情:名字] 就会单独发一张表情包，和分条消息一样有节奏；这里管表情包库与合集、AI 自动起名打标签、Bot 偷图、发送节奏、每个群的单独设置，还能看用量统计和给库做体检</p>
        </div>
      </div>
      <StickerStatusBar overview={overview} reloading={query.refreshing} onReload={query.reload} />
      <Tabs
        className="sbx-tabs"
        activeKey={tab}
        onChange={(key) => setTab(key as StickerTab)}
        destroyOnHidden
        items={[
          { key: "library", label: <span><PictureOutlined /> 表情包库</span>, children: <LibraryTab overview={overview} onChanged={query.reload} /> },
          { key: "ai", label: <span><RobotOutlined /> AI 标注与偷图</span>, children: <AiTab overview={overview} onChanged={query.reload} /> },
          { key: "policy", label: <span><SafetyCertificateOutlined /> 策略与频率</span>, children: <PolicyTab overview={overview} onSaved={query.reload} /> },
          { key: "preview", label: <span><EyeOutlined /> 菜单预览</span>, children: <PreviewTab overview={overview} /> },
          { key: "insights", label: <span><LineChartOutlined /> 统计</span>, children: <InsightsTab overview={overview} onChanged={query.reload} /> },
          { key: "health", label: <span><MedicineBoxOutlined /> 体检</span>, children: <HealthTab overview={overview} onChanged={query.reload} /> },
          { key: "packs", label: <span><AppstoreOutlined /> 合集{overview.counts.packs ? ` ${overview.counts.packs}` : ""}</span>, children: <PacksTab overview={overview} onChanged={query.reload} /> },
          { key: "trash", label: <span><DeleteOutlined /> 回收站{overview.counts.trash ? ` ${overview.counts.trash}` : ""}</span>, children: <TrashTab onChanged={query.reload} /> },
        ]}
      />
    </div>
  );
}
