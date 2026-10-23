import { CopyOutlined, ReloadOutlined, RollbackOutlined, ThunderboltOutlined, UndoOutlined } from "@ant-design/icons";
import {
  App as AntApp,
  AutoComplete,
  Button,
  Input,
  InputNumber,
  Progress,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useState } from "react";
import { enumLabel } from "./environment-config";
import {
  compactJson,
  generateToken,
  humanizeValue,
  labelOf,
  metaOf,
  parseCsvList,
  parseJsonStringList,
  rangeHint,
  serializeCsvList,
  serializeJsonStringList,
  tokenStrength,
  tokenWarnings,
} from "./environment-fields";
import type { EnvironmentEntry, EnvironmentValueSource } from "./types";

const { Text } = Typography;

// 来源只在“需要注意”时显示：默认值和根 .env 是常态，不再给每行都挂标签
export const SOURCE_BADGE: Partial<Record<EnvironmentValueSource, { label: string; color: string; tip: string }>> = {
  process: { label: "外部覆盖", color: "red", tip: "进程环境变量优先，面板里的修改不会生效" },
  environment: { label: "环境文件覆盖", color: "orange", tip: ".env.<环境名> 中的值优先，面板里的修改不会生效" },
  pinned: { label: "部署固定", color: "default", tip: "由镜像或 compose 固定的部署参数，面板修改不会生效" },
};

export interface FieldRowProps {
  entry: EnvironmentEntry;
  draft: string | null | undefined;
  dirty: boolean;
  error?: string;
  highlighted?: boolean;
  onChange: (value: string | null) => void;
  onUndo: () => void;
}

function shownValue(entry: EnvironmentEntry, draft: string | null | undefined, dirty: boolean): string {
  if (dirty) return draft ?? "";
  return entry.value ?? "";
}

function BooleanEditor({ entry, value, onChange }: { entry: EnvironmentEntry; value: string; onChange: (value: string) => void }): React.JSX.Element {
  const effective = (value || entry.defaultValue || "false").toLowerCase() === "true";
  return (
    <Space size={8}>
      <Switch
        checked={effective}
        onChange={(checked) => onChange(checked ? "true" : "false")}
        aria-label={labelOf(entry)}
      />
      <Text type="secondary" className="envx-small">
        {effective ? "开启" : "关闭"}
      </Text>
    </Space>
  );
}

function ListEditor({
  entry,
  value,
  json,
  onChange,
}: {
  entry: EnvironmentEntry;
  value: string;
  json: boolean;
  onChange: (value: string) => void;
}): React.JSX.Element {
  const source = value || entry.defaultValue || "";
  const items = json ? parseJsonStringList(source) : parseCsvList(source);
  if (items === null) {
    // 现有值不是字符串数组：退回文本框让人修正
    return <Input value={value} onChange={(event) => onChange(event.target.value)} className="envx-mono" />;
  }
  return (
    <Select
      mode="tags"
      value={items}
      tokenSeparators={[",", "，", " "]}
      open={false}
      suffixIcon={null}
      placeholder={json ? "输入后回车添加" : "输入后回车添加，逗号分隔"}
      onChange={(next: string[]) => onChange(json ? serializeJsonStringList(next) : serializeCsvList(next))}
      style={{ width: "100%" }}
      aria-label={labelOf(entry)}
    />
  );
}

function JsonEditor({ entry, value, onChange }: { entry: EnvironmentEntry; value: string; onChange: (value: string) => void }): React.JSX.Element {
  return (
    <Space.Compact style={{ width: "100%" }}>
      <Input.TextArea
        value={value}
        autoSize={{ minRows: 1, maxRows: 6 }}
        placeholder={entry.defaultValue ?? "JSON"}
        className="envx-mono"
        onChange={(event) => onChange(event.target.value)}
        onBlur={() => {
          const compact = compactJson(value);
          if (compact !== null && compact !== value) onChange(compact);
        }}
        aria-label={labelOf(entry)}
      />
    </Space.Compact>
  );
}

