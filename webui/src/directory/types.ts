// 群组 / 用户页 2.0 的接口类型，与后端 webui/directory.py 的返回一致。
import type { FeatureState } from "../types";

export type FeatureSource = FeatureState["source"] | "agent_config";

export interface GroupRow {
  groupId: string;
  groupName?: string | null;
  firstSeenAt: string | null;
  lastActiveAt?: string | null;
  memberCount: number;
  activeMembers7d: number;
  agentEnabled: boolean;
  personaEnabled: boolean;
  lastAgentAt: string | null;
  overrideCount: number;
  disabledFeatures: string[];
  memberOverrideCount: number;
  checkinsToday: number;
}

export interface GroupsSummary {
  total: number;
  agentEnabled: number;
  active24h: number;
  active7d: number;
  withOverrides: number;
  checkinsToday: number;
  members: number;
}

export interface GroupDetail {
  groupId: string;
  groupName?: string | null;
  firstSeenAt?: string | null;
  lastActiveAt?: string | null;
  memberCount: number;
  features: FeatureState[];
}

export interface RankedMember {
  userId: string;
  name: string;
}

export interface GroupOverview {
  groupId: string;
  roles: Record<string, number>;
  activeMembers: { "24h": number; "7d": number; "30d": number };
  checkins: {
    today: number;
    yesterday: number;
    streakTop: (RankedMember & { streakDays: number; totalDays: number; checkedToday: boolean })[];
  };
  affinityTop: (RankedMember & { groupAffinity: number })[];
  recentMembers: (RankedMember & { role: string; lastSeenAt: string | null })[];
  agent: {
    configured: boolean;
    enabled: boolean;
    personaEnabled: boolean;
    proactiveEnabled: boolean;
    dailyLimit: number | null;
    lastAgentAt: string | null;
  };
  overrides: { group: number; member: number; membersWithOverrides: number };
}

export interface MemberRow {
  userId: string;
  nickname?: string | null;
  groupNickname?: string | null;
  role: string;
  title?: string | null;
  firstSeenAt?: string | null;
  lastSeenAt?: string | null;
  active: boolean;
  groupAffinity: number;
  streakDays: number;
  totalDays: number;
  checkedToday: boolean;
  overrideCount: number;
}

export interface MemberOverride {
  userId: string;
  name: string;
  role: string;
  feature: string;
  featureName: string;
  enabled: boolean;
  effective: boolean;
  groupEffective: boolean;
  blockedByGroup: boolean;
  redundant: boolean;
}

export interface UserRow {
  userId: string;
  nickname?: string | null;
  firstInteractionAt: string | null;
  lastInteractionAt?: string | null;
  affinity: number;
  affinityLevel?: string;
  groupCount: number;
  overrideCount: number;
  memberOverrideCount: number;
}

export interface UsersSummary {
  total: number;
  active24h: number;
  active7d: number;
  withOverrides: number;
  withMemberOverrides: number;
  checkinsToday: number;
  privateAgentPaused: number;
}

export interface Membership {
  groupId: string;
  groupName?: string | null;
  groupNickname?: string | null;
  role: string;
  title?: string | null;
  active: boolean;
  firstSeenAt?: string | null;
  lastSeenAt?: string | null;
  groupAffinity: number;
  streakDays: number;
  totalDays: number;
  checkedToday: boolean;
  overrideCount: number;
  agentEnabled: boolean;
}

export interface UserProfile {
  userId: string;
  nickname?: string | null;
  firstInteractionAt: string | null;
  lastInteractionAt?: string | null;
  affinity: number;
  affinityLevel: string;
  level: { name: string; nextLevel: string | null; toNext: number | null; progress: number };
  groupCount: number;
  checkedTodayCount: number;
  privateOverrides: number;
  memberOverrides: number;
  memberships: Membership[];
}

export interface BatchResult {
  updated: number;
  feature: string;
}
