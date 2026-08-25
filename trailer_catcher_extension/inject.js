// Runs in the PAGE's JavaScript context (not the extension's isolated world).
//
// ⚠️ WHY THIS EXISTS. Capturing the playback URL and re-fetching it does not
// work — Google binds those URLs to the browser session and returns 403 to
// everything else, including Chrome's own download manager (tested 2026-08-25).
//
// But the media itself arrives here in the clear. YouTube hands each DASH
// segment to the browser through Media Source Extensions, and the very last
// thing it does with the bytes is `SourceBuffer.appendBuffer(chunk)`. So we
// keep a copy on the way past. No request is made, no signature is needed, and
// nothing is decrypted — we are simply not throwing away what already arrived.
//
// ⚠️ CONSEQUENCE: we only get what the player actually fetched. The video has
// to buffer through. Fine for a two-minute trailer, useless for a feature.
(() => {
  if (window.__trailerCatcherHooked) return;
  window.__trailerCatcherHooked = true;

  const ENDPOINT = "http://127.0.0.1:5961";
  // buffers: SourceBuffer -> { mime, chunks: [Uint8Array] }
  const buffers = new Map();

  const origAdd = MediaSource.prototype.addSourceBuffer;
  MediaSource.prototype.addSourceBuffer = function (mime) {
    const sb = origAdd.call(this, mime);
    // ⚠️ Track by SourceBuffer identity, not by index. YouTube creates and
    // recreates them as quality changes, and indices shift underneath you.
    buffers.set(sb, { mime: String(mime || ""), chunks: [] });
    return sb;
  };

  // ⚠️ ADS COME DOWN THE SAME PIPE. YouTube plays pre-roll through the same
  // MediaSource and the same SourceBuffers, so without this the captured file
  // is the advert with the trailer welded on after it. The player marks its
  // container with `ad-showing` while an ad is on screen.
  function adPlaying() {
    const p = document.getElementById("movie_player");
    return !!(p && (p.classList.contains("ad-showing") ||
                    p.classList.contains("ad-interrupting")));
  }

  const origAppend = SourceBuffer.prototype.appendBuffer;
  SourceBuffer.prototype.appendBuffer = function (data) {
    try {
      const rec = buffers.get(this);
      if (rec && data && adPlaying()) {
        // Drop anything captured during an ad, and throw away whatever we
        // collected before it — the real video's init segment comes after.
        rec.chunks.length = 0;
        rec.sawAd = true;
        return origAppend.call(this, data);
      }
      if (rec && data) {
        const view = data instanceof ArrayBuffer
          ? new Uint8Array(data)
          : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
        // Copy — the caller may reuse or neuter the underlying buffer.
        rec.chunks.push(new Uint8Array(view));
      }
    } catch (e) { /* never break playback over a capture failure */ }
    return origAppend.call(this, data);
  };

  function held() {
    const out = { video: null, audio: null };
    for (const rec of buffers.values()) {
      const bytes = rec.chunks.reduce((n, c) => n + c.length, 0);
      if (!bytes) continue;
      const slot = rec.mime.startsWith("audio/") ? "audio" : "video";
      // Keep the largest — a quality change leaves an abandoned short buffer.
      if (!out[slot] || bytes > out[slot].bytes) out[slot] = { rec, bytes };
    }
    return out;
  }

  function join(rec) {
    const total = rec.chunks.reduce((n, c) => n + c.length, 0);
    const all = new Uint8Array(total);
    let o = 0;
    for (const c of rec.chunks) { all.set(c, o); o += c.length; }
    return all;
  }

  async function harvest(meta) {
    const h = held();
    if (!h.video) return { ok: false, error: "nothing buffered — let it play" };
    const vid = (location.search.match(/[?&]v=([^&]+)/) || [])[1] || "";
    // ⚠️ Posts straight to the local service rather than passing megabytes back
    // through the extension. http://127.0.0.1 counts as a trustworthy origin,
    // so an https page is allowed to reach it; the service sends CORS headers.
    for (const slot of ["video", "audio"]) {
      if (!h[slot]) continue;
      const r = await fetch(
        `${ENDPOINT}/api/mse?kind=${slot}&vid=${encodeURIComponent(vid)}` +
        `&mime=${encodeURIComponent(h[slot].rec.mime)}`,
        { method: "POST", body: join(h[slot].rec) });
      if (!r.ok) return { ok: false, error: `${slot} upload failed (${r.status})` };
    }
    const done = await fetch(`${ENDPOINT}/api/mse_done`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vid, title: document.title, ...meta }),
    });
    return await done.json().catch(() => ({ ok: false, error: "bad reply" }));
  }

  // ⚠️ HOW MUCH HAVE WE ACTUALLY GOT? The player fetches progressively, so a
  // click made 40s into a 2-minute trailer captures 40 seconds — which is
  // exactly what happened on the first successful catch. Rather than ask the
  // user to judge when buffering is finished, report it and offer to wait.
  function capturedSeconds() {
    const v = document.querySelector("video");
    if (!v || !v.buffered || !v.buffered.length) return 0;
    return v.buffered.end(v.buffered.length - 1);
  }
  function totalSeconds() {
    const v = document.querySelector("video");
    return v && isFinite(v.duration) ? v.duration : 0;
  }

  window.addEventListener("message", async (ev) => {
    if (ev.source !== window || !ev.data || ev.data.__tc !== "harvest") return;
    const meta = ev.data.meta || {};
    let res;
    try {
      const v = document.querySelector("video");
      // ⚠️ RE-READ duration every tick, never cache it. An ad's duration is
      // what you get if you sample once at the start — that is how this locked
      // onto "8/12" and then waited forever for a 12-second video that had
      // already been replaced by the real one.
      if (v) {
        await new Promise((resolve) => {
          let idle = 0;
          const tick = () => {
            if (adPlaying()) {                 // ads don't count toward progress
              window.postMessage({ __tc: "waiting", have: 0, total: 0 }, "*");
              return;
            }
            const have = capturedSeconds(), total = totalSeconds();
            window.postMessage({
              __tc: "waiting",
              have: Math.round(have), total: Math.round(total),
            }, "*");
            if (total && have >= total - 1.5) { stop(); return resolve(); }
            // Give up waiting if nothing progresses for ~30s (paused, stalled).
            if (have === last) { if (++idle > 60) { stop(); return resolve(); } }
            else { idle = 0; last = have; }
          };
          let last = -1;
          const iv = setInterval(tick, 500);
          const onEnd = () => { stop(); resolve(); };
          const stop = () => {
            clearInterval(iv);
            v.removeEventListener("ended", onEnd);
          };
          v.addEventListener("ended", onEnd);
          tick();
        });
      }
      res = await harvest(meta);
      res.captured = Math.round(capturedSeconds());
      res.total = Math.round(total);
    } catch (e) { res = { ok: false, error: e.message }; }
    window.postMessage({ __tc: "result", res }, "*");
  });

  // Let the page report what it is holding, for the badge.
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || !ev.data || ev.data.__tc !== "status") return;
    const h = held();
    window.postMessage({
      __tc: "status_result",
      video: h.video ? h.video.bytes : 0,
      audio: h.audio ? h.audio.bytes : 0,
    }, "*");
  });
})();
