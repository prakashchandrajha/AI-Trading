const SERVER = "http://127.0.0.1:8776";
const SCREENER = "https://www.screener.in";
const POLL_MS = 2000;

let busy = false;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function postJson(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    console.error(`[Extension Bridge] HTTP Error ${res.status} on ${url}`);
    throw new Error(`HTTP ${res.status}`);
  }
}

function buildUrl(job) {
  if (job.job_type === "company") {
    return `${SCREENER}/company/${encodeURIComponent(job.symbol)}/consolidated/`;
  }
  const query = encodeURIComponent(job.query || "");
  return `${SCREENER}/screen/raw/?query=${query}`;
}

function extractScreenerPage(job) {
  function clean(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  const tables = Array.from(document.querySelectorAll("table")).map((table, index) => {
    const headers = Array.from(table.querySelectorAll("thead th")).map((th) => clean(th.innerText));
    const fallbackHeaders = Array.from(table.querySelectorAll("tr:first-child th, tr:first-child td")).map((cell) => clean(cell.innerText));
    const finalHeaders = headers.length ? headers : fallbackHeaders;
    const bodyRows = Array.from(table.querySelectorAll("tbody tr"));
    const rows = bodyRows.map((tr) => {
      const cells = Array.from(tr.querySelectorAll("td, th")).map((td) => clean(td.innerText));
      const row = {};
      cells.forEach((value, cellIndex) => {
        const key = finalHeaders[cellIndex] || `col_${cellIndex + 1}`;
        row[key] = value;
      });
      const firstLink = tr.querySelector("a[href*='/company/']");
      if (firstLink) {
        row._href = firstLink.getAttribute("href");
      }
      return row;
    }).filter((row) => Object.values(row).some(Boolean));
    const heading = clean(table.closest("section")?.querySelector("h2, h3")?.innerText || "");
    return { index, heading, headers: finalHeaders, rows };
  });

  const ratios = {};
  const ratioItems = Array.from(document.querySelectorAll("#top-ratios li, .company-ratios li"));
  ratioItems.forEach((li) => {
    const text = clean(li.innerText);
    // Format is usually "Name ₹ Value" or "Name Value %"
    // Split by first newline or just find the name
    const nameElem = li.querySelector(".name");
    const valElem = li.querySelector(".value");
    if (nameElem && valElem) {
        ratios[clean(nameElem.innerText)] = clean(valElem.innerText);
    } else {
        // Fallback
        const parts = text.split(" ");
        if (parts.length > 1) {
            ratios[parts[0]] = parts.slice(1).join(" ");
        }
    }
  });

  const announcements = [];
  document.querySelectorAll(".announcements li, #documents li, .announcement, .alert").forEach((el) => {
    const text = clean(el.innerText);
    if (text && !announcements.includes(text)) announcements.push(text);
  });

  return {
    job_id: job.id,
    job_type: job.job_type,
    url: location.href,
    title: clean(document.querySelector("h1")?.innerText || document.title),
    captured_at: new Date().toISOString(),
    capture_mode: "operator_passive",
    adapter_version: "screener-passive-v1",
    tables,
    ratios,
    announcements: announcements.slice(0, 10),
    raw_text: clean(document.body.innerText).slice(0, 20000),
    error: null,
  };
}

async function waitForTabLoad(tabId, timeoutMs = 15000) {
  await sleep(1000);
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    let t = await chrome.tabs.get(tabId);
    if (t.status === 'complete') {
      await sleep(500);
      return true;
    }
    await sleep(250);
  }
  throw new Error("Tab load timeout");
}

async function runJob(job) {
  const url = buildUrl(job);
  try {
    // Hybrid capture: finds any open Screener tab and navigates it to the
    // requested stock/screen URL. Never creates new tabs.
    const expected = new URL(url);
    const tabs = await chrome.tabs.query({});

    // First try: exact URL match
    let targetTab = tabs.find((tab) => {
      if (!tab.url) return false;
      try {
        const current = new URL(tab.url);
        return current.origin === expected.origin
          && current.pathname.replace(/\/$/, "") === expected.pathname.replace(/\/$/, "");
      } catch (_) {
        return false;
      }
    });

    // Second try: any open screener.in tab — navigate it to the requested URL
    if (!targetTab) {
      targetTab = tabs.find((tab) => tab.url && tab.url.includes("screener.in"));
      if (targetTab && targetTab.id) {
        console.log(`[Screener Bridge] Navigating pinned tab ${targetTab.id} to ${url}...`);
        await chrome.tabs.update(targetTab.id, { url: url });
        await waitForTabLoad(targetTab.id, 20000);
      }
    }

    if (!targetTab || !targetTab.id) {
      throw new Error("PIN_TAB_REQUIRED: pin any screener.in tab in Chrome for pre-market lookups");
    }
    console.log(`[Screener Bridge] Passively capturing job ${job.id} from tab ${targetTab.id}`);
    
    // Add timeout to executeScript
    const executePromise = chrome.scripting.executeScript({
      target: { tabId: targetTab.id },
      func: extractScreenerPage,
      args: [job],
    });
    
    const timeoutPromise = new Promise((_, reject) => setTimeout(() => reject(new Error("Timeout during executeScript")), 15000));
    
    const [result] = await Promise.race([executePromise, timeoutPromise]);
    console.log(`[Screener Bridge] Script execution complete, posting result`);
    
    await postJson(`${SERVER}/jobs/${job.id}/result`, { result: result.result });
    console.log(`[Screener Bridge] Result posted successfully`);
  } catch (error) {
    console.error(`[Screener Bridge] Job error: ${error.message}`);
    try {
      await postJson(`${SERVER}/jobs/${job.id}/result`, {
        result: {
          job_id: job.id,
          job_type: job.job_type,
          url,
          captured_at: new Date().toISOString(),
          capture_mode: "operator_passive",
          adapter_version: "screener-passive-v1",
          tables: [],
          ratios: {},
          announcements: [],
          raw_text: "",
          error: String(error),
        },
      });
    } catch (e) {
      console.error(`[Screener Bridge] Failed to post error result: ${e.message}`);
    }
  } finally {
    // Keep the user's Screener tab alive for session continuity and future jobs.
  }
}
async function poll() {
  if (busy) return;
  try {
    const response = await fetch(`${SERVER}/jobs/next`);
    if (response.status !== 200) return;
    const payload = await response.json();
    if (!payload.job) return;
    if (payload.job.job_type === "reload_extension" || payload.job.type === "reload_extension") {
      console.log("[Screener Bridge] Reloading extension via command from UI!");
      try { await postJson(`${SERVER}/jobs/${payload.job.id}/result`, { result: { status: "reloaded" } }); } catch (e) { console.error("Reload error:", e); }
      setTimeout(() => chrome.runtime.reload(), 500);
      return;
    }
    busy = true;
    await runJob(payload.job);
  } catch (e) {
    if (!e.message || !e.message.includes("Failed to fetch")) {
      console.error("[Extension Bridge] CRITICAL LOGIC ERROR during poll:", e);
    }
  } finally {
    busy = false;
  }
}

setInterval(poll, POLL_MS);
poll();

// Keep-alive heartbeat listener
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "PING" || msg.ping === "keepAlive") {
    sendResponse({ status: "PONG" });
  }
});

console.log("[Screener Bridge] Service Worker Started.");

// Keep service worker alive with alarms
chrome.alarms.create("keepAlive", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "keepAlive") {
    console.log("[Screener Bridge] Alarm woke up Service Worker.");
    poll();
  }
});
