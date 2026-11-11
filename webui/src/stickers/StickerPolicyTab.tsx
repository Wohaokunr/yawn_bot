// 策略与频率：全局设置、发送节奏、菜单策略、收藏/偷图、AI 标注、回收站、按群单独设置。
import { DeleteOutlined, PlusOutlined, RobotOutlined, SaveOutlined, UndoOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Card, Input, InputNumber, Popconfirm, Radio, Segmented, Select, Switch, Tag } from "antd";
import { useEffect, useMemo, useState } from "react";
import { SaveStatus, useUnsavedChanges } from "../shared";
import { parseGroupIds } from "../web-search/web-search-meta";
import { errorText } from "./StickerLibraryTab";
import { stickersApi } from "./stickers-api";
import {
  COLLECT_LABELS,
  describeOverride,
  describeQuiet,
  describeSteal,
  describeStickerAccess,
  emptyOverride,
  FREQUENCY_HINTS,
  MENU_STRATEGY_HINTS,
  OVERRIDE_FIELD_LABELS,
  overrideIsEmpty,
  SCOPE_LABELS,
  STEAL_LABELS,
  STICKER_SETTING_RANGES,
  stickerSettingsEqual,
  validateStickerSettings,
} from "./stickers-meta";
import type { StickerNumericKey } from "./stickers-meta";
import type { GroupOverride, MenuStrategy, StickerOverview, StickerSettings } from "./types";

function NumberField({ field, draft, server, errors, onChange }: {
  field: StickerNumericKey;
  draft: StickerSettings;
  server: StickerSettings;
  errors: Record<string, string>;
  onChange: (key: StickerNumericKey, value: number) => void;
}): React.JSX.Element {
  const range = STICKER_SETTING_RANGES[field];
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
        status={errors[field] ? "error" : undefined}
        onChange={(value) => onChange(field, typeof value === "number" ? Math.round(value) : range.min)}
        addonAfter={range.unit}
        style={{ width: "100%" }}
      />
      {errors[field] && <span className="sbx-field-error">{errors[field]}</span>}
    </label>
  );
}

function GroupListField({ label, value, placeholder, error, onChange }: { label: string; value: string[]; placeholder: string; error?: string; onChange: (values: string[]) => void }): React.JSX.Element {
  const [text, setText] = useState(value.join("\n"));
  useEffect(() => {
    if (parseGroupIds(text).ids.join(",") !== value.join(",")) setText(value.join("\n"));
  }, [value]);
  const invalid = parseGroupIds(text).invalid;
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
          onChange(parseGroupIds(event.target.value).ids);
        }}
      />
      {(error || invalid.length > 0) && <span className="sbx-field-error">{invalid.length ? `忽略了不合法的项：${invalid.slice(0, 3).join("、")}` : error}</span>}
    </label>
  );
}

const OVERRIDE_NUMBERS = ["groupCooldownSeconds", "groupDailyLimit", "groupHourlyLimit", "maxPerMessage", "menuSize"] as const;

function triState(value: boolean | null): "inherit" | "on" | "off" {
  return value === null ? "inherit" : value ? "on" : "off";
}

function fromTriState(value: string): boolean | null {
  return value === "inherit" ? null : value === "on";
}

