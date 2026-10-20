import { ApiOutlined, SafetyOutlined } from "@ant-design/icons";
import { Segmented } from "antd";
import { useNavigate } from "react-router-dom";
import { confirmDiscardChanges } from "./shared";

export type GroupWorkspaceView = "agent" | "access";

// 同一个群的 Agent 工作台与功能权限是同一处工作区的两个视图，用分段切换互通。
export function GroupWorkspaceSwitch({
  groupId,
  active,
}: {
  groupId: string;
  active: GroupWorkspaceView;
}): React.JSX.Element {
  const navigate = useNavigate();
  return (
    <Segmented
      className="group-workspace-switch"
      value={active}
      onChange={(value) => {
        if (value === active || !confirmDiscardChanges()) return;
        navigate(value === "agent" ? `/agent/${groupId}` : `/groups/${groupId}`);
      }}
      options={[
        { value: "agent", label: "Agent 工作台", icon: <ApiOutlined /> },
        { value: "access", label: "功能权限", icon: <SafetyOutlined /> },
      ]}
    />
  );
}
