import { Select, Space, Tag, Typography } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Member } from "../types";

const { Text } = Typography;

const ROLE_LABEL: Record<string, { label: string; color: string }> = {
  owner: { label: "群主", color: "gold" },
  admin: { label: "管理员", color: "green" },
  bot: { label: "Bot", color: "blue" },
};

export function roleTag(role: string | undefined): React.JSX.Element | null {
  const meta = role ? ROLE_LABEL[role] : undefined;
  return meta ? <Tag color={meta.color} className="rel-role-tag">{meta.label}</Tag> : null;
}

// 成员选择器：按群昵称/QQ 昵称/QQ 号远程搜索，也接受直接输入的纯数字 QQ。
export function MemberPicker({
  groupId,
  value,
  onChange,
  placeholder,
  exclude,
  nameOf,
  id,
  autoFocus,
}: {
  groupId: string;
  value?: string;
  onChange?: (value: string | undefined) => void;
  placeholder: string;
  exclude?: string;
  nameOf: (userId: string) => string;
  id?: string;
  autoFocus?: boolean;
}): React.JSX.Element {
  const [keyword, setKeyword] = useState("");
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(false);
  const timerRef = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      setLoading(true);
      api<Member[]>(`/groups/${groupId}/members?page=1&pageSize=20&search=${encodeURIComponent(keyword.trim())}`, { signal: controller.signal })
        .then((response) => setMembers(response.data))
        .catch(() => { if (!controller.signal.aborted) setMembers([]); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, keyword ? 220 : 0);
    return () => {
      controller.abort();
      window.clearTimeout(timerRef.current);
    };
  }, [groupId, keyword]);

  const options = useMemo(() => {
    const list = members
      .filter((member) => member.userId !== exclude)
      .map((member) => {
        const name = (member.groupNickname || member.nickname || "").trim() || member.userId;
        return {
          value: member.userId,
          title: name,
          label: <Space size={6} className="rel-member-option">
            <span className="rel-avatar is-xs" aria-hidden="true" data-initial={name.slice(0, 1)} />
            <Text>{name}</Text>
            <Text type="secondary" className="rel-mono">{member.userId}</Text>
            {roleTag(member.role)}
          </Space>,
        };
      });
    const raw = keyword.trim();
    if (/^\d{5,12}$/.test(raw) && !list.some((option) => option.value === raw) && raw !== exclude) {
      list.unshift({ value: raw, title: raw, label: <Space size={6}><Text>使用 QQ</Text><Text strong className="rel-mono">{raw}</Text></Space> });
    }
    if (value && !list.some((option) => option.value === value)) {
      const name = nameOf(value);
      list.unshift({ value, title: name, label: <Space size={6}><Text>{name}</Text><Text type="secondary" className="rel-mono">{value}</Text></Space> });
    }
    return list;
  }, [members, exclude, keyword, value, nameOf]);

  return (
    <Select
      id={id}
      autoFocus={autoFocus}
      showSearch={{ filterOption: false, onSearch: setKeyword }}
      allowClear
      value={value}
      placeholder={placeholder}
      loading={loading}
      options={options}
      optionLabelProp="title"
      notFoundContent={loading ? "搜索中…" : keyword ? "没有匹配的成员，可直接输入 QQ 号" : "输入昵称或 QQ 搜索"}
      onChange={(next) => {
        onChange?.(next ?? undefined);
        setKeyword("");
      }}
      style={{ width: "100%" }}
    />
  );
}
