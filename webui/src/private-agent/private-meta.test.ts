import { describe, expect, it } from "vitest";
import type { PrivateSectionRow } from "./private-api";
import {
  cleanUserId,
  formatHitRate,
  filterPrivateSections,
  groupCounts,
  inheritedOf,
  issueScopeLabel,
  ownOverride,
  parsePrivateImport,
  parsePrivateTab,
  personaActionLabel,
  pipelineLabel,
  tokenBar,
  worstIssue,
} from "./private-meta";

function row(patch: Partial<PrivateSectionRow> = {}): PrivateSectionRow {
  return {
    id: "private.rules",
    title: "私聊规则",
    group: "rules",
    groupLabel: "规则与安全",
    category: "private",
    layer: "private_system",
    layerLabel: "私聊 system",
    kind: "static",
    description: "",
    when: "每轮",
    safety: false,
    protocol: false,
    editable: true,
    modes: ["replace", "append", "prepend", "disable", "builtin"],
    defaultText: "默认规则",
    defaultTokens: 3,
    sample: null,
    globalOverride: null,
    userOverride: null,
    userOverrideCount: 0,
    effectiveScope: null,
    effectiveMode: "default",
    effectiveText: "默认规则",
    effectiveTokens: 3,
    pipelines: [{ pipeline: "turn", label: "主回合", where: "system#1", condition: "" }],
    ...patch,
  };
}

const override = (mode: "append" | "replace" | "disable" | "builtin", text = "") => ({ mode, text, note: "", updatedAt: "", updatedBy: "" });

describe("private-meta", () => {
  it("parses tabs and QQ numbers defensively", () => {
    expect(parsePrivateTab("persona")).toBe("persona");
    expect(parsePrivateTab("nope")).toBe("prompts");
    expect(cleanUserId(" 12345 ")).toBe("12345");
    expect(cleanUserId("abc")).toBe("");
    expect(cleanUserId("12")).toBe("");
  });

  it("layers user overrides on top of global", () => {
    const item = row({ globalOverride: override("append", "全局追加"), userOverride: override("disable") });
    expect(ownOverride(item, "user")?.mode).toBe("disable");
    expect(ownOverride(item, "global")?.mode).toBe("append");
    expect(inheritedOf(item, "user")).toBe("默认规则\n全局追加");
    expect(inheritedOf(item, "global")).toBe("默认规则");
    expect(inheritedOf(row({ globalOverride: override("builtin") }), "user")).toBe("默认规则");
  });

  it("filters by group, pipeline, own overrides and search", () => {
    const rows = [
      row(),
      row({ id: "private.safety", group: "rules", safety: true, userOverride: override("append", "x"), effectiveMode: "append" }),
      row({ id: "private.worker_role", group: "worker", pipelines: [{ pipeline: "worker", label: "后台小助手", where: "system#1", condition: "" }], userOverrideCount: 3 }),
    ];
    expect(filterPrivateSections(rows, { group: "rules", filter: "all", query: "", pipeline: "" })).toHaveLength(2);
    expect(filterPrivateSections(rows, { group: "all", filter: "mine", query: "", pipeline: "" }).map((item) => item.id)).toEqual(["private.safety"]);
    expect(filterPrivateSections(rows, { group: "all", filter: "all", query: "", pipeline: "worker" }).map((item) => item.id)).toEqual(["private.worker_role"]);
    expect(filterPrivateSections(rows, { group: "all", filter: "tools", query: "", pipeline: "" })).toHaveLength(1);
    expect(filterPrivateSections(rows, { group: "all", filter: "all", query: "WORKER", pipeline: "" })).toHaveLength(1);
    expect(groupCounts(rows).rules).toEqual({ total: 2, modified: 1 });
  });

  it("imports only private sections", () => {
    const parsed = parsePrivateImport(JSON.stringify({ overrides: { "private.rules": { mode: "append", text: "a" }, "core.role_intro": { mode: "disable" }, "ptool.notes": { mode: "replace", text: "b" } } }));
    expect(parsed.error).toBeNull();
    expect(parsed.changes.map((item) => item.sectionId)).toEqual(["private.rules", "ptool.notes"]);
    expect(parsed.skipped).toBe(1);
    expect(parsePrivateImport("{").error).toBe("不是合法的 JSON");
    expect(parsePrivateImport(JSON.stringify({ overrides: { "core.x": { mode: "disable" } } })).error).toBe("没有可导入的私聊分段");
  });

  it("labels issues, pipelines, persona actions and token bars", () => {
    expect(worstIssue([])).toBeNull();
    expect(worstIssue([{ level: "info", code: "a", sectionId: "x", message: "", scope: "global" }, { level: "danger", code: "b", sectionId: "y", message: "", scope: "user:1" }])).toBe("danger");
    expect(issueScopeLabel("user:12345")).toBe("用户 12345");
    expect(pipelineLabel("report")).toBe("汇报");
    expect(personaActionLabel("copy_group")).toBe("复制群聊人设");
    expect(tokenBar(800, 700)).toEqual({ percent: 100, status: "exception" });
    expect(tokenBar(350, 700)).toEqual({ percent: 50, status: "normal" });
  });
});

describe("withPipelineTask", () => {
  it("labels private snapshots by pipeline", async () => {
    const { withPipelineTask } = await import("./private-meta");
    const { taskLabel } = await import("../agent-prompts/prompt-meta");
    const snap = withPipelineTask({ task: "agent_dialogue", pipeline: "worker" } as never);
    expect(taskLabel(snap.task)).toBe("私聊小助手");
    expect(withPipelineTask({ task: "agent_dialogue" } as never).task).toBe("agent_dialogue");
  });
});

describe("formatHitRate", () => {
  it("shows a percentage with one decimal, or a dash when the provider did not report usage", () => {
    expect(formatHitRate(0.8765)).toBe("87.7%");
    expect(formatHitRate(0)).toBe("0%");
    expect(formatHitRate(null)).toBe("—");
    expect(formatHitRate(undefined)).toBe("—");
  });
});
