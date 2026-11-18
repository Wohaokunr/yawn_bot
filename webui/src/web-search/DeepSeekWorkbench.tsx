import { Alert, Button, Card, Checkbox, Input, Select, Space, Typography } from "antd";
import { useEffect, useState } from "react";
import { api, getCsrfToken } from "../api";
import { useApiQuery } from "../shared";

const BASE = "/deepseek";
const FILE_LIMIT = 16 * 1024 * 1024;
const AUDIO_LIMIT = 8 * 1024 * 1024;
const MAX_TTS_CHARS = 2000;
const modes = [
  { value: "text_recognition", label: "提取文字" },
  { value: "rich_layout_recognition", label: "图文解析" },
];

type Attachment = { id: string; name: string };
type Reply = {
  text: string;
  title: string;
  refused: boolean;
  truncated: boolean;
  citations: { url: string; title: string }[];
  usage: Record<string, number>;
};

const usageLabels: Record<string, string> = {
  prompt_tokens: "输入 Token",
  completion_tokens: "输出 Token",
  total_tokens: "总 Token",
  tool_search: "搜索",
  tool_fetch: "读取网页",
  tool_image_search: "图片搜索",
};

function json(body: unknown): Pick<RequestInit, "method" | "body"> {
  return { method: "POST", body: JSON.stringify(body) };
}

function upload(file: File): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file };
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "DeepSeek 调用失败";
}

function Dialogue(): React.JSX.Element {
  const [workspace, setWorkspace] = useState("");
  const [title, setTitle] = useState("");
  const [turns, setTurns] = useState<{ text: string; reply: Reply }[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [text, setText] = useState("");
  const [search, setSearch] = useState(false);
  const [readUrl, setReadUrl] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [mode, setMode] = useState(modes[0].value);
  const [busy, setBusy] = useState<"send" | "upload" | "reset" | null>(null);
  const [error, setError] = useState("");

  async function workspaceId(): Promise<string> {
    if (workspace) return workspace;
    const result = await api<{ id: string }>(`${BASE}/sessions`, { method: "POST" });
    setWorkspace(result.data.id);
    return result.data.id;
  }

  async function addFile(file: File): Promise<void> {
    if (file.size > FILE_LIMIT) {
      setError("文件最大 16 MiB");
      return;
    }
    setBusy("upload");
    setError("");
    try {
      const id = await workspaceId();
      const query = new URLSearchParams({ name: file.name, mode });
      const { data } = await api<Attachment>(`${BASE}/sessions/${id}/files?${query}`, upload(file));
      setAttachments((previous) => previous.some((item) => item.id === data.id) ? previous : [...previous, data]);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  }

  async function send(): Promise<void> {
    setBusy("send");
    setError("");
    try {
      const id = await workspaceId();
      const { data } = await api<Reply>(`${BASE}/sessions/${id}/chat`, json({
        text,
        files: attachments.map((item) => item.id),
        search,
        read_url: readUrl,
        thinking,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
        language: navigator.language || "zh-CN",
      }));
      setTitle(data.title);
      setTurns((previous) => [...previous, { text: [text.trim(), ...attachments.map((item) => item.name)].filter(Boolean).join("\n"), reply: data }]);
      setText("");
      setAttachments([]);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  }

  async function reset(): Promise<void> {
    setBusy("reset");
    setError("");
    try {
      if (workspace) await api(`${BASE}/sessions/${workspace}`, { method: "DELETE" });
    } catch (err) {
      if (!(err && typeof err === "object" && "status" in err && err.status === 404)) {
        setError(errorText(err));
        setBusy(null);
        return;
      }
    }
    setWorkspace("");
    setTitle("");
    setTurns([]);
    setAttachments([]);
    setText("");
    setBusy(null);
  }

  return (
    <Card title={title || "新对话"} extra={<Button disabled={!workspace || !!busy} onClick={() => void reset()}>新建会话</Button>}>
      <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
        {turns.map((turn, index) => (
          <Card key={index} size="small" title="DeepSeek">
            <Typography.Paragraph style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}><strong>你</strong> {turn.text}</Typography.Paragraph>
            {turn.reply.refused
              ? <Alert type="warning" message={turn.reply.text} />
              : <Typography.Paragraph style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{turn.reply.text}</Typography.Paragraph>}
            {turn.reply.truncated && <Typography.Text type="secondary">回复达到输出上限</Typography.Text>}
            {turn.reply.citations.length > 0 && (
              <ol aria-label="来源">
                {turn.reply.citations.map((source, citationIndex) => (
                  <li key={`${source.url}-${citationIndex}`}><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a></li>
                ))}
              </ol>
            )}
            <Typography.Text type="secondary">
              {Object.entries(turn.reply.usage).map(([key, value]) => `${usageLabels[key] ?? key} ${value}`).join(" · ")}
            </Typography.Text>
          </Card>
        ))}
        {error && <Alert type="error" title={error} />}
        {busy === "send" && <Typography.Text role="status">DeepSeek 正在处理和审核回复…</Typography.Text>}
        <Input.TextArea aria-label="对话消息" value={text} maxLength={12000} rows={3} disabled={!!busy} onChange={(event) => setText(event.target.value)} />
        <Space wrap>
          <Checkbox checked={thinking} disabled={!!busy} onChange={(event) => setThinking(event.target.checked)}>深度思考</Checkbox>
          <Checkbox checked={search} disabled={!!busy} onChange={(event) => setSearch(event.target.checked)}>联网搜索</Checkbox>
          <Checkbox checked={readUrl} disabled={!!busy} onChange={(event) => setReadUrl(event.target.checked)}>阅读链接</Checkbox>
        </Space>
        <Space wrap>
          <Select aria-label="文档解析方式" options={modes} value={mode} onChange={setMode} disabled={!!busy} />
          <input type="file" aria-label="对话附件" disabled={!!busy || attachments.length >= 3} onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void addFile(file);
          }} />
          <Button aria-label="发送消息" type="primary" loading={busy === "send"} disabled={!!busy || (!text.trim() && !attachments.length)} onClick={() => void send()}>发送</Button>
        </Space>
        {attachments.map((file) => (
          <Space key={file.id}>
            <Typography.Text>{file.name}</Typography.Text>
            <Button size="small" disabled={!!busy} onClick={() => setAttachments((items) => items.filter((item) => item.id !== file.id))}>移除</Button>
          </Space>
        ))}
      </Space>
    </Card>
  );
}

