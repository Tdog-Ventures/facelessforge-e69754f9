const http = require("http");
const fs = require("fs");
const path = require("path");

let chromium;
try {
  ({ chromium } = require("playwright"));
} catch {
  try {
    ({ chromium } = require("playwright-core"));
  } catch {
    ({ chromium } = require("/tmp/opencode/node_modules/playwright-core"));
  }
}

const BUILD_DIR = path.join(__dirname, "..", "build");
const AXE_PATH = (() => {
  try {
    return require.resolve("axe-core/axe.min.js");
  } catch {
    return path.join(__dirname, "..", "node_modules", "axe-core", "axe.min.js");
  }
})();
const OUT_DIR = process.env.EVIDENCE_OUT_DIR || "";
const CHROME_Candidates = [
  process.env.CHROME_PATH,
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium-browser",
  "/usr/bin/chromium",
].filter(Boolean);
const EXECUTABLE_PATH = CHROME_Candidates.find((p) => fs.existsSync(p));

const MIME = {
  ".html": "text/html",
  ".js": "application/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

function startServer() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const urlPath = req.url.split("?")[0];
      if (urlPath === "/api/users/me" || urlPath === "/api/auth/me") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ id: "u1", email: "user@example.com", name: "Test User", role: "user" }));
        return;
      }
      if (urlPath === "/api/health") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ status: "ok" }));
        return;
      }
      if (urlPath.startsWith("/api/")) {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({}));
        return;
      }
      let filePath = path.join(BUILD_DIR, urlPath === "/" ? "index.html" : urlPath);
      if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
        filePath = path.join(BUILD_DIR, "index.html");
      }
      const ext = path.extname(filePath);
      res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream" });
      res.end(fs.readFileSync(filePath));
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

