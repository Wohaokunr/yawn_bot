import { ApiOutlined, SafetyOutlined } from "@ant-design/icons";
import { Segmented } from "antd";
import { useNavigate } from "react-router-dom";
import { confirmDiscardChanges } from "./shared";

export type GroupWorkspaceView = "agent" | "access";

// 同一个群的 Agent 工作台与成员与权限（概览、群功能、成员、覆盖一览）是同一处工作区的两个视图，用分段切换互通。
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
        { value: "access", label: "成员与权限", icon: <SafetyOutlined /> },
      ]}
    />
  );
}
