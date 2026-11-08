import { sceneBudgetLabel } from "./format";

test("scene budget uses narration time, not target divided into 30s slots", () => {
  const budget = sceneBudgetLabel({
    targetDuration: 136,
    productionMode: "editorial",
    scenes: [
      { duration: 31, end_time: 31 },
      { duration: 33, end_time: 59 },
      { duration: 36, end_time: 90 },
      { duration: 36, end_time: 118 },
    ],
  });
  expect(budget.narrationBudget).toBe(133.5);
  expect(budget.expected).toBe(5);
  expect(budget.actual).toBe(4);
  expect(budget.mismatch).toBe(true);
  expect(budget.text).toContain("target 136s");
  expect(budget.text).toContain("expected 5 scenes");
  expect(budget.text).toContain("timeline ends 1:58");
  expect(budget.text).toContain("summed durations 136s");
  expect(budget.text).not.toContain("× 30s");
});
