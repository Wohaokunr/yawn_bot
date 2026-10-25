import { Button, Tag } from "antd";
import { api } from "../api";
import { useApiQuery } from "../shared";

export interface VisionStatus {
  route: "direct" | "caption" | "none";
  dialogueProvider: string;
  dialogueModel: string;
  dialogueMultimodal: string;
  learnedUnsupported: boolean;
  visionConfigured: boolean;
  visionProvider: string;
  visionModel: string;
  stats: { total: number; loaded: number; failed: number; via: Record<string, number> };
  reasons: { code: string; count: number; message: string; hint: string }[];
  advice: string[];
  settings: { allowedHosts: string[]; httpFallback: boolean; urlPassthrough: boolean; autoCaption: string };
  captionMemory: number;
}

const ROUTE_LABEL: Record<VisionStatus["route"], { text: string; color: string }> = {
  direct: { text: "对话模型直接看图", color: "green" },
  caption: { text: "识图模型转述后再回答", color: "blue" },
  none: { text: "当前看不了图", color: "red" },
};

const VIA_LABEL: Record<string, string> = {
  url: "图片链接",
  segment_base64: "消息内嵌",
  get_image_base64: "协议端 base64",
  get_image_url: "协议端链接",
  get_file_base64: "协议端文件",
  get_file_url: "协议端文件链接",
  local_file: "共享目录",
  local_cache: "本地缓存",
};

function isVisionStatus(value: unknown): value is VisionStatus {
  return Boolean(value && typeof value === "object" && "route" in value && "stats" in value);
}

/** 识图诊断：识图走哪条路、最近 24 小时取图成败、取不到图时该改哪里。 */
export function VisionStatusCard(): React.JSX.Element | null {
  const query = useApiQuery({
    queryKey: ["agent-vision-status"],
    fetcher: (signal) => api<VisionStatus>("/agent/vision/status", { signal }).then((r) => r.data),
  });
  const data = query.data;
  if (!isVisionStatus(data)) return null;
  const route = ROUTE_LABEL[data.route] ?? ROUTE_LABEL.none;
  const model = data.route === "caption" ? `${data.visionProvider}/${data.visionModel}` : `${data.dialogueProvider}/${data.dialogueModel}`;
  const via = Object.entries(data.stats.via)
    .sort((a, b) => b[1] - a[1])
    .map(([key, count]) => `${VIA_LABEL[key] ?? key} ${count}`)
    .join("、");
  return (
    <div className="agent-config-toggle-card agent-vision-card" aria-label="识图诊断">
      <div className="agent-vision-body">
        <div className="agent-config-toggle-title">
          识图状态 <Tag color={route.color}>{route.text}</Tag>
        </div>
        <div className="agent-config-toggle-help">
          模型 {model}
          {data.learnedUnsupported ? "（对话模型最近拒收图片，暂时改走转述）" : ""}
        </div>
        <div className="agent-config-toggle-help">
          {data.stats.total
            ? `近 24 小时取图 ${data.stats.total} 次，成功 ${data.stats.loaded}，失败 ${data.stats.failed}${via ? `；来源：${via}` : ""}`
            : "近 24 小时还没有收到图片"}
        </div>
        {data.reasons.length > 0 && (
          <ul className="agent-vision-list">
            {data.reasons.map((item) => (
              <li key={item.code}>
                {item.message} × {item.count}：{item.hint}
              </li>
            ))}
          </ul>
        )}
        {data.advice.length > 0 && (
          <ul className="agent-vision-list agent-vision-advice">
            {data.advice.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        )}
      </div>
      <Button size="small" onClick={() => query.reload()}>
        刷新
      </Button>
    </div>
  );
}
