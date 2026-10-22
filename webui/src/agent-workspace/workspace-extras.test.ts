import { describe, expect, it } from "vitest";
import type { AgentDiagnostics } from "../types";
import { diagnosticsReport, healthScore, healthTone } from "./CockpitExtras";
import { CONFIG_EXPORT_KIND, parseConfigImport, pickPortableConfig, serializeConfigExport, TOOL_PRESETS } from "./config-transfer";

function diagnostics(partial: { enabled?: boolean; blockers?: AgentDiagnostics["blockers"]; failures?: number } = {}): AgentDiagnostics {
  return {
    groupId: "1",
    tools: { adminToday: 1, adminLimit: 10, criticalToday: 0, criticalLimit: 2, allowlist: [] },
    effective: {
      enabled: partial.enabled ?? true,
      replyTriggerEnabled: true,
      explicitWakeupEnabled: true,
      proactiveEnabled: true,
      proactiveActiveEnabled: true,
      proactiveToday: 0,
      dailyLimit: 30,
      dailyRemaining: 30,
      cooldownMinutes: 8,
      cooldownRemainingMinutes: 0,
      lastAgentAt: null,
      lastProactiveAt: null,
      activeTopic: null,
      mediaCacheEnabled: false,
      shortConversation: { enabled: true, active: false, sessionId: null, topic: null, botTurns: 0, evaluations: 0, consecutiveWaits: 0 },
    },
    memory: {
      groupId: "1", runtimeEnabled: true, pendingMessages: 0, lastCompactedMessageId: null, lastCompactedAt: null,
      countsByType: {}, total: 3, oldestUpdatedAt: null, newestUpdatedAt: null, rebuildRequired: false,
      lastAttemptAt: null, lastSuccessAt: null, lastError: null, consecutiveFailures: partial.failures ?? 0, inFlight: false,
    },
    llm: { routes: [{ task: "agent_dialogue", profile: "default", provider: "p", model: "m", thinking: "auto", multimodal: "auto", configured: true }], unconfiguredProviders: [] },
    blockers: partial.blockers ?? [],
    generatedAt: "2026-10-22T12:00:00+08:00",
  };
}

describe("health score", () => {
  it("weights blockers by severity and clamps to 0..100", () => {
    expect(healthScore(diagnostics())).toBe(100);
    expect(healthScore(diagnostics({ enabled: false }))).toBe(0);
    expect(healthScore(diagnostics({ blockers: [{ code: "a", severity: "error", title: "", detail: "" }] }))).toBe(65);
    expect(healthScore(diagnostics({ blockers: [{ code: "b", severity: "warning", title: "", detail: "" }], failures: 2 }))).toBe(78);
    expect(healthTone(90)).toBe("ok");
    expect(healthTone(60)).toBe("warning");
    expect(healthTone(10)).toBe("error");
  });

  it("produces a pasteable text report", () => {
    const report = diagnosticsReport("猫窝（1）", diagnostics({ blockers: [{ code: "x", severity: "error", title: "Bot 当前离线", detail: "无连接" }] }));
    expect(report).toContain("猫窝（1）");
    expect(report).toContain("健康分：65 / 100");
    expect(report).toContain("特权工具：1/10");
    expect(report).toContain("- [error] Bot 当前离线：无连接");
  });
});

describe("config transfer", () => {
  it("only keeps portable, well-typed fields", () => {
    expect(pickPortableConfig({
      enabled: false,
      version: "v1",
      dailyLimit: 9,
      cooldownMinutes: "x",
      proactiveEnabled: true,
      crossGroupVisibility: "everyone",
      toolAllowlist: ["mute_member"],
    })).toEqual({ dailyLimit: 9, proactiveEnabled: true, toolAllowlist: ["mute_member"] });
  });

  it("round-trips an export file and accepts a bare config object", () => {
    const text = serializeConfigExport("1", { dailyLimit: 12, enabled: true });
    expect(JSON.parse(text).kind).toBe(CONFIG_EXPORT_KIND);
    expect(parseConfigImport(text)).toEqual({ dailyLimit: 12 });
    expect(parseConfigImport('{"idleThresholdMinutes": 20}')).toEqual({ idleThresholdMinutes: 20 });
    expect(() => parseConfigImport("not json")).toThrow("有效的 JSON");
    expect(() => parseConfigImport('{"enabled": true}')).toThrow("没有可导入");
  });

  it("never puts critical tools into a preset", () => {
    const critical = ["kick_member", "set_whole_group_mute", "set_group_admin", "delete_group_file", "move_group_file", "rename_group_file", "delete_group_folder"];
    for (const preset of TOOL_PRESETS) {
      expect(preset.tools.some((tool) => critical.includes(tool))).toBe(false);
    }
  });
});
