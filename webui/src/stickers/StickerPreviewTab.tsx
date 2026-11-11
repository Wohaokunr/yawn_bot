// 菜单预览：某个群、某种场景下模型看到的表情包说明，菜单每一项为什么在那，以及“写这个名字会发哪张”。
import { EyeOutlined, SearchOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Input, Select, Tag } from "antd";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { errorText } from "./StickerLibraryTab";
import { stickerImageUrl, stickersApi } from "./stickers-api";
import { MENU_WHY_LABELS, OVERRIDE_FIELD_LABELS } from "./stickers-meta";
import type { StickerOverview, StickerPromptPreview, StickerResolve } from "./types";

const DEFAULT_SCENES: Record<string, string> = { conversation: "对话", active_interject: "主动插话", warmup: "冷场暖场", followup: "续聊" };

export function PreviewTab({ overview }: { overview: StickerOverview }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const groups = useMemo(() => {
    const ids = new Set<string>([...Object.keys(overview.groupNames), ...overview.usage.groups.map((row) => row.groupId), ...Object.keys(overview.settings.groupOverrides)]);
    return [...ids].map((id) => ({ value: id, label: overview.groupNames[id] ? `${overview.groupNames[id]}（${id}）` : id }));
  }, [overview]);
  const [groupId, setGroupId] = useState(searchParams.get("stk.group") ?? "");
  const [scene, setScene] = useState("conversation");
  const [preview, setPreview] = useState<StickerPromptPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [name, setName] = useState("");
  const [resolved, setResolved] = useState<StickerResolve | null>(null);
  const [resolving, setResolving] = useState(false);
  const byId = new Map(overview.stickers.map((item) => [item.id, item]));
  const validGroup = /^\d{4,12}$/.test(groupId.trim());
  const load = async (nextScene = scene) => {
    if (!validGroup) {
      message.warning("填一个群号");
      return;
    }
    setLoading(true);
    try {
      setPreview(await stickersApi.promptPreview(groupId.trim(), nextScene));
      setSearchParams(updateSearchParams(searchParams, { "stk.group": groupId.trim() }), { replace: true });
    } catch (error) {
      message.error(errorText(error, "预览失败"));
    } finally {
      setLoading(false);
    }
  };
  const resolve = async () => {
    if (!validGroup || !name.trim()) {
      message.warning("先选群，再写一个名字");
      return;
    }
    setResolving(true);
    try {
      setResolved(await stickersApi.resolve(groupId.trim(), name.trim()));
    } catch (error) {
      message.error(errorText(error, "没查成"));
    } finally {
      setResolving(false);
    }
  };
  const scenes = preview?.scenes ?? DEFAULT_SCENES;
  return (
    <div className="web-test">
      <Card size="small" title="这个群现在一轮对话里，模型看到的表情包说明">
        <div className="web-toolbar">
          <Select
            showSearch
            style={{ minWidth: 240 }}
            placeholder="选择或输入群号"
            value={groupId || undefined}
            options={groups}
            onSearch={(value) => setGroupId(value.replace(/\D/g, ""))}
            onChange={(value: string) => setGroupId(value)}
            notFoundContent={null}
          />
          <Select
            style={{ minWidth: 120 }}
            value={scene}
            aria-label="发言场景"
            onChange={(value: string) => {
              setScene(value);
              if (preview) void load(value);
            }}
            options={Object.entries(scenes).map(([value, label]) => ({ value, label }))}
          />
          <Button type="primary" icon={<EyeOutlined />} loading={loading} onClick={() => void load()}>预览</Button>
        </div>
        <p className="sbx-muted">菜单按“本群可用、置顶在前、新图照顾几天、其余按菜单策略”挑，每小时换一批，不看聊天内容；人设的接梗倾向和发言场景也会影响是否出现（在调试页看完整提示词）。</p>
        {preview && (
          <>
            {preview.overridden && preview.overridden.length > 0 && (
              <Alert type="info" showIcon message={`这个群有单独设置：${preview.overridden.map((key) => OVERRIDE_FIELD_LABELS[key] ?? key).join("、")}`} />
            )}
            {!preview.open && <Alert type="info" showIcon message={`这一轮不会给模型看表情包：${preview.reasonText ?? preview.reason}`} description={preview.reason === "cooldown" ? `还要冷却 ${Math.ceil(preview.cooldownSeconds)} 秒` : undefined} />}
            {preview.open && (
              <>
                <p className="sbx-muted">
                  本群可用 {preview.available} 张，今日还能发 {preview.leftToday} 张{typeof preview.leftHour === "number" ? `，这个小时还能发 ${preview.leftHour} 张` : ""}
                  {typeof preview.chars === "number" ? `；这段说明 ${preview.chars} 个字` : ""}。
                </p>
                <pre className="stk-prompt" aria-label="提示词预览">{preview.text}</pre>
                {preview.menu && preview.menu.length > 0 && (
                  <div className="stk-menu-plan" aria-label="菜单里的每一项">
                    {preview.menu.map((row) => (
                      <div key={row.id} className="stk-menu-item">
                        {byId.get(row.id) && !byId.get(row.id)?.fileMissing ? <img src={stickerImageUrl(byId.get(row.id)!)} alt="" loading="lazy" /> : <span className="stk-rank-blank" />}
                        <div>
                          <strong>{row.name}</strong>
                          <Tag color={row.why === "pinned" ? "magenta" : row.why === "new" ? "green" : row.why === "favorite" ? "gold" : "default"}>{MENU_WHY_LABELS[row.why] ?? row.why}</Tag>
                          {row.hint && <span className="sbx-muted">{row.hint}</span>}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </>
        )}
      </Card>
      <Card size="small" title="模型写这个名字，会发哪一张">
        <div className="web-toolbar">
          <Input style={{ maxWidth: 240 }} value={name} maxLength={24} placeholder="如 狗头、doge、无语" onChange={(event) => setName(event.target.value)} onPressEnter={() => void resolve()} aria-label="要试的名字" />
          <Button icon={<SearchOutlined />} loading={resolving} onClick={() => void resolve()}>试一下</Button>
        </div>
        <p className="sbx-muted">和真正发送时用的是同一个对照：先按编号，再按名字、别名、情绪标签，都要完全对上（可以省空格和大小写），不做“差不多”的猜测。有好几张时轮着发、偏向用得少的。</p>
        {resolved && (
          <div aria-label="对照结果">
            <Alert
              type={resolved.match === "none" ? "warning" : "success"}
              showIcon
              message={resolved.match === "none" ? "对不上：这张不会发出去，文字照常发" : `${resolved.matchText}对上了 ${resolved.count} 张`}
              description={!resolved.gateOpen && resolved.reasonText ? `不过这个群现在不发表情包：${resolved.reasonText}` : resolved.match === "none" ? "可以到“统计”里把它加成某一张的别名。" : undefined}
            />
            <div className="stk-chiprow">
              {resolved.candidates.map((item) => <Tag key={item.id} className="stk-chip" onClick={() => setSearchParams(updateSearchParams(searchParams, { "stk.tab": null, "stk.edit": item.id, "stk.f": "all" }), { replace: true })}>{item.name}</Tag>)}
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