function OverridesEditor({ overview, value, error, onChange }: {
  overview: StickerOverview;
  value: Record<string, GroupOverride>;
  error?: string;
  onChange: (value: Record<string, GroupOverride>) => void;
}): React.JSX.Element {
  const [adding, setAdding] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const known = useMemo(() => {
    const ids = new Set<string>([...Object.keys(overview.groupNames), ...overview.usage.groups.map((row) => row.groupId)]);
    return [...ids].filter((id) => /^\d{4,12}$/.test(id) && !(id in value)).map((id) => ({ value: id, label: overview.groupNames[id] ? `${overview.groupNames[id]}（${id}）` : id }));
  }, [overview, value]);
  const set = (group: string, patch: Partial<GroupOverride>) => onChange({ ...value, [group]: { ...(value[group] ?? emptyOverride()), ...patch } });
  const remove = (group: string) => {
    const next = { ...value };
    delete next[group];
    onChange(next);
    if (open === group) setOpen(null);
  };
  const add = () => {
    const group = adding.trim();
    if (!/^\d{4,12}$/.test(group) || group in value) return;
    onChange({ ...value, [group]: emptyOverride() });
    setOpen(group);
    setAdding("");
  };
  return (
    <div className="stk-overrides">
      <p className="sbx-muted">上面是所有群的默认设置。某个群想更爱发、更克制、或者单独开关，在这里给它单独写；没写的项跟着默认走。什么都没改的群保存时会自动去掉。</p>
      <div className="web-toolbar">
        <Select
          showSearch
          allowClear
          style={{ minWidth: 240 }}
          placeholder="选择或输入群号"
          value={adding || undefined}
          options={known}
          onSearch={(text) => setAdding(text.replace(/\D/g, ""))}
          onChange={(group: string | undefined) => setAdding(group ?? "")}
          notFoundContent={null}
          aria-label="要单独设置的群"
        />
        <Button icon={<PlusOutlined />} disabled={!/^\d{4,12}$/.test(adding.trim()) || adding.trim() in value} onClick={add}>单独设置这个群</Button>
      </div>
      {error && <span className="sbx-field-error">{error}</span>}
      <ul className="stk-override-list">
        {Object.entries(value).map(([group, override]) => (
          <li key={group} className={open === group ? "is-open" : ""}>
            <div className="stk-override-head">
              <button type="button" className="stk-override-name" onClick={() => setOpen(open === group ? null : group)} aria-expanded={open === group}>
                <strong>{overview.groupNames[group] ?? `群 ${group}`}</strong>
                <span className="sbx-muted">{overview.groupNames[group] ? `${group} · ` : ""}{describeOverride(override)}{overrideIsEmpty(override) ? "（保存时会去掉）" : ""}</span>
              </button>
              <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={() => remove(group)} aria-label={`去掉群 ${group} 的单独设置`} />
            </div>
            {open === group && (
              <div className="stk-override-body">
                <div className="sbx-field-grid">
                  <label className="sbx-field">
                    <span className="sbx-field-label">这个群发不发</span>
                    <Segmented value={triState(override.enabled)} onChange={(next) => set(group, { enabled: fromTriState(String(next)) })} options={[{ value: "inherit", label: "跟默认" }, { value: "on", label: "一定开" }, { value: "off", label: "一定关" }]} />
                    <span className="sbx-muted">“一定开”不受白名单和禁用名单影响。</span>
                  </label>
                  <label className="sbx-field">
                    <span className="sbx-field-label">性格</span>
                    <Select allowClear placeholder={`跟默认（${overview.frequencyLabels[overview.settings.frequency]}）`} value={override.frequency ?? undefined} onChange={(next: StickerSettings["frequency"] | undefined) => set(group, { frequency: next ?? null })} options={(["rare", "normal", "often"] as const).map((key) => ({ value: key, label: overview.frequencyLabels[key] }))} aria-label={`群 ${group} 的性格`} />
                  </label>
                  {OVERRIDE_NUMBERS.map((key) => (
                    <label className="sbx-field" key={key}>
                      <span className="sbx-field-label">{STICKER_SETTING_RANGES[key].label}</span>
                      <InputNumber
                        min={STICKER_SETTING_RANGES[key].min}
                        max={STICKER_SETTING_RANGES[key].max}
                        value={override[key]}
                        placeholder={`跟默认（${overview.settings[key]}）`}
                        onChange={(next) => set(group, { [key]: typeof next === "number" ? Math.round(next) : null })}
                        addonAfter={STICKER_SETTING_RANGES[key].unit}
                        style={{ width: "100%" }}
                        aria-label={`群 ${group} 的${OVERRIDE_FIELD_LABELS[key]}`}
                      />
                    </label>
                  ))}
                  <label className="sbx-field">
                    <span className="sbx-field-label">主动发言里发不发</span>
                    <Segmented value={triState(override.allowProactive)} onChange={(next) => set(group, { allowProactive: fromTriState(String(next)) })} options={[{ value: "inherit", label: "跟默认" }, { value: "on", label: "发" }, { value: "off", label: "不发" }]} />
                  </label>
                  <label className="sbx-field">
                    <span className="sbx-field-label">偷图</span>
                    <Select allowClear placeholder={`跟默认（${STEAL_LABELS[overview.settings.stealMode]}）`} value={override.stealMode ?? undefined} onChange={(next: StickerSettings["stealMode"] | undefined) => set(group, { stealMode: next ?? null })} options={(["off", "review", "auto"] as const).map((key) => ({ value: key, label: STEAL_LABELS[key] }))} aria-label={`群 ${group} 的偷图方式`} />
                  </label>
                  <label className="sbx-field">
                    <span className="sbx-field-label">群友收藏</span>
                    <Select allowClear placeholder={`跟默认（${COLLECT_LABELS[overview.settings.collectMode]}）`} value={override.collectMode ?? undefined} onChange={(next: StickerSettings["collectMode"] | undefined) => set(group, { collectMode: next ?? null })} options={(["off", "review", "auto"] as const).map((key) => ({ value: key, label: COLLECT_LABELS[key] }))} aria-label={`群 ${group} 的收藏方式`} />
                  </label>
                  <label className="sbx-field">
                    <span className="sbx-field-label">备注</span>
                    <Input value={override.note} maxLength={60} onChange={(event) => set(group, { note: event.target.value })} placeholder="为什么单独设置" aria-label={`群 ${group} 的备注`} />
                  </label>
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PolicyTab({ overview, onSaved }: { overview: StickerOverview; onSaved: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const server = overview.settings;
  const [draft, setDraft] = useState<StickerSettings>(server);
  const [base, setBase] = useState<StickerSettings>(server);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (stickerSettingsEqual(base, server)) return;
    setDraft((current) => (stickerSettingsEqual(current, base) ? server : current));
    setBase(server);
  }, [server]);
  const dirty = !stickerSettingsEqual(draft, server);
  const [protectedText, setProtectedText] = useState(server.stealProtectedUsers.join("\n"));
  useEffect(() => {
    if (draft.stealProtectedUsers.join(",") !== protectedText.split(/[\s,，、]+/).filter((item) => /^\d{5,12}$/.test(item)).join(",")) setProtectedText(draft.stealProtectedUsers.join("\n"));
  }, [draft.stealProtectedUsers]);
  useUnsavedChanges(dirty);
  const errors = useMemo(() => validateStickerSettings(draft), [draft]);
  const update = <K extends keyof StickerSettings>(key: K, value: StickerSettings[K]) => setDraft({ ...draft, [key]: value });
  const num = (key: StickerNumericKey) => <NumberField key={key} field={key} draft={draft} server={server} errors={errors} onChange={(field, value) => update(field, value)} />;
  const save = async () => {
    setSaving(true);
    try {
      setDraft(await stickersApi.saveSettings(draft));
      onSaved();
      message.success("表情包策略已保存，立即生效");
    } catch (error) {
      message.error(errorText(error, "保存失败"));
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="sbx-policy">
      <div className="sbx-policy-summary">
        <div>
          <strong>{describeStickerAccess(draft)}</strong>
          <span className="sbx-muted">设置保存在 data/agent_stickers/settings.json，不需要重启 Bot。</span>
        </div>
        <span className="sbx-toolbar-spacer" />
        <SaveStatus dirty={dirty} saving={saving} />
        <Button icon={<UndoOutlined />} disabled={!dirty} onClick={() => setDraft(server)}>撤销修改</Button>
        <Popconfirm title="恢复为默认策略？" description="只改草稿，点保存后才生效。" onConfirm={() => setDraft(overview.defaults)}>
          <Button>恢复默认</Button>
        </Popconfirm>
        <Button type="primary" icon={<SaveOutlined />} disabled={!dirty || Object.keys(errors).length > 0} loading={saving} onClick={() => void save()}>保存</Button>
      </div>
      <div className="sbx-grid">
        <Card size="small" title="开放范围" className="sbx-card-wide">
          <div className="sbx-switch-row">
            <Switch checked={draft.enabled} onChange={(value) => update("enabled", value)} aria-label="允许 Agent 发表情包" />
            <div>
              <strong>允许 Agent 发表情包</strong>
              <span className="sbx-muted">关闭后模型看不到表情包菜单，写了 [表情:…] 也会被去掉。</span>
            </div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.allowProactive} onChange={(value) => update("allowProactive", value)} aria-label="主动发言也能发" />
            <div>
              <strong>主动插话、暖场、续聊里也能发</strong>
              <span className="sbx-muted">人设“接梗”倾向低的角色仍然不会主动发。</span>
            </div>
          </div>
          <Radio.Group
            className="sbx-gap"
            value={draft.groupMode}
            onChange={(event) => update("groupMode", event.target.value as StickerSettings["groupMode"])}
            optionType="button"
            options={[
              { value: "all", label: "所有群" },
              { value: "allowlist", label: "只开放白名单群" },
            ]}
          />
          <div className="sbx-field-grid">
            {draft.groupMode === "allowlist" && (
              <GroupListField label="白名单群号" value={draft.allowedGroups} error={errors.allowedGroups} placeholder="每行一个群号" onChange={(values) => update("allowedGroups", values)} />
            )}
            <GroupListField label="禁用的群（优先于白名单）" value={draft.blockedGroups} placeholder="这些群永远不发表情包" onChange={(values) => update("blockedGroups", values)} />
          </div>
        </Card>
        <Card size="small" title="发送频率">
          <Radio.Group
            value={draft.frequency}
            onChange={(event) => update("frequency", event.target.value as StickerSettings["frequency"])}
            optionType="button"
            options={(["rare", "normal", "often"] as const).map((key) => ({ value: key, label: overview.frequencyLabels[key] }))}
          />
          <p className="sbx-muted">{FREQUENCY_HINTS[draft.frequency]}。这只改变提示词里的语气，下面的数字才是硬上限。</p>
          {num("groupCooldownSeconds")}
          {num("groupDailyLimit")}
          {num("maxPerMessage")}
        </Card>
        <Card size="small" title="发送节奏">
          <p className="sbx-muted">这几项都只看计数和时间，不看聊天内容。</p>
          {num("groupHourlyLimit")}
          {num("minMessagesBetween")}
          {num("stickerCooldownMinutes")}
          <div className="sbx-switch-row">
            <Switch checked={draft.quietEnabled} onChange={(value) => update("quietEnabled", value)} aria-label="安静时段" />
            <div>
              <strong>安静时段不发表情包</strong>
              <span className="sbx-muted">{describeQuiet(draft)}；文字照常回。</span>
            </div>
          </div>
          {draft.quietEnabled && (
            <div className="sbx-field-grid">
              <label className="sbx-field">
                <span className="sbx-field-label">从</span>
                <Input value={draft.quietStart} maxLength={5} placeholder="23:00" status={errors.quietStart ? "error" : undefined} onChange={(event) => update("quietStart", event.target.value.trim())} aria-label="安静时段开始" />
                {errors.quietStart && <span className="sbx-field-error">{errors.quietStart}</span>}
              </label>
              <label className="sbx-field">
                <span className="sbx-field-label">到</span>
                <Input value={draft.quietEnd} maxLength={5} placeholder="07:00" status={errors.quietEnd ? "error" : undefined} onChange={(event) => update("quietEnd", event.target.value.trim())} aria-label="安静时段结束" />
                {errors.quietEnd && <span className="sbx-field-error">{errors.quietEnd}</span>}
              </label>
            </div>
          )}
        </Card>
        <Card size="small" title="菜单怎么挑">
          <Select
            style={{ width: "100%" }}
            value={draft.menuStrategy}
            aria-label="菜单策略"
            onChange={(value: MenuStrategy) => update("menuStrategy", value)}
            options={(Object.keys(MENU_STRATEGY_HINTS) as MenuStrategy[]).map((key) => ({ value: key, label: overview.menuStrategyLabels[key] ?? key }))}
          />
          <p className="sbx-muted">{MENU_STRATEGY_HINTS[draft.menuStrategy]}。置顶的永远在最前，同一个情绪标签不会占满菜单；挑的时候不看聊天内容。</p>
          {num("menuSize")}
          {num("newBoostDays")}
          {num("menuHintChars")}
          <div className="sbx-switch-row">
            <Switch checked={draft.menuShowTags} onChange={(value) => update("menuShowTags", value)} aria-label="菜单附情绪标签" />
            <div><strong>菜单后面附一行情绪标签</strong><span className="sbx-muted">模型可以写 [表情:无语] 让系统在同标签里挑一张；关掉省一点提示词。</span></div>
          </div>
        </Card>
        <Card size="small" title="群友收藏（save_sticker）">
          <Radio.Group
            value={draft.collectMode}
            onChange={(event) => update("collectMode", event.target.value as StickerSettings["collectMode"])}
            optionType="button"
            options={(["off", "review", "auto"] as const).map((key) => ({ value: key, label: COLLECT_LABELS[key] }))}
          />
          <p className="sbx-muted">群友回复一张图说“收藏这个表情”，Bot 会把它存进库；群主/管理员收藏的直接可用。</p>
          <div className="sbx-switch-row">
            <Switch checked={draft.collectAdminOnly} onChange={(value) => update("collectAdminOnly", value)} aria-label="只有管理员能收藏" />
            <div><strong>只有群主/管理员能收藏</strong></div>
          </div>
          {num("memberDailyCollect")}
          <label className="sbx-field">
            <span className="sbx-field-label">收藏来的图在哪些群用</span>
            <Radio.Group value={draft.collectScope} onChange={(event) => update("collectScope", event.target.value as StickerSettings["collectScope"])} optionType="button" options={(["all", "origin"] as const).map((key) => ({ value: key, label: SCOPE_LABELS[key] }))} />
          </label>
          <div className="sbx-switch-row">
            <Switch checked={draft.memberManageOwn} onChange={(value) => update("memberManageOwn", value)} aria-label="成员可以管自己的表情包" />
            <div><strong>成员可以改名、删掉自己收藏的</strong><span className="sbx-muted">在群里或私聊里跟 Bot 说就行；从他那偷的图，他也能让 Bot 删掉。</span></div>
          </div>
        </Card>
        <Card size="small" title="私聊">
          <div className="sbx-switch-row">
            <Switch
              checked={draft.privateEnabled}
              disabled={!draft.enabled}
              onChange={(value) => update("privateEnabled", value)}
              aria-label="私聊里也发表情包"
            />
            <div>
              <strong>私聊 Agent 也能发表情包</strong>
              <span className="sbx-muted">只用对所有群开放的通用表情；本人设置了“不要表情”时不发。次数按人计，不占群的额度。</span>
            </div>
          </div>
          {num("privateDailyLimit")}
          {num("privateCooldownSeconds")}
        </Card>
        <Card size="small" title={<span><RobotOutlined /> AI 标注</span>}>
          <div className="sbx-switch-row">
            <Switch checked={draft.aiLabelEnabled} onChange={(value) => update("aiLabelEnabled", value)} aria-label="启用 AI 标注" />
            <div>
              <strong>让识图模型给表情包起名、打标签</strong>
              <span className="sbx-muted">{overview.vision.ready ? `识图模型：${overview.vision.model}` : "还没有能看图的模型（AI_VISION_MODEL），开着也用不了"}</span>
            </div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.aiLabelOnUpload} disabled={!draft.aiLabelEnabled} onChange={(value) => update("aiLabelOnUpload", value)} aria-label="上传时自动补全" />
            <div><strong>上传时补全空着的名字和标签</strong><span className="sbx-muted">关掉后上传要手填名字。</span></div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.aiLabelOnCollect} disabled={!draft.aiLabelEnabled} onChange={(value) => update("aiLabelOnCollect", value)} aria-label="收藏和偷图时自动起名" />
            <div><strong>群友收藏、Bot 偷图时没写名字就让 AI 起</strong><span className="sbx-muted">关掉后由对话模型自己起名。</span></div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.aiLabelReviewUnsafe} disabled={!draft.aiLabelEnabled} onChange={(value) => update("aiLabelReviewUnsafe", value)} aria-label="不合适的进待审核" />
            <div><strong>AI 觉得不合适的图进待审核</strong><span className="sbx-muted">色情、血腥、带隐私（证件、聊天记录）的图不会直接能用。</span></div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.aiLabelAliases} disabled={!draft.aiLabelEnabled} onChange={(value) => update("aiLabelAliases", value)} aria-label="AI 顺便起别名" />
            <div><strong>让 AI 顺便起一两个别名</strong><span className="sbx-muted">模型换个说法也能发出这张。</span></div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.aiLabelRetry} disabled={!draft.aiLabelEnabled} onChange={(value) => update("aiLabelRetry", value)} aria-label="识别失败再试一次" />
            <div><strong>识图模型答得不对时再试一次</strong><span className="sbx-muted">只在它没按格式回答或空回复时重试。</span></div>
          </div>
          {num("aiLabelConcurrency")}
          <label className="sbx-field">
            <span className="sbx-field-label">AI 把握低于这个值的收藏/偷图先进待审核（0 = 不看把握）</span>
            <InputNumber min={0} max={1} step={0.05} value={draft.reviewBelowConfidence} status={errors.reviewBelowConfidence ? "error" : undefined} onChange={(value) => update("reviewBelowConfidence", typeof value === "number" ? Math.round(value * 100) / 100 : 0)} style={{ width: "100%" }} aria-label="待审核的把握门槛" />
          </label>
          <label className="sbx-field">
            <span className="sbx-field-label">命名要求（追加在标注提示词后面）</span>
            <Input.TextArea
              value={draft.aiLabelStyle}
              maxLength={300}
              showCount
              autoSize={{ minRows: 2, maxRows: 4 }}
              disabled={!draft.aiLabelEnabled}
              status={errors.aiLabelStyle ? "error" : undefined}
              placeholder="如：名字用网络梗，四个字以内；标签用开心/难过/无语/震惊这类情绪词"
              onChange={(event) => update("aiLabelStyle", event.target.value)}
            />
          </label>
        </Card>
        <Card size="small" title="Bot 偷图（steal_sticker）">
          <Radio.Group
            value={draft.stealMode}
            onChange={(event) => update("stealMode", event.target.value as StickerSettings["stealMode"])}
            optionType="button"
            options={(["off", "review", "auto"] as const).map((key) => ({ value: key, label: STEAL_LABELS[key] }))}
          />
          <p className="sbx-muted">群友发了好玩的表情包，Bot 觉得以后用得上会自己存下来（不用谁开口）。{describeSteal(draft)}。</p>
          <div className="sbx-switch-row">
            <Switch checked={draft.stealPhotos} onChange={(value) => update("stealPhotos", value)} aria-label="也偷普通照片" />
            <div><strong>普通照片、截图也能偷</strong><span className="sbx-muted">默认只偷 QQ 标成表情包的图。私聊里的照片一律进待审核。</span></div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.stealAnimated} onChange={(value) => update("stealAnimated", value)} aria-label="动图也偷" />
            <div><strong>动图也偷</strong></div>
          </div>
          <label className="sbx-field">
            <span className="sbx-field-label">偷来的图在哪些群用</span>
            <Radio.Group value={draft.stealScope} onChange={(event) => update("stealScope", event.target.value as StickerSettings["stealScope"])} optionType="button" options={(["all", "origin"] as const).map((key) => ({ value: key, label: SCOPE_LABELS[key] }))} />
            <span className="sbx-muted">选“只在来源群用”时，A 群的梗图不会被发到 B 群；之后可以在编辑框里单独放开。</span>
          </label>
          {num("groupDailySteals")}
          {num("stealSenderDaily")}
          {num("stealMinSide")}
          {num("stealCooldownSeconds")}
          <GroupListField label="这些群里不偷图（发还是照常发）" value={draft.stealBlockedGroups} placeholder="每行一个群号" onChange={(values) => update("stealBlockedGroups", values)} />
          <div className="sbx-switch-row">
            <Switch checked={draft.privateStealEnabled} onChange={(value) => update("privateStealEnabled", value)} aria-label="私聊里也能存" />
            <div><strong>私聊里也能存他发来的表情包</strong></div>
          </div>
          {num("privateDailySteals")}
          <label className="sbx-field">
            <span className="sbx-field-label">别偷这些人的图（QQ 号，每行一个）</span>
            <Input.TextArea
              value={protectedText}
              autoSize={{ minRows: 1, maxRows: 4 }}
              placeholder="说过“别偷我的图”的群友"
              onChange={(event) => {
                setProtectedText(event.target.value);
                update("stealProtectedUsers", event.target.value.split(/[\s,，、]+/).map((item) => item.trim()).filter((item) => /^\d{5,12}$/.test(item)));
              }}
            />
          </label>
          <div className="sbx-switch-row">
            <Switch checked={draft.memberSelfProtect} onChange={(value) => update("memberSelfProtect", value)} aria-label="成员可以自己说别偷我的图" />
            <div><strong>成员自己说“别偷我的图”就加进名单</strong><span className="sbx-muted">关掉后只有管理员能改这个名单。</span></div>
          </div>
        </Card>
        <Card size="small" title="回收站">
          {num("trashDays")}
          <p className="sbx-muted">删掉的表情包先进回收站，过了天数自动清掉；回收站里的图不会发，也不会被收藏/偷图悄悄加回来。</p>
        </Card>
        <Card size="small" title={`按群单独设置（${Object.keys(draft.groupOverrides).length}）`} className="sbx-card-wide">
          <OverridesEditor overview={overview} value={draft.groupOverrides} error={errors.groupOverrides} onChange={(value) => update("groupOverrides", value)} />
        </Card>
      </div>
    </div>
  );
}
