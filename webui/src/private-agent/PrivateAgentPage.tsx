import { FileSearchOutlined, FileTextOutlined, SmileOutlined } from "@ant-design/icons";
import { Tabs } from "antd";
import { useSearchParams } from "react-router-dom";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { CapturesTab } from "./CapturesTab";
import { PersonaTab } from "./PersonaTab";
import { PromptsTab } from "./PromptsTab";
import { cleanUserId, parsePrivateTab, type PrivateFilter, type PrivateTab } from "./private-meta";

/** 私聊 Agent：私聊提示词注册表、每用户人设、最近请求。URL 参数统一用 ``pa.*``。 */
export function PrivateAgentPage(): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parsePrivateTab(searchParams.get("pa.tab"));
  const userId = cleanUserId(searchParams.get("pa.user"));
  const update = (patch: Record<string, string | null>): void => {
    setSearchParams(updateSearchParams(searchParams, patch), { replace: true });
  };
  const setTab = (next: PrivateTab): void => update({ "pa.tab": next === "prompts" ? null : next, "pa.section": null });
  const pickUser = (next: string | null): void => update({ "pa.user": next });
  const openSection = (sectionId: string): void => update({ "pa.tab": null, "pa.section": sectionId });

  return <div className="sbx-page pa-page">
    <div className="sbx-heading">
      <h2 className="sbx-title">私聊 Agent</h2>
      <p>私聊里发给模型的提示词（全局或只给某位用户改）、每位用户自己的人设，以及最近真实发出的请求</p>
    </div>
    <Tabs
      activeKey={tab}
      onChange={(key) => setTab(key as PrivateTab)}
      destroyOnHidden
      items={[
        {
          key: "prompts",
          label: <span><FileTextOutlined /> 提示词</span>,
          children: <PromptsTab params={{
            userId,
            group: searchParams.get("pa.group") ?? "all",
            filter: (searchParams.get("pa.filter") ?? "all") as PrivateFilter,
            query: searchParams.get("pa.q") ?? "",
            pipeline: searchParams.get("pa.pipe") ?? "",
            sectionId: searchParams.get("pa.section") ?? "",
            update,
          }} />,
        },
        { key: "persona", label: <span><SmileOutlined /> 人设</span>, children: <PersonaTab userId={userId} onPickUser={pickUser} /> },
        { key: "captures", label: <span><FileSearchOutlined /> 最近请求</span>, children: <CapturesTab userId={userId} onPickUser={pickUser} onOpenSection={openSection} /> },
      ]}
    />
  </div>;
}
