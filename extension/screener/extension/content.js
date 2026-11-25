setInterval(() => {
  try {
    if (chrome.runtime && chrome.runtime.sendMessage) {
      chrome.runtime.sendMessage({ type: "PING" }).catch(() => {});
    }
  } catch (e) {
    // Ignore context invalidated errors
  }
}, 10000);
