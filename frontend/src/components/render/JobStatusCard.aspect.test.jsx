import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import JobStatusCard from "./JobStatusCard";

const done = (extra = {}) => ({
  status: "completed", progress: 100, current_step: "completed",
  output_url: "/v.mp4", duration: 136, ...extra,
});

test("a 9:16 render is labelled 1080×1920 in a portrait player", () => {
  const html = renderToStaticMarkup(
    <JobStatusCard job={done({ aspect_ratio: "9:16" })} project={{ name: "Egg" }} canEdit={false} />
  );
  expect(html).toContain("1080×1920");
  expect(html).not.toContain("1920×1080");
  expect(html).toContain("aspect-ratio:9 / 16");
  expect(html).not.toContain("aspect-video");
});

test("a 16:9 render keeps the landscape label and player", () => {
  const html = renderToStaticMarkup(
    <JobStatusCard job={done({ aspect_ratio: "16:9" })} project={{ name: "Wide" }} canEdit={false} />
  );
  expect(html).toContain("1920×1080");
  expect(html).toContain("aspect-ratio:16 / 9");
});

test("the project aspect is used when the job has none, 1:1 included", () => {
  const html = renderToStaticMarkup(
    <JobStatusCard job={done()} project={{ name: "Sq", aspect_ratio: "1:1" }} canEdit={false} />
  );
  expect(html).toContain("1080×1080");
  expect(html).toContain("aspect-ratio:1 / 1");
});
