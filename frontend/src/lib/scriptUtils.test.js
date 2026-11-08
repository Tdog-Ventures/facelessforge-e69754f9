import {
  countWords,
  estimateDurationSeconds,
  formatNarration,
  validateManualScript,
  invalidationNotice,
  chapterFields,
  saveScript,
  MIN_MANUAL_WORDS,
} from "./scriptUtils";

test("word count and duration math matches backend (2.5 wps)", () => {
  expect(countWords("word ".repeat(60))).toBe(60);
  expect(countWords("  ")).toBe(0);
  expect(countWords(null)).toBe(0);
  expect(estimateDurationSeconds(60)).toBe(24);
  expect(formatNarration(24)).toBe("0:24");
  expect(formatNarration(90)).toBe("1:30");
});

test("manual validation rejects empty, short, and oversize scripts", () => {
  expect(validateManualScript("").ok).toBe(false);
  expect(validateManualScript("   ").ok).toBe(false);
  const short = validateManualScript("only a few words here");
  expect(short.ok).toBe(false);
  expect(short.error).toContain(String(MIN_MANUAL_WORDS));
  const long = validateManualScript(`ok ${"x".repeat(20001)}`);
  expect(long.ok).toBe(false);
  expect(long.error).toContain("20000");
});

test("manual validation accepts a real script with stats", () => {
  const text = "Everyone thinks building an AI YouTube video generator is simple. Pick a topic. Write a prompt. Press generate. But a real video platform needs scripts, visuals, narration, captions, rendering, storage, retries, billing, and quality checks together.";
  const result = validateManualScript(text);
  expect(result.ok).toBe(true);
  expect(result.words).toBeGreaterThanOrEqual(MIN_MANUAL_WORDS);
  expect(result.seconds).toBe(Math.floor(result.words / 2.5));
  expect(result.cleaned).toBe(text.trim());
});

test("invalidation notice summarises dependent drafts, never masters", () => {
  expect(invalidationNotice({})).toBe(null);
  expect(invalidationNotice({ script_invalidated: { scenes: 0, metadata: 0, assets: 0 } })).toBe(null);
  const notice = invalidationNotice({
    script_invalidated: { scenes: 2, metadata: 1, assets: 3 },
    script_notice: "custom",
  });
  expect(notice).toBe("custom");
  const fallback = invalidationNotice({ script_invalidated: { scenes: 1, metadata: 1, assets: 2 } });
  expect(fallback).toContain("regenerate");
  expect(fallback).toContain("Completed videos were kept");
});

test("chapter rows accept generator keys and UI keys", () => {
  expect(chapterFields({ time: "0:00", label: "Intro" })).toEqual({ timestamp: "0:00", title: "Intro" });
  expect(chapterFields({ timestamp: "1:22", title: "Fry" })).toEqual({ timestamp: "1:22", title: "Fry" });
});

test("saveScript posts manual source and returns the project view", async () => {
  const view = { project: { id: "p1" }, script: { full_script: "word ".repeat(30) } };
  const api = { patch: jest.fn().mockResolvedValue({ data: view }) };
  const out = await saveScript(api, "p1", { full_script: "word ".repeat(30), source: "manual" });
  expect(api.patch).toHaveBeenCalledWith("/projects/p1/script", {
    full_script: expect.any(String),
    source: "manual",
  });
  expect(out).toBe(view);
});

test("saveScript surfaces server errors as Error", async () => {
  const api = { patch: jest.fn().mockRejectedValue({ response: { data: { detail: "credits_exhausted" } } }) };
  await expect(saveScript(api, "p1", { full_script: "x", source: "manual" })).rejects.toThrow();
});
