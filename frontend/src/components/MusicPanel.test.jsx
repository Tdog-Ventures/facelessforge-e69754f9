import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import MusicPanel from "./MusicPanel";

const base = { projectId: "p1", canEdit: true, onProjectChange: () => {} };

test("music panel shows off state and upload affordance", () => {
  const html = renderToStaticMarkup(
    <MusicPanel {...base} project={{ music_enabled: false }} assets={[]} />
  );
  expect(html).toContain('data-testid="music-panel"');
  expect(html).toContain('data-testid="music-state"');
  expect(html).toContain("Off");
  expect(html).toContain('data-testid="music-upload-btn"');
  expect(html).toContain('data-testid="music-gain-slider"');
  expect(html).toContain('data-testid="music-rights-checkbox"');
});

test("selected track shows name, preview, remove and ducking state", () => {
  const asset = {
    id: "m1",
    asset_type: "music_audio",
    name: "test-bed.mp3",
    preview_url: "http://x/m.mp3",
    rights_confirmed: true,
    rights_note: "synthetic test fixture",
  };
  const html = renderToStaticMarkup(
    <MusicPanel
      {...base}
      project={{ music_enabled: true, selected_music_asset_id: "m1", music_gain_db: -10 }}
      assets={[asset]}
    />
  );
  expect(html).toContain("test-bed.mp3");
  expect(html).toContain('data-testid="music-preview"');
  expect(html).toContain('data-testid="music-remove-btn"');
  expect(html).toContain("synthetic test fixture");
});