function MediaTools(): React.JSX.Element {
  const [tool, setTool] = useState<"ocr" | "asr" | "tts">("ocr");
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState(modes[0].value);
  const [text, setText] = useState("");
  const [voice, setVoice] = useState("stella");
  const [output, setOutput] = useState("");
  const [audio, setAudio] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => () => { if (audio) URL.revokeObjectURL(audio); }, [audio]);

  async function run(): Promise<void> {
    setBusy(true);
    setError("");
    setOutput("");
    setAudio("");
    try {
      if (tool === "tts") {
        const response = await fetch(`/webui/api/v1${BASE}/tts`, {
          ...json({ text, voice }),
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() },
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.error?.message || `语音合成失败 (${response.status})`);
        }
        setAudio(URL.createObjectURL(await response.blob()));
      } else if (file) {
        const limit = tool === "ocr" ? FILE_LIMIT : AUDIO_LIMIT;
        if (file.size > limit) throw new Error(`文件最大 ${limit / (1024 * 1024)} MiB`);
        if (tool === "ocr") {
          const query = new URLSearchParams({ name: file.name, mode });
          const { data } = await api<{ pages: { index: number; text: string; finish_reason: string }[] }>(
            `${BASE}/ocr?${query}`, upload(file),
          );
          setOutput(data.pages.map((page) => `第 ${page.index + 1} 页${page.finish_reason === "length" ? "（部分结果）" : ""}\n${page.text}`).join("\n\n"));
        } else {
          const { data } = await api<{ text: string }>(`${BASE}/asr`, upload(file));
          setOutput(data.text || "没有识别到语音");
        }
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="文件与语音">
      <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
        <Select aria-label="选择工具" value={tool} disabled={busy} onChange={(value) => {
          setTool(value); setFile(null); setError(""); setAudio(""); setOutput("");
        }} options={[
          { value: "ocr", label: "OCR · 识别文档文字" },
          { value: "asr", label: "ASR · 转写语音" },
          { value: "tts", label: "TTS · 合成语音" },
        ]} />
        {tool === "ocr" && <Select aria-label="OCR 解析方式" options={modes} value={mode} onChange={setMode} disabled={busy} />}
        {tool === "tts" ? (
          <>
            <Input.TextArea aria-label="朗读文本" maxLength={MAX_TTS_CHARS} value={text} rows={3} disabled={busy} onChange={(event) => setText(event.target.value)} />
            <Select aria-label="音色" value={voice} onChange={setVoice} disabled={busy} options={[
              { value: "stella", label: "海星" }, { value: "tide", label: "暗潮" },
            ]} />
          </>
        ) : (
          <input key={tool} type="file" aria-label={tool === "asr" ? "音频文件（WAV 或 Ogg Opus）" : "OCR 文件"} accept={tool === "asr" ? ".wav,.ogg,.opus" : undefined} disabled={busy} onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        )}
        <Button type="primary" loading={busy} disabled={tool === "tts" ? !text.trim() : !file} onClick={() => void run()}>运行</Button>
        {error && <Alert type="error" title={error} />}
        {output && <Input.TextArea aria-label="识别结果" value={output} readOnly autoSize={{ minRows: 3, maxRows: 16 }} />}
        {audio && <audio aria-label="合成语音" controls src={audio} />}
      </Space>
    </Card>
  );
}

export function DeepSeekWorkbench({ media = false }: { media?: boolean }): React.JSX.Element {
  const status = useApiQuery<{ enabled: boolean; configured: boolean }>({
    queryKey: ["deepseek", "status"],
    fetcher: async (signal) => (await api<{ enabled: boolean; configured: boolean }>(`${BASE}/status`, { signal })).data,
  });
  if (status.loading) return <Typography.Text role="status">正在检查 DeepSeek 配置…</Typography.Text>;
  if (status.error) return <Alert type="error" title={errorText(status.error)} />;
  if (!status.data?.enabled || !status.data.configured) {
    return <Alert type="info" title="DeepSeek 工作台尚未启用" description="请在环境配置中启用 DEEPSEEK_WEBUI_ENABLED，并配置 DEEPSEEK_API_KEY 或官方 DeepSeek 模型提供商。" />;
  }
  return media ? <MediaTools /> : <Dialogue />;
}