export function TokenEditor({
  value,
  configured,
  onChange,
}: {
  value: string;
  configured: boolean;
  onChange: (value: string) => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [revealed, setRevealed] = useState(false);
  const strength = tokenStrength(value);
  const warnings = tokenWarnings(value);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      message.success("已复制新 Token，请妥善保存");
    } catch {
      setRevealed(true);
      message.info("浏览器不允许自动复制，已显示明文，请手动复制");
    }
  };
  return (
    <Space orientation="vertical" size={6} style={{ width: "100%" }}>
      <Space.Compact style={{ width: "100%" }}>
        {revealed ? (
          <Input
            value={value}
            onChange={(event) => onChange(event.target.value)}
            className="envx-mono"
            aria-label="新的运维 Token"
          />
        ) : (
          <Input.Password
            value={value}
            visibilityToggle={false}
            autoComplete="new-password"
            placeholder={configured ? "已配置，输入新值以替换" : "未配置"}
            onChange={(event) => onChange(event.target.value)}
            aria-label="新的运维 Token"
          />
        )}
        <Tooltip title={revealed ? "隐藏" : "显示明文"}>
          <Button onClick={() => setRevealed((current) => !current)}>{revealed ? "隐藏" : "显示"}</Button>
        </Tooltip>
        <Tooltip title="在浏览器本地生成 32 位随机 Token">
          <Button
            icon={<ThunderboltOutlined />}
            onClick={() => {
              onChange(generateToken());
              setRevealed(true);
            }}
          >
            生成
          </Button>
        </Tooltip>
        <Tooltip title="复制">
          <Button icon={<CopyOutlined />} disabled={!value} onClick={() => void copy()} aria-label="复制新 Token" />
        </Tooltip>
      </Space.Compact>
      {value && (
        <div className="envx-token-meter">
          <Progress percent={strength.score} status={strength.status} showInfo={false} size="small" />
          <Text type={warnings.length ? "warning" : "success"} className="envx-small">
            强度：{strength.label}
            {warnings.length ? ` · ${warnings.join("；")}` : ""}
          </Text>
        </div>
      )}
    </Space>
  );
}

export function FieldEditor({
  entry,
  value,
  dirty,
  onChange,
}: {
  entry: EnvironmentEntry;
  value: string;
  dirty: boolean;
  onChange: (value: string) => void;
}): React.JSX.Element {
  const meta = metaOf(entry);
  if (entry.key === "WEBUI_ADMIN_TOKEN") {
    return <TokenEditor value={dirty ? value : ""} configured={entry.effectiveConfigured} onChange={onChange} />;
  }
  if (entry.secret) {
    return (
      <Input.Password
        value={dirty ? value : ""}
        visibilityToggle={false}
        autoComplete="new-password"
        placeholder={entry.effectiveConfigured ? "已配置，输入新值以替换" : "未配置"}
        onChange={(event) => onChange(event.target.value)}
        aria-label={labelOf(entry)}
      />
    );
  }
  if (entry.kind === "enum") {
    return (
      <Select
        value={value || undefined}
        placeholder={entry.defaultValue ? `默认：${enumLabel(entry, entry.defaultValue)}` : "请选择"}
        onChange={onChange}
        style={{ width: "100%" }}
        options={entry.options.map((option) => ({ value: option, label: enumLabel(entry, option) }))}
        aria-label={labelOf(entry)}
      />
    );
  }
  if (entry.kind === "boolean") return <BooleanEditor entry={entry} value={value} onChange={onChange} />;
  if (meta.format === "csv-list") return <ListEditor entry={entry} value={value} json={false} onChange={onChange} />;
  if (meta.format === "json-string-list") return <ListEditor entry={entry} value={value} json onChange={onChange} />;
  if (entry.kind === "json") return <JsonEditor entry={entry} value={value} onChange={onChange} />;
  if (entry.kind === "integer" || entry.kind === "number") {
    return (
      <InputNumber
        value={value === "" ? null : value}
        stringMode
        min={meta.min !== null ? String(meta.min) : undefined}
        max={meta.max !== null ? String(meta.max) : undefined}
        step={entry.kind === "integer" ? 1 : 0.1}
        precision={entry.kind === "integer" ? 0 : undefined}
        placeholder={entry.defaultValue ? `默认 ${entry.defaultValue}` : undefined}
        onChange={(next) => onChange(next === null ? "" : String(next))}
        style={{ width: "100%" }}
        aria-label={labelOf(entry)}
      />
    );
  }
  if (meta.suggestions.length > 0) {
    return (
      <AutoComplete
        value={value}
        options={meta.suggestions.map((item) => ({ value: item }))}
        placeholder={entry.defaultValue ? `默认：${entry.defaultValue}` : undefined}
        onChange={(next: string) => onChange(next)}
        style={{ width: "100%" }}
        aria-label={labelOf(entry)}
      />
    );
  }
  return (
    <Input
      value={value}
      placeholder={entry.defaultValue ? `默认：${entry.defaultValue}` : entry.defaultValue === "" ? "默认留空" : "未配置"}
      onChange={(event) => onChange(event.target.value)}
      className={meta.format === "url" ? "envx-mono" : undefined}
      aria-label={labelOf(entry)}
    />
  );
}

