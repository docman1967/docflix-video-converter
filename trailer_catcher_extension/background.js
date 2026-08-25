// Trailer Catcher — Chrome extension, self-built, loaded UNPACKED.
//
// It does exactly one thing: watch the media URLs Chrome requests while a
// YouTube video plays, and POST the latest pair to the local Trailer Catcher
// when the toolbar button is clicked.
//
// It computes NOTHING. Chrome has already done the signature work, the `n`
// transform and the BotGuard attestation by the time these requests go out —
// this just reads the finished URL off the wire. That is why it will not rot
// every time YouTube changes their player internals.
//
// It sends data to 127.0.0.1:5961 and nowhere else. There is no analytics, no
// remote config, no auto-update. Read it; it is short on purpose.

const ENDPOINT = "http://127.0.0.1:5961/api/capture";

// ⚠️ MV3 service workers get evicted when idle, taking any in-memory state with
// them. chrome.storage.session survives that and is cleared on browser restart,
// which is exactly the lifetime we want.
async function remember(tabId, patch) {
  const key = "tab" + tabId;
  const cur = (await chrome.storage.session.get(key))[key] || {};
  await chrome.storage.session.set({ [key]: { ...cur, ...patch } });
}

async function recall(tabId) {
  const key = "tab" + tabId;
  return (await chrome.storage.session.get(key))[key] || {};
}

// ── capture the media URLs as the player requests them ──────────────────────
//
// ⚠️ YouTube serves DASH, so video and audio arrive as SEPARATE requests. Grab
// only one and you get a silent clip with nothing to tell you why. The `mime`
// parameter distinguishes them.
function videoIdOf(url) {
  return (String(url || "").match(/[?&]v=([^&]+)/) || [])[1] || null;
}

chrome.webRequest.onBeforeRequest.addListener(
  async (details) => {
    if (details.tabId < 0) return;
    const url = details.url;
    const mime = decodeURIComponent(
      (url.match(/[?&]mime=([^&]*)/) || [])[1] || ""
    );
    // ⚠️ Not every videoplayback request carries a `mime` param. Fall back to
    // the itag, which always identifies the stream: YouTube's video-only itags
    // are 133-137/160/242-248/298-303/394-401, audio-only are 139-141/171/249-251.
    let kind = mime.startsWith("video/") ? "video"
             : mime.startsWith("audio/") ? "audio" : null;
    if (!kind) {
      const itag = parseInt((url.match(/[?&]itag=(\d+)/) || [])[1] || "0", 10);
      const AUDIO = [139, 140, 141, 171, 249, 250, 251, 256, 258, 327, 338];
      if (itag) kind = AUDIO.includes(itag) ? "audio" : "video";
    }
    if (!kind) return;

    // ⚠️ TAG EACH CAPTURE WITH THE VIDEO IT BELONGS TO, and never wipe.
    // The first version reset the tab's state whenever the URL changed, to stop
    // you downloading the previous video. But YouTube is a single-page app, so
    // that navigation event can arrive AFTER the player has already requested
    // media — the reset raced the capture and destroyed it. Symptom: the badge
    // showed "V" on the search page, then the button said "play" on the video.
    // Validating at click time can't race anything.
    let vid = null;
    try {
      const tab = await chrome.tabs.get(details.tabId);
      vid = videoIdOf(tab.url);
    } catch (e) { /* tab gone; keep the URL anyway */ }

    const patch = kind === "video" ? { video_url: url } : { audio_url: url };
    patch.vid = vid;
    const prev = await recall(details.tabId);
    // A new video invalidates the other stream, or we'd mux across two videos.
    if (vid && prev.vid && prev.vid !== vid) {
      await chrome.storage.session.set({ ["tab" + details.tabId]: patch });
    } else {
      await remember(details.tabId, patch);
    }
    showHeld(details.tabId);
  },
  { urls: ["*://*.googlevideo.com/videoplayback*"] }
);

async function showHeld(tabId) {
  const st = await recall(tabId);
  const txt = (st.video_url ? "V" : "") + (st.audio_url ? "A" : "");
  if (txt) {
    chrome.action.setBadgeText({ tabId, text: txt });
    chrome.action.setBadgeBackgroundColor({ tabId, color: "#3a5a8a" });
  }
}

// ⚠️ There is deliberately NO tabs.onUpdated reset here. Wiping state on URL
// change is what broke the first version — see the capture listener above.
// Staleness is caught at click time by comparing the tagged video id, which
// cannot race a navigation event.

// The page tells us it's holding for the buffer to fill — show progress rather
// than looking frozen while it waits.
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.__tc === "waiting" && sender.tab) {
    // total 0 = an ad is playing; say so rather than showing a bogus target.
    const txt = msg.total ? `${msg.have}/${msg.total}` : "ad";
    flash(sender.tab.id, txt, "#b06000", false);   // no auto-clear while waiting
  }
});

