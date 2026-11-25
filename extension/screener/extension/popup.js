const SERVER = "http://127.0.0.1:8776";

document.addEventListener("DOMContentLoaded", async () => {
  const serverBadge = document.getElementById("serverBadge");
  const serverStatus = document.getElementById("serverStatus");
  const tabStatus = document.getElementById("tabStatus");
  const lastCapture = document.getElementById("lastCapture");
  const logBox = document.getElementById("logBox");

  const extractActiveBtn = document.getElementById("extractActiveBtn");
  const queueFnoBtn = document.getElementById("queueFnoBtn");
  const queueEarningsBtn = document.getElementById("queueEarningsBtn");

  function log(msg) {
    const time = new Date().toLocaleTimeString();
    logBox.textContent = `[${time}] ${msg}`;
  }

  // 1. Check Server Connection
  async function checkServer() {
    try {
      const res = await fetch(`${SERVER}/health`, { method: "GET" });
      if (res.ok) {
        const data = await res.json();
        serverBadge.textContent = "ONLINE";
        serverBadge.className = "badge";
        serverStatus.textContent = `Online (Pending: ${data.pending}, Results: ${data.results})`;
        return true;
      }
    } catch (_) {
      // offline
    }
    serverBadge.textContent = "OFFLINE";
    serverBadge.className = "badge badge-offline";
    serverStatus.textContent = "Offline (Run extension server)";
    return false;
  }

  // 2. Check Pinned / Active Screener.in Tab
  async function checkTabs() {
    try {
      const tabs = await chrome.tabs.query({});
      const screenerTab = tabs.find(t => t.url && t.url.includes("screener.in"));
      if (screenerTab) {
        let label = "Screener Tab Open";
        if (screenerTab.url.includes("/company/")) {
          const sym = screenerTab.url.split("/company/")[1].split("/")[0].toUpperCase();
          label = `Company: ${sym}`;
        } else if (screenerTab.url.includes("/screen/")) {
          label = "Screen Page";
        }
        tabStatus.textContent = label;
        tabStatus.style.color = "#58a6ff";
        return screenerTab;
      }
    } catch (_) {}
    tabStatus.textContent = "No Screener Tab";
    tabStatus.style.color = "#da3633";
    return null;
  }

  // 3. Load Storage Info
  async function loadStorage() {
    try {
      const data = await chrome.storage.local.get(["lastCaptureTime", "lastCaptureTitle"]);
      if (data.lastCaptureTime) {
        const dt = new Date(data.lastCaptureTime);
        lastCapture.textContent = `${dt.toLocaleTimeString()} (${data.lastCaptureTitle || "Done"})`;
      }
    } catch (_) {}
  }

  await checkServer();
  const activeTab = await checkTabs();
  await loadStorage();

  // 4. Extract Active Tab Button
  extractActiveBtn.addEventListener("click", async () => {
    extractActiveBtn.disabled = true;
    extractActiveBtn.textContent = "Extracting...";
    log("Requesting capture from active tab...");

    try {
      const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
      let targetTab = tabs[0];
      if (!targetTab || !targetTab.url || !targetTab.url.includes("screener.in")) {
        // Fall back to any open screener tab
        const allTabs = await chrome.tabs.query({});
        targetTab = allTabs.find(t => t.url && t.url.includes("screener.in"));
      }

      if (!targetTab) {
        log("Error: Open any screener.in company or screen page first.");
        extractActiveBtn.disabled = false;
        extractActiveBtn.textContent = "⚡ Extract Active Screener Tab";
        return;
      }

      const isCompany = targetTab.url.includes("/company/");
      const sym = isCompany ? targetTab.url.split("/company/")[1].split("/")[0].toUpperCase() : null;
      const jobId = `manual_${Date.now()}`;

      const job = {
        id: jobId,
        job_type: isCompany ? "company" : "screen",
        symbol: sym,
        query: isCompany ? null : targetTab.url,
      };

      // Queue job to server
      const postRes = await fetch(`${SERVER}/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job }),
      });

      if (postRes.ok) {
        log(`Queued job ${jobId}. Service worker extracting...`);
        chrome.storage.local.set({
          lastCaptureTime: new Date().toISOString(),
          lastCaptureTitle: sym || "Custom Screen",
        });
        setTimeout(async () => {
          await checkServer();
          await loadStorage();
          extractActiveBtn.disabled = false;
          extractActiveBtn.textContent = "⚡ Extract Active Screener Tab";
          log(`Capture queued successfully! Check AI-Trading runtime.`);
        }, 1500);
      } else {
        log(`Server rejected job: HTTP ${postRes.status}`);
        extractActiveBtn.disabled = false;
        extractActiveBtn.textContent = "⚡ Extract Active Screener Tab";
      }
    } catch (err) {
      log(`Failed: ${err.message}`);
      extractActiveBtn.disabled = false;
      extractActiveBtn.textContent = "⚡ Extract Active Screener Tab";
    }
  });

  // 5. Queue F&O Leaders Screen
  queueFnoBtn.addEventListener("click", async () => {
    queueFnoBtn.disabled = true;
    log("Queueing F&O Leaders Quality Screen...");
    try {
      const jobId = `fno_leaders_${Date.now()}`;
      const query = "Market Capitalization > 25000 AND Return on capital employed > 15 AND Debt to equity < 1.0 AND Return on equity > 12 AND Price to Earning < 60";
      const res = await fetch(`${SERVER}/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job: { id: jobId, job_type: "screen", screen_name: "fno_leaders_quality", query },
        }),
      });
      if (res.ok) {
        log("F&O Leaders screen queued. Extension will navigate tab.");
      } else {
        log(`Server error: HTTP ${res.status}`);
      }
    } catch (e) {
      log(`Error: ${e.message}`);
    } finally {
      setTimeout(() => { queueFnoBtn.disabled = false; }, 2000);
    }
  });

  // 6. Queue Earnings Acceleration Screen
  queueEarningsBtn.addEventListener("click", async () => {
    queueEarningsBtn.disabled = true;
    log("Queueing Earnings Acceleration Screen...");
    try {
      const jobId = `fno_earnings_${Date.now()}`;
      const query = "Market Capitalization > 20000 AND YOY Quarterly sales growth > 10 AND YOY Quarterly profit growth > 12 AND Return on capital employed > 12";
      const res = await fetch(`${SERVER}/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job: { id: jobId, job_type: "screen", screen_name: "fno_earnings_momentum", query },
        }),
      });
      if (res.ok) {
        log("Earnings screen queued. Extension will extract candidates.");
      } else {
        log(`Server error: HTTP ${res.status}`);
      }
    } catch (e) {
      log(`Error: ${e.message}`);
    } finally {
      setTimeout(() => { queueEarningsBtn.disabled = false; }, 2000);
    }
  });
});
