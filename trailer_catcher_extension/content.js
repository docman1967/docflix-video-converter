// Bridge between the page-context hook (inject.js) and the extension.
//
// ⚠️ Content scripts run in an ISOLATED world — they can see the DOM but not
// the page's own MediaSource/SourceBuffer objects. So the hook has to be
// injected as a real <script> tag to run in the page's context, and we talk to
// it with window.postMessage.
(() => {
  const s = document.createElement("script");
  s.src = chrome.runtime.getURL("inject.js");
  s.onload = () => s.remove();
  (document.head || document.documentElement).appendChild(s);

  const pending = {};
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || !ev.data) return;
    if (ev.data.__tc === "waiting") {
      // Tell the badge we are deliberately holding until it finishes buffering,
      // rather than capturing a truncated clip.
      chrome.runtime.sendMessage({ __tc: "waiting", have: ev.data.have,
                                   total: ev.data.total });
    }
    if (ev.data.__tc === "result" && pending.harvest) {
      pending.harvest(ev.data.res); delete pending.harvest;
    }
    if (ev.data.__tc === "status_result" && pending.status) {
      pending.status(ev.data); delete pending.status;
    }
  });

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg.__tc === "harvest") {
      pending.harvest = reply;
      window.postMessage({ __tc: "harvest", meta: msg.meta || {} }, "*");
      return true;                       // reply asynchronously
    }
    if (msg.__tc === "status") {
      pending.status = reply;
      window.postMessage({ __tc: "status" }, "*");
      return true;
    }
  });
})();