// ⚠️ Track the clear timer so a later badge can cancel an earlier one's. The
// "…" flash used to schedule a wipe 6s out, which then erased the buffering
// progress that replaced it — the badge showed "8/12" and then vanished, and
// it looked like the capture had died when it was still waiting.
const clearTimers = {};
function flash(tabId, text, colour, autoClear = true) {
  if (clearTimers[tabId]) clearTimeout(clearTimers[tabId]);
  chrome.action.setBadgeText({ tabId, text });
  chrome.action.setBadgeBackgroundColor({ tabId, color: colour });
  if (autoClear) {
    clearTimers[tabId] = setTimeout(() => {
      chrome.action.setBadgeText({ tabId, text: "" });
      delete clearTimers[tabId];
    }, 8000);
  }
}

// ── the button ──────────────────────────────────────────────────────────────
chrome.action.onClicked.addListener(async (tab) => {
  // ⚠️ MSE PATH — the only one that works. Ask the page for the media it has
  // already buffered. See inject.js for why re-fetching the URL cannot work.
  flash(tab.id, "…", "#3a5a8a");
  try {
    const res = await chrome.tabs.sendMessage(tab.id, { __tc: "harvest" });
    if (res && res.ok) {
      flash(tab.id, String(res.height || "ok"), "#2e7d32");
      if (res.total && res.captured < res.total - 2) {
        console.warn(`Trailer Catcher: captured ${res.captured}s of ${res.total}s`);
      }
    } else {
      flash(tab.id, "err", "#c62828");
      console.warn("Trailer Catcher:", (res && res.error) || "no reply from page");
    }
  } catch (e) {
    // Almost always: page loaded before the extension, so no content script.
    flash(tab.id, "load", "#b06000");
    console.warn("Trailer Catcher: no content script in this tab — reload the "
                 + "YouTube page after loading/reloading the extension.", e.message);
  }
  return;

  // ── everything below is the retired URL-capture path, kept only as a record ──
  /* eslint-disable no-unreachable */
  const st = await recall(tab.id);
  if (!st.video_url) {
    // The player has to have actually started for any media URL to exist.
    flash(tab.id, "play", "#b06000");
    return;
  }
  // ⚠️ Staleness check, replacing the old destructive reset. If what we hold
  // was captured for a different video than the one on screen, refuse — do not
  // quietly file the previous trailer under this title.
  const nowVid = videoIdOf(tab.url);
  if (nowVid && st.vid && st.vid !== nowVid) {
    flash(tab.id, "old", "#b06000");
    console.warn(`Trailer Catcher: held URLs are for ${st.vid}, page is ${nowVid} — replay this video.`);
    return;
  }
  flash(tab.id, "…", "#3a5a8a");

  // ⚠️ CHROME MUST DO THE FETCHING.
  //
  // Handing the URL to ffmpeg gets a hard 403 — verified with curl, with and
  // without the range params, with Referer/Origin set, from the same machine
  // seconds later. Those URLs carry spc/svpuc/vprv/n/sig: Google binds them to
  // the browser SESSION, not just the IP, and lifting one out is exactly the
  // technique being defended against.
  //
  // chrome.downloads uses the browser's own network context, so the request
  // that goes out is indistinguishable from the player's.
  const stamp = Date.now();
  const jobs = [["video", st.video_url], ["audio", st.audio_url]].filter(([, u]) => u);
  const files = [];
  for (const [kind, url] of jobs) {
    const name = `trailer_catcher/${stamp}.${kind}`;
    try {
      const id = await chrome.downloads.download({
        url, filename: name, conflictAction: "overwrite", saveAs: false,
      });
      files.push({ kind, name, id });
    } catch (e) {
      flash(tab.id, "dl", "#c62828");
      console.warn("Trailer Catcher: chrome.downloads refused —", e.message);
      return;
    }
  }
  // Wait for Chrome to finish (or fail) each one before telling the service.
  const done = await Promise.all(files.map(f => new Promise((resolve) => {
    const check = () => chrome.downloads.search({ id: f.id }, (r) => {
      const d = r && r[0];
      if (!d) return resolve({ ...f, state: "gone" });
      if (d.state === "complete") return resolve({ ...f, state: "complete", path: d.filename });
      if (d.state === "interrupted") return resolve({ ...f, state: "interrupted", why: d.error });
      setTimeout(check, 500);
    });
    check();
  })));
  const bad = done.find(d => d.state !== "complete");
  if (bad) {
    flash(tab.id, "403?", "#c62828");
    console.warn("Trailer Catcher: Chrome could not fetch it either —", bad);
    return;
  }
  console.log("Trailer Catcher: Chrome fetched", done.map(d => d.path));

  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        // Local paths Chrome has already written — the service muxes these
        // rather than fetching anything itself.
        files: done.map(d => ({ kind: d.kind, path: d.path })),
        video_url: st.video_url,
        audio_url: st.audio_url || null,
        page_title: tab.title || st.page_title || "",
        page_url: tab.url || "",
        ua: navigator.userAgent,
      }),
    });
    const j = await res.json().catch(() => ({}));
    if (j.ok) flash(tab.id, String(j.height || "ok"), "#2e7d32");
    else flash(tab.id, "err", "#c62828");
    if (j.error) console.warn("Trailer Catcher:", j.error);
  } catch (e) {
    // Almost always "the local service isn't running".
    flash(tab.id, "off", "#c62828");
    console.warn("Trailer Catcher: local service unreachable —", e.message);
  }
});