/** 中文名取自说明首句，说明里再重复一遍没有意义：只显示剩下的部分 */
export function describeRest(description: string, label: string): string | null {
  if (!description || description === label || description === "根 .env 中的自定义配置项") return null;
  if (!description.startsWith(label)) return description;
  const rest = description.slice(label.length).replace(/^[，,；;。：:\s]+/, "").trim();
  if (!rest) return null;
  // 开头的括号说明（单位、范围）去掉括号，和后文用 · 连接
  const bracket = rest.match(/^[（(]([^）)]*)[）)][，,；;。\s]*(.*)$/);
  if (!bracket) return rest;
  return bracket[2] ? `${bracket[1]} · ${bracket[2]}` : bracket[1];
}

function defaultText(entry: EnvironmentEntry): string | null {
  if (entry.secret) return null;
  if (entry.defaultValue === null) return null;
  if (entry.defaultValue === "") return "默认留空";
  if (entry.kind === "boolean") return `默认${entry.defaultValue === "true" ? "开启" : "关闭"}`;
  if (entry.kind === "enum") return `默认 ${enumLabel(entry, entry.defaultValue)}`;
  const human = humanizeValue(entry, entry.defaultValue);
  return `默认 ${entry.defaultValue}${human ? `（${human}）` : ""}`;
}

export function FieldRow({ entry, draft, dirty, error, highlighted, onChange, onUndo }: FieldRowProps): React.JSX.Element {
  const value = shownValue(entry, draft, dirty);
  const removing = dirty && draft === null;
  const label = labelOf(entry);
  const meta = metaOf(entry);
  const badge = SOURCE_BADGE[entry.source];
  const detail = describeRest(entry.description, label);
  const human = humanizeValue(entry, value);
  const hints = [defaultText(entry), rangeHint(entry), human ? `当前 ≈ ${human}` : null].filter(Boolean);
  const problem = error ?? (!dirty ? entry.issue : null);
  const canReset = entry.configured && !removing;
  return (
    <div
      className={`envx-field${dirty ? " is-dirty" : ""}${problem ? " has-error" : ""}${highlighted ? " is-highlighted" : ""}`}
      id={`env-field-${entry.key}`}
      data-key={entry.key}
    >
      <div className="envx-field-head">
        <div className="envx-field-title">
          <Text strong>{label}</Text>
          {entry.secret && <Tag className="envx-tag">敏感</Tag>}
          {entry.configured && !dirty && <Tag className="envx-tag" color="pink">已自定义</Tag>}
          {dirty && <Tag className="envx-tag" color="gold">未保存</Tag>}
          {meta.deprecated && <Tag className="envx-tag">已弃用</Tag>}
          {badge && (
            <Tooltip title={badge.tip}>
              <Tag className="envx-tag" color={badge.color}>{badge.label}</Tag>
            </Tooltip>
          )}
        </div>
        <Text code copyable={{ text: entry.key, tooltips: ["复制配置名", "已复制"] }} className="envx-key">{entry.key}</Text>
        {detail && <div className="envx-desc">{detail}</div>}
      </div>
      <div className="envx-field-control">
        {removing ? (
          <div className="envx-removing">保存后从配置文件移除，恢复为默认值</div>
        ) : (
          <FieldEditor entry={entry} value={value} dirty={dirty} onChange={onChange} />
        )}
        {problem && <div className="envx-error" role="alert">{problem}</div>}
        {hints.length > 0 && <div className="envx-hint">{hints.join(" · ")}</div>}
      </div>
      <div className="envx-field-actions">
        {dirty && (
          <Tooltip title="撤销未保存的修改">
            <Button size="small" type="text" icon={<UndoOutlined />} onClick={onUndo} aria-label={`撤销 ${entry.key}`} />
          </Tooltip>
        )}
        {canReset && (
          <Tooltip title={entry.secret ? "从配置文件移除" : "恢复默认（从配置文件移除）"}>
            <Button
              size="small"
              type="text"
              icon={entry.secret ? <RollbackOutlined /> : <ReloadOutlined />}
              onClick={() => onChange(null)}
              aria-label={`恢复默认 ${entry.key}`}
            />
          </Tooltip>
        )}
      </div>
    </div>
  );
}
