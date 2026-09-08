// Finite BUSY Bar firmware/API health example.
// This uses only the credential-free /api/version endpoint.
const APP_ID = "app.busy.integrations_demo";
const VERSION_URL = "http://10.0.4.20/api/version";
const POLL_MS = 10000;
const RUN_MS = 30000;

let stopped = false;
let inFlight = false;
let pollNumber = 0;

// localStorage is synchronous on the device.  Increment once per run rather
// than once per poll so the flash-backed value does not churn every 10 sec.
const previousRuns = Number(localStorage.getItem("run_count") || "0");
const runNumber = Number.isFinite(previousRuns) ? previousRuns + 1 : 1;
localStorage.setItem("run_count", String(runNumber));

async function pollVersion() {
  if (stopped || inFlight) return;
  inFlight = true;
  try {
    const response = await fetch(VERSION_URL);
    const body = await response.json();
    if (!stopped) {
      // BUSY's embedded Response does not provide browser .ok/.status.
      console.log(APP_ID + " run=" + runNumber + " poll=" + (++pollNumber), body.api_semver);
    }
  } catch (error) {
    if (!stopped) console.error(APP_ID + " version request failed", error);
  } finally {
    inFlight = false;
  }
}

pollVersion();
const intervalId = setInterval(pollVersion, POLL_MS);
setTimeout(function stopDemo() {
  stopped = true;
  clearInterval(intervalId);
  console.log(APP_ID + " stopped after " + RUN_MS + " ms");
}, RUN_MS);
