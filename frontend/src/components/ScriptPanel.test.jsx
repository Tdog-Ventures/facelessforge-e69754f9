import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import ScriptPanel from "./ScriptPanel";
import ManualScriptForm from "./ManualScriptForm";

const baseProps = { projectId: "project-1", canEdit: true, onChange: () => {} };

test("script tab shows both AI and manual entry when no script exists", () => {
  const html = renderToStaticMarkup(<ScriptPanel {...baseProps} script={null} />);
  expect(html).toContain("No script yet");
  expect(html).toContain('data-testid="generate-script-btn"');
  expect(html).toContain("Generate Script with AI");
  expect(html).toContain('data-testid="manual-script-btn"');
  expect(html).toContain("Write or Paste Script");
  expect(html).not.toContain('data-testid="manual-script-form"');
});

test("manual form renders textarea, stats, save and cancel", () => {
  const html = renderToStaticMarkup(
    <ManualScriptForm projectId="project-1" onSaved={() => {}} onCancel={() => {}} />
  );
  expect(html).toContain('data-testid="manual-script-form"');
  expect(html).toContain('data-testid="manual-script-textarea"');
  expect(html).toContain('data-testid="manual-script-stats"');
  expect(html).toContain("0 words");
  expect(html).toContain('data-testid="save-manual-script-btn"');
  expect(html).toContain("Save Script");
  expect(html).toContain('data-testid="cancel-manual-script-btn"');
  expect(html).toContain("no AI used");
});

test("existing manual script is editable with a Manual badge and save", () => {
  const script = {
    id: "s1",
    full_script: "word ".repeat(30),
    word_count: 30,
    estimated_duration: 12,
    source: "manual",
  };
  const html = renderToStaticMarkup(<ScriptPanel {...baseProps} script={script} />);
  expect(html).toContain('data-testid="script-textarea"');
  expect(html).toContain('data-testid="script-source-badge"');
  expect(html).toContain("Manual");
  expect(html).toContain('data-testid="save-script-btn"');
  expect(html).toContain('data-testid="regenerate-script-btn"');
});

test("existing AI script shows AI badge without manual overwrite warning", () => {
  const script = {
    id: "s1",
    full_script: "word ".repeat(30),
    word_count: 30,
    estimated_duration: 12,
    source: "ai_generated",
    hook_option_one: "Hook one",
  };
  const html = renderToStaticMarkup(<ScriptPanel {...baseProps} script={script} />);
  expect(html).toContain("AI");
  expect(html).not.toContain('data-testid="regenerate-confirm"');
});
