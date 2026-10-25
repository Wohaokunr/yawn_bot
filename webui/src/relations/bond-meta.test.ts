import { describe, expect, it } from "vitest";
import type { AgentBondItem } from "../types";
import { affinityWord, bondSentence, initiativeRatio, isLocked, stageMeta, trustWord } from "./bond-meta";

const base: AgentBondItem = {
  id: "1", userId: "7", name: "阿眠", stage: "friend", stageLabel: "朋友", familiarity: 0.6,
  label: "爱逗我的老朋友", activeDays: 4, lastInteractionAt: null, knownDays: 30,
  memberToBotCount: 6, botToMemberCount: 2, lockedFields: ["member_calls_bot"],
};

describe("bond-meta", () => {
  it("maps stages and falls back to stranger", () => {
    expect(stageMeta("close").label).toBe("亲近");
    expect(stageMeta("unknown").label).toBe("陌生");
  });

  it("describes affinity and trust in words, matching backend thresholds", () => {
    expect(affinityWord(0.6)).toBe("很亲近");
    expect(affinityWord(0.2)).toBe("友好");
    expect(affinityWord(0)).toBe("中性");
    expect(affinityWord(-0.2)).toBe("略有不满");
    expect(affinityWord(-0.5)).toBe("有过不愉快");
    expect(trustWord(0.8)).toBe("很信任");
    expect(trustWord(undefined)).toBe("一般");
  });

  it("reads locks by backend field names", () => {
    expect(isLocked(base, "memberCallsBot")).toBe(true);
    expect(isLocked(base, "label")).toBe(false);
  });

  it("builds a sentence and initiative ratio", () => {
    expect(bondSentence(base)).toBe("朋友 · 爱逗我的老朋友 · 认识 30 天 · 来往 4 天");
    expect(initiativeRatio(base)).toBe(0.75);
    expect(initiativeRatio({ ...base, memberToBotCount: 0, botToMemberCount: 0 })).toBeNull();
  });
});
