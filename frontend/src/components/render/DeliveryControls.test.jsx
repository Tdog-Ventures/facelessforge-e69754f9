import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import JobStatusCard from "./JobStatusCard";
import LastTerminalNotice from "./LastTerminalNotice";


test("completed render uses the stable authenticated delivery URL", () => {
  const html = renderToStaticMarkup(
    <JobStatusCard
      job={{
        status: "completed",
        progress: 100,
        current_step: "completed",
        output_url: "https://object.example/expired-signed-url",
        duration: 60,
      }}
      project={{ name: "Test project" }}
      downloadUrl="/api/projects/project-1/render/jobs/job-1/download"
      canEdit={false}
    />
  );

  expect(html).toContain('src="/api/projects/project-1/render/jobs/job-1/download"');
  expect(html).toContain('href="/api/projects/project-1/render/jobs/job-1/download"');
  expect(html).not.toContain("expired-signed-url");
});


test("failed render notice exposes an explicit retry action", () => {
  const html = renderToStaticMarkup(
    <LastTerminalNotice
      job={{
        status: "FAILED",
        error_message: "Voiceover provider unavailable",
        completed_at: new Date().toISOString(),
      }}
      canEdit
      starting={false}
      onRetry={() => {}}
    />
  );

  expect(html).toContain("Voiceover provider unavailable");
  expect(html).toContain("Retry render");
});


test("verification remains an active cancellable render phase", () => {
  const html = renderToStaticMarkup(
    <JobStatusCard
      job={{
        status: "verifying",
        progress: 96,
        current_step: "video_qa",
      }}
      project={{ name: "Test project" }}
      canEdit
      cancelling={false}
      onCancel={() => {}}
    />
  );

  expect(html).toContain('data-testid="render-cancel-btn"');
});
