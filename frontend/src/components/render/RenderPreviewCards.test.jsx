import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";

import RenderPreviewCards from "./RenderPreviewCards";

function foreground(html) {
  const tags = html.match(/<img\b[^>]*>/g) || [];
  return tags.find((tag) => tag.includes('data-testid="render-preview-thumbnail"')) || "";
}

test("a landscape thumbnail is letterboxed inside the 9:16 frame", () => {
  const html = renderToStaticMarkup(
    <RenderPreviewCards
      aspect="9:16"
      selectedVoice={null}
      selectedThumbnail={{ preview_url: "/wide.png", width: 1280, height: 720 }}
    />
  );
  const image = foreground(html);
  expect(image).toContain("object-contain");
  expect(image).not.toContain("object-cover");
  expect(html).toContain('data-testid="render-preview-frame"');
  expect(html).toContain("aspect-ratio:9 / 16");
  expect(html).toContain("blur-xl");
});

test("a portrait thumbnail fills the 9:16 frame", () => {
  const html = renderToStaticMarkup(
    <RenderPreviewCards
      aspect="9:16"
      selectedVoice={null}
      selectedThumbnail={{ preview_url: "/tall.png", width: 1080, height: 1920 }}
    />
  );
  const image = foreground(html);
  expect(image).toContain("object-cover");
  expect(image).not.toContain("object-contain");
  expect(html).toContain("aspect-ratio:9 / 16");
  expect(html).toContain("h-full");
  expect(html).not.toContain("blur-xl");
});

test("a portrait image measured on load fills the frame", async () => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement("div");
  document.body.appendChild(element);
  const root = createRoot(element);
  await act(async () => {
    root.render(
      <RenderPreviewCards
        aspect="9:16"
        selectedVoice={null}
        selectedThumbnail={{ preview_url: "/tall.png" }}
      />,
    );
  });
  const image = element.querySelector("[data-testid='render-preview-thumbnail']");
  expect(image.className).toContain("object-contain");
  Object.defineProperty(image, "naturalWidth", { configurable: true, value: 1080 });
  Object.defineProperty(image, "naturalHeight", { configurable: true, value: 1920 });
  await act(async () => {
    image.dispatchEvent(new Event("load", { bubbles: true }));
  });
  expect(element.querySelector("[data-testid='render-preview-thumbnail']").className).toContain("object-cover");
  expect(element.querySelector("[data-testid='render-preview-frame']").style.aspectRatio).toBe("9 / 16");
  await act(async () => { root.unmount(); });
  element.remove();
});