(async () => {
  const server = await startServer();
  const port = server.address().port;
  const launchOpts = { args: ["--no-sandbox"] };
  if (EXECUTABLE_PATH) launchOpts.executablePath = EXECUTABLE_PATH;
  const browser = await chromium.launch(launchOpts);
  const failures = [];
  const report = {};

  for (const theme of ["light", "dark"]) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 2200 } });
    const page = await context.newPage();

    await page.route("**/api/**", async (route) => {
      const url = route.request().url();
      if (url.includes("/api/users/me") || url.includes("/api/auth/me")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ id: "u1", email: "user@example.com", name: "Test User", role: "user" }),
        });
        return;
      }
      if (url.includes("/api/health")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "ok" }),
        });
        return;
      }
      // Delay POST /projects slightly if tested
      if (route.request().method() === "POST" && url.includes("/api/projects")) {
        await new Promise((r) => setTimeout(r, 400));
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ id: "p-test", name: "Test" }),
        });
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });

    await page.addInitScript((t) => {
      window.localStorage.setItem("theme", t);
      window.localStorage.setItem("forge_invite", "FORGE");
    }, theme);

    await page.goto(`http://127.0.0.1:${port}/app/projects/new`, { waitUntil: "networkidle" });
    await page.waitForSelector('[data-testid="create-project-form"]');

    if (OUT_DIR) {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(OUT_DIR, `create-project-${theme}-default.png`), fullPage: true });
    }

    // Expand commercial mode, auto-post, and trigger validation errors
    await page.click('[data-testid="field-commercial"]');
    await page.click('#auto-post');
    await page.click('[data-testid="create-project-submit"]');
    await page.waitForSelector('[data-testid="error-name"]');

    // Focus name input to measure focus indicator
    await page.focus('[data-testid="field-name"]');

    if (OUT_DIR) {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(OUT_DIR, `create-project-${theme}-commercial-errors.png`), fullPage: true });
    }

    await page.addScriptTag({ path: AXE_PATH });

    const metrics = await page.evaluate(async () => {
      function parseColor(str) {
        const m = str && str.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([0-9.]+))?\)/);
        if (!m) return null;
        return { r: +m[1], g: +m[2], b: +m[3], a: m[4] !== undefined ? +m[4] : 1 };
      }
      function blend(fg, bg) {
        if (!fg) return bg;
        if (fg.a >= 1 || !bg) return { r: fg.r, g: fg.g, b: fg.b, a: 1 };
        const a = fg.a;
        return {
          r: Math.round(fg.r * a + bg.r * (1 - a)),
          g: Math.round(fg.g * a + bg.g * (1 - a)),
          b: Math.round(fg.b * a + bg.b * (1 - a)),
          a: 1,
        };
      }
      function effectiveBg(el) {
        const layers = [];
        let cur = el;
        while (cur && cur !== document.documentElement) {
          const c = parseColor(getComputedStyle(cur).backgroundColor);
          if (c && c.a > 0) layers.unshift(c);
          cur = cur.parentElement;
        }
        let base = parseColor(getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
        for (const layer of layers) {
          base = blend(layer, base);
        }
        return base;
      }
      function srgbToLin(c) {
        c /= 255;
        return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
      }
      function lum(rgb) {
        return 0.2126 * srgbToLin(rgb.r) + 0.7152 * srgbToLin(rgb.g) + 0.0722 * srgbToLin(rgb.b);
      }
      function contrast(c1, c2) {
        if (!c1 || !c2) return 0;
        const l1 = lum(c1);
        const l2 = lum(c2);
        return +((Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)).toFixed(2);
      }

      const htmlTheme = document.documentElement.getAttribute("data-theme");
      const htmlColorScheme = getComputedStyle(document.documentElement).colorScheme;
      const bodyBg = effectiveBg(document.body);

      const firstCard = document.querySelector('[data-testid="create-project-form"] > div');
      const cardBg = effectiveBg(firstCard);

      const nameInput = document.querySelector('[data-testid="field-name"]');
      const topicTextarea = document.querySelector('[data-testid="field-topic"]');
      const toneSelect = document.querySelector('[data-testid="field-tone"]');
      const durationInput = document.querySelector('[data-testid="field-duration"]');
      const commercialCheckbox = document.querySelector('[data-testid="field-commercial"]');

      const inputStyle = getComputedStyle(nameInput);
      const inputBg = effectiveBg(nameInput);
      const inputText = blend(parseColor(inputStyle.color), inputBg);
      const inputBorder = blend(parseColor(inputStyle.borderColor), cardBg);
      const inputFocusOutline = blend(parseColor(inputStyle.outlineColor), cardBg);

      const phStyle = getComputedStyle(nameInput, "::placeholder");
      const phColor = blend(parseColor(phStyle.color), inputBg);

      const selectStyle = getComputedStyle(toneSelect);
      const selectBg = effectiveBg(toneSelect);
      const selectText = blend(parseColor(selectStyle.color), selectBg);
      const selectBorder = blend(parseColor(selectStyle.borderColor), cardBg);

      const firstLabel = document.querySelector('[data-testid="create-project-form"] label span');
      const labelColor = blend(parseColor(getComputedStyle(firstLabel).color), cardBg);

      const hintEl = Array.from(document.querySelectorAll('[data-testid="create-project-form"] label span')).find(
        (s) => s.textContent.includes("chars"),
      );
      const hintColor = hintEl ? blend(parseColor(getComputedStyle(hintEl).color), cardBg) : null;

      const errEl = document.querySelector('[data-testid="error-name"]');
      const errColor = blend(parseColor(getComputedStyle(errEl).color), cardBg);

      const allButtons = Array.from(document.querySelectorAll('[data-testid="create-project-form"] button'));
      const selectedPill = allButtons.find((b) => b.textContent.trim() === "300s");
      const unselectedPill = allButtons.find((b) => b.textContent.trim() === "60s");
      const submitBtn = document.querySelector('[data-testid="create-project-submit"]');
      const cancelBtn = allButtons.find((b) => b.textContent.trim() === "Cancel");

      const selPillBg = effectiveBg(selectedPill);
      const selPillText = blend(parseColor(getComputedStyle(selectedPill).color), selPillBg);
      const selPillBorder = blend(parseColor(getComputedStyle(selectedPill).borderColor), cardBg);

      const unselPillBg = effectiveBg(unselectedPill);
      const unselPillText = blend(parseColor(getComputedStyle(unselectedPill).color), unselPillBg);
      const unselPillBorder = blend(parseColor(getComputedStyle(unselectedPill).borderColor), cardBg);

      const submitBg = effectiveBg(submitBtn);
      const submitText = blend(parseColor(getComputedStyle(submitBtn).color), submitBg);

      const cancelBg = effectiveBg(cancelBtn);
      const cancelText = blend(parseColor(getComputedStyle(cancelBtn).color), cancelBg);

      const axeResults = await window.axe.run('[data-testid="create-project-form"]');

      return {
        htmlTheme,
        htmlColorScheme,
        inputColorScheme: inputStyle.colorScheme,
        selectColorScheme: selectStyle.colorScheme,
        checkboxColorScheme: getComputedStyle(commercialCheckbox).colorScheme,
        colors: {
          bodyBg: getComputedStyle(document.body).backgroundColor,
          cardBg: getComputedStyle(firstCard).backgroundColor,
          inputBg: inputStyle.backgroundColor,
          inputText: inputStyle.color,
          placeholder: phStyle.color,
          label: getComputedStyle(firstLabel).color,
          hint: hintEl ? getComputedStyle(hintEl).color : null,
          error: getComputedStyle(errEl).color,
        },
        contrasts: {
          labelVsCard: contrast(labelColor, cardBg),
          hintVsCard: contrast(hintColor, cardBg),
          inputTextVsBg: contrast(inputText, inputBg),
          placeholderVsBg: contrast(phColor, inputBg),
          selectTextVsBg: contrast(selectText, selectBg),
          errorVsCard: contrast(errColor, cardBg),
          selectedPillTextVsBg: contrast(selPillText, selPillBg),
          selectedPillBorderVsCard: contrast(selPillBorder, cardBg),
          unselectedPillTextVsBg: contrast(unselPillText, unselPillBg),
          inputBorderVsCard: contrast(inputBorder, cardBg),
          selectBorderVsCard: contrast(selectBorder, cardBg),
          focusOutlineVsCard: contrast(inputFocusOutline, cardBg),
          submitTextVsBg: contrast(submitText, submitBg),
          cancelTextVsBg: contrast(cancelText, cancelBg),
        },
        axeViolations: axeResults.violations.map((v) => ({
          id: v.id,
          impact: v.impact,
          nodes: v.nodes.length,
        })),
      };
    });

    // Check disabled state contrast on both input and submit button
    const disabledMetrics = await page.evaluate(() => {
      function parseColor(str) {
        const m = str && str.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([0-9.]+))?\)/);
        if (!m) return null;
        return { r: +m[1], g: +m[2], b: +m[3], a: m[4] !== undefined ? +m[4] : 1 };
      }
      function srgbToLin(c) {
        c /= 255;
        return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
      }
      function lum(rgb) {
        return 0.2126 * srgbToLin(rgb.r) + 0.7152 * srgbToLin(rgb.g) + 0.0722 * srgbToLin(rgb.b);
      }
      function contrast(c1, c2) {
        if (!c1 || !c2) return 0;
        const l1 = lum(c1);
        const l2 = lum(c2);
        return +((Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)).toFixed(2);
      }
      const btn = document.querySelector('[data-testid="create-project-submit"]');
      const inp = document.querySelector('[data-testid="field-name"]');
      btn.style.transition = "none";
      inp.style.transition = "none";
      btn.disabled = true;
      inp.disabled = true;
      btn.offsetHeight;
      inp.offsetHeight;
      const btnStyle = getComputedStyle(btn);
      const inpStyle = getComputedStyle(inp);
      const btnContrast = contrast(parseColor(btnStyle.color), parseColor(btnStyle.backgroundColor));
      const inpContrast = contrast(parseColor(inpStyle.color), parseColor(inpStyle.backgroundColor));
      btn.disabled = false;
      inp.disabled = false;
      btn.style.transition = "";
      inp.style.transition = "";
      return {
        disabledButtonContrast: btnContrast,
        disabledInputContrast: inpContrast,
      };
    });
    metrics.contrasts.disabledButtonTextVsBg = disabledMetrics.disabledButtonContrast;
    metrics.contrasts.disabledInputTextVsBg = disabledMetrics.disabledInputContrast;

    report[theme] = metrics;

    const c = metrics.contrasts;
    const check = (name, actual, min) => {
      if (actual < min) {
        failures.push(`[${theme}] ${name}: ${actual}:1 < required ${min}:1`);
      }
    };

    if (metrics.htmlTheme !== theme) failures.push(`[${theme}] html[data-theme] is ${metrics.htmlTheme}`);
    if (metrics.htmlColorScheme !== theme) failures.push(`[${theme}] html color-scheme is ${metrics.htmlColorScheme}`);
    if (metrics.inputColorScheme !== theme) failures.push(`[${theme}] input color-scheme is ${metrics.inputColorScheme}`);
    if (metrics.selectColorScheme !== theme) failures.push(`[${theme}] select color-scheme is ${metrics.selectColorScheme}`);

    check("labelVsCard", c.labelVsCard, 4.5);
    check("hintVsCard", c.hintVsCard, 4.5);
    check("inputTextVsBg", c.inputTextVsBg, 4.5);
    check("placeholderVsBg", c.placeholderVsBg, 4.5);
    check("selectTextVsBg", c.selectTextVsBg, 4.5);
    check("errorVsCard", c.errorVsCard, 4.5);
    check("selectedPillTextVsBg", c.selectedPillTextVsBg, 4.5);
    check("selectedPillBorderVsCard", c.selectedPillBorderVsCard, 3.0);
    check("unselectedPillTextVsBg", c.unselectedPillTextVsBg, 4.5);
    check("inputBorderVsCard", c.inputBorderVsCard, 3.0);
    check("selectBorderVsCard", c.selectBorderVsCard, 3.0);
    check("focusOutlineVsCard", c.focusOutlineVsCard, 3.0);
    check("submitTextVsBg", c.submitTextVsBg, 4.5);
    check("cancelTextVsBg", c.cancelTextVsBg, 4.5);
    check("disabledButtonTextVsBg", c.disabledButtonTextVsBg, 4.5);
    check("disabledInputTextVsBg", c.disabledInputTextVsBg, 4.5);

    if (metrics.axeViolations.length > 0) {
      failures.push(`[${theme}] axe violations: ${JSON.stringify(metrics.axeViolations)}`);
    }

    await context.close();
  }

  console.log(JSON.stringify({ report, failures }, null, 2));
  await browser.close();
  server.close();
  if (failures.length > 0) {
    process.exit(1);
  }
})();
