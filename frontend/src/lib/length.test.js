import { formatLength, lengthIndex, lengthStops } from "./length";

test("lengths under a minute stay in seconds and longer lengths use minutes", () => {
  expect(formatLength(5)).toBe("5 seconds");
  expect(formatLength(45)).toBe("45 seconds");
  expect(formatLength(59)).toBe("59 seconds");
  expect(formatLength(60)).toBe("1 minute");
  expect(formatLength(90)).toBe("2 minutes");
  expect(formatLength(120)).toBe("2 minutes");
  expect(formatLength(136)).toBe("2 minutes");
  expect(formatLength(300)).toBe("5 minutes");
  expect(formatLength(3600)).toBe("60 minutes");
});

test("the scale steps by 5 seconds until a minute, then by whole minutes", () => {
  const stops = lengthStops();
  expect(stops[0]).toBe(5);
  expect(stops).toContain(55);
  expect(stops).not.toContain(56);
  expect(stops).not.toContain(90);
  expect(stops).toContain(60);
  expect(stops).toContain(120);
  expect(stops).toContain(300);
  expect(stops[stops.length - 1]).toBe(3600);
  expect(new Set(stops).size).toBe(stops.length);
  expect(stops[lengthIndex(300)]).toBe(300);
  expect(stops[lengthIndex(136)]).toBe(120);
  expect(stops[lengthIndex(45)]).toBe(45);
});
