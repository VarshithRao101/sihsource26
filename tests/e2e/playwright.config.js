// tests/e2e/playwright.config.js
//
// The end-to-end walkthrough of the console. This is a DEV dependency and lives
// outside modules/05_frontend on purpose: the shipped frontend still has zero
// runtime dependencies and no build step, and nothing in here changes that.
//
//   cd tests/e2e
//   npm install
//   npm test              headless, the whole journey
//   npm run demo          headed and slowed down, for showing somebody
//
// The backend is started for you if it is not already running. It takes about
// twenty seconds to boot because it warms the solver JIT and loads the ML
// surrogate before serving anything.

const { defineConfig } = require("@playwright/test");
const path = require("path");

const REPO = path.resolve(__dirname, "..", "..");
const PY = path.join(REPO, ".venv", "Scripts", "python.exe");
const BASE = process.env.SIH_BASE_URL || "http://127.0.0.1:8000";

module.exports = defineConfig({
  testDir: __dirname,
  // A real dam break solve on real terrain is the thing under test. It takes
  // as long as it takes; a short timeout here would only mean testing a
  // simulation we did not let finish.
  timeout: 20 * 60 * 1000,
  expect: { timeout: 20 * 1000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { outputFolder: "report", open: "never" }]],

  use: {
    baseURL: BASE,
    viewport: { width: 1600, height: 950 },
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    trace: "retain-on-failure",
    // WebGL for the Babylon scene. Headless Chromium falls back to SwiftShader,
    // which is slow but real - the test asserts on mesh data, not on pixels.
    launchOptions: { args: ["--use-gl=angle", "--enable-unsafe-swiftshader"] },
  },

  projects: [
    { name: "e2e", use: { browserName: "chromium" } },
    {
      // Headed and slowed down. Same assertions - it is the demo AND the test,
      // so the thing being shown is the thing being checked.
      name: "demo",
      use: {
        browserName: "chromium",
        headless: false,
        launchOptions: {
          slowMo: 400,
          args: ["--use-gl=angle", "--enable-unsafe-swiftshader"],
        },
      },
    },
  ],

  webServer: {
    command: `"${PY}" -m uvicorn modules.04_backend.api:app --port 8000`,
    cwd: REPO,
    url: `${BASE}/health`,
    reuseExistingServer: true,
    timeout: 180 * 1000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
