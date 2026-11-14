const qs = (id) => document.getElementById(id);

function showToast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  qs("toasts").appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

/* ---------- Navigation ---------- */

function initNav() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
      btn.classList.add("active");
      qs(`view-${btn.dataset.view}`).classList.add("active");
    });
  });
  qs("brand").addEventListener("click", () => {
    document.querySelector('.nav-item[data-view="search"]').click();
    if (window.resetSearch) window.resetSearch(); // the logo also clears whatever was searched
    document.querySelector(".content").scrollTop = 0;
  });
}

/* ---------- Helpers ---------- */

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

/* ---------- Queue ---------- */

const jobs = new Map();
const jobOrder = [];
const expanded = new Set();

function upsertJob(job) {
  if (!jobs.has(job.id)) jobOrder.unshift(job.id);
  jobs.set(job.id, job);
  renderJobs();
}

function appendLog(jobId, line) {
  const job = jobs.get(jobId);
  if (!job) return;
  job.log.push(line);
  renderJobs();
}

function updateProgress(jobId, bytes, speedMbps, percent) {
  const job = jobs.get(jobId);
  if (!job) return;
  job.bytes_downloaded = bytes;
  job.speed_mbps = speedMbps;
  job.percent = percent;
  renderJobs();
}

function formatBytes(bytes) {
  if (!bytes) return "0 MB";
  return (bytes / 1_000_000).toFixed(1) + " MB";
}

function renderJobs() {
  const list = qs("job-list");
  qs("job-empty").hidden = jobOrder.length > 0;
  list.innerHTML = "";
  for (const id of jobOrder) {
    const job = jobs.get(id);
    if (!job) continue;
    const row = document.createElement("div");
    row.className = "job-row" + (expanded.has(id) ? " expanded" : "");

    let progressHtml = "";
    if (job.status === "running") {
      const pct = job.percent;
      const barClass = pct == null ? "progress-fill indeterminate" : "progress-fill";
      const barStyle = pct == null ? "" : `style="width:${pct.toFixed(1)}%"`;
      progressHtml = `
        <div class="job-progress-row">
          <div class="progress-track"><div class="${barClass}" ${barStyle}></div></div>
          <span class="job-progress-stats">${formatBytes(job.bytes_downloaded)}${pct != null ? ` · ${pct.toFixed(0)}%` : ""} · ${(job.speed_mbps || 0).toFixed(1)} MB/s</span>
        </div>
      `;
    }

    row.innerHTML = `
      <div class="job-row-top">
        <span class="pill pill-${job.status}">${job.status}</span>
        <span class="job-label" title="${escapeHtml(job.label)}">${escapeHtml(job.label)}</span>
        <span class="job-time">${formatTime(job.created_at)}</span>
        ${job.redownloadable ? `<button type="button" class="btn btn-outline btn-small" data-again="${job.id}">${job.status === "removed" ? "Download again" : "Retry"}</button>` : ""}
        ${job.cancelable ? `<button type="button" class="icon-btn danger" data-cancel="${job.id}" title="${job.status === "queued" ? "Remove from queue" : "Stop download"}">${CANCEL_ICON}</button>` : ""}
        ${job.removable ? `<button type="button" class="icon-btn danger" data-delete="${job.id}" title="Delete files">${TRASH_ICON}</button>` : ""}
        <button class="job-toggle">${expanded.has(id) ? "hide log" : "show log"}</button>
      </div>
      ${progressHtml}
      <div class="job-log">${escapeHtml(job.log.slice(-400).join("\n"))}</div>
    `;
    row.querySelector(".job-toggle").addEventListener("click", () => {
      if (expanded.has(id)) expanded.delete(id);
      else expanded.add(id);
      renderJobs();
    });
    row.querySelector("[data-delete]")?.addEventListener("click", () => confirmDelete(job));
    row.querySelector("[data-cancel]")?.addEventListener("click", () => cancelJob(job));
    row.querySelector("[data-again]")?.addEventListener("click", (e) => downloadAgain(job, e.currentTarget));
    list.appendChild(row);
  }
  const active = [...jobs.values()].filter((j) => j.status === "queued" || j.status === "running").length;
  const badge = qs("queue-count");
  badge.hidden = active === 0;
  badge.textContent = String(active);
}

const TRASH_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a1 1 0 001 1h8a1 1 0 001-1l1-12M9 7V4h6v3"/></svg>';

const CANCEL_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';

/* Asks before deleting. Resolves true if the user confirms. */
function askConfirm({ title, message, confirmLabel }) {
  return new Promise((resolve) => {
    const dlg = qs("confirm-dialog");
    qs("confirm-title").textContent = title;
    qs("confirm-message").textContent = message;
    qs("confirm-ok").textContent = confirmLabel;
    const done = (value) => {
      dlg.close();
      qs("confirm-ok").onclick = qs("confirm-cancel").onclick = null;
      dlg.onclose = null;
      resolve(value);
    };
    qs("confirm-ok").onclick = () => done(true);
    qs("confirm-cancel").onclick = () => done(false);
    dlg.onclose = () => done(false);
    dlg.showModal();
  });
}

async function confirmDelete(job) {
  let status;
  try {
    status = await api(`/api/jobs/${job.id}/files`);
  } catch (err) {
    showToast(err.message, "bad");
    return;
  }
  const n = status.existing;
  const gone = status.missing ? ` ${status.missing} ${status.missing === 1 ? "file is" : "files are"} already gone.` : "";
  const message = n
    ? `This deletes ${n} ${n === 1 ? "file" : "files"} from your library.${gone} You can download it again afterwards.`
    : "The files for this download are already gone from your library, so nothing will be deleted. You can still download it again afterwards.";
  if (!(await askConfirm({ title: `Delete ${job.label}?`, message, confirmLabel: "Delete" }))) return;
  try {
    const res = await api(`/api/jobs/${job.id}/delete`, { method: "POST" });
    showToast(res.deleted ? `Deleted ${res.deleted} ${res.deleted === 1 ? "file" : "files"}` : "Nothing to delete. Ready to download again.", "ok");
  } catch (err) {
    showToast(err.message, "bad");
  }
}

async function cancelJob(job) {
  // A queued job hasn't touched anything yet, so it goes straight away (Retry undoes it).
  if (job.status === "running") {
    const ok = await askConfirm({
      title: `Stop ${job.label}?`,
      message: "What it has downloaded so far is discarded and the next item in the queue starts. You can queue it again afterwards.",
      confirmLabel: "Stop download",
    });
    if (!ok) return;
  }
  try {
    await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
  } catch (err) {
    showToast(err.message, "bad");
  }
}

async function downloadAgain(job, btn) {
  btn.disabled = true;
  try {
    await api(`/api/jobs/${job.id}/redownload`, { method: "POST" });
  } catch (err) {
    btn.disabled = false;
    showToast(err.message, "bad");
  }
}

function formatTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (_) {
    return "";
  }
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws`);

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "job_list") {
      jobs.clear();
      jobOrder.length = 0;
      for (const job of msg.jobs) {
        jobs.set(job.id, job);
        jobOrder.push(job.id);
      }
      renderJobs();
    } else if (msg.type === "job_removed") {
      jobs.delete(msg.job_id);
      const i = jobOrder.indexOf(msg.job_id);
      if (i >= 0) jobOrder.splice(i, 1);
      renderJobs();
    } else if (msg.type === "job_update") {
      upsertJob(msg.job);
    } else if (msg.type === "job_log") {
      appendLog(msg.job_id, msg.line);
    } else if (msg.type === "job_progress") {
      updateProgress(msg.job_id, msg.bytes, msg.speed_mbps, msg.percent);
    }
  };

  ws.onclose = () => setTimeout(connectWebSocket, 2000);
}

/* ---------- Settings ---------- */

async function loadSettings() {
  const s = await api("/api/settings");

  qs("nav-enabled").checked = s.navidrome.enabled;
  qs("nav-url").value = s.navidrome.url;
  qs("nav-username").value = s.navidrome.username;

  qs("qobuz-enabled").checked = s.qobuz.enabled;
  qs("qobuz-use-token").checked = s.qobuz.use_auth_token;
  qs("qobuz-email").value = s.qobuz.email_or_userid;
  qs("qobuz-quality").value = String(s.qobuz.quality);

  qs("tidal-enabled").checked = s.tidal.enabled;
  qs("tidal-quality").value = String(s.tidal.quality);

  qs("deezer-enabled").checked = s.deezer.enabled;
  qs("deezer-quality").value = String(s.deezer.quality);

  qs("soundcloud-enabled").checked = s.soundcloud.enabled;
  qs("soundcloud-app-version").value = s.soundcloud.app_version;

  qs("youtube-enabled").checked = s.youtube.enabled;
  qs("youtube-fallback").checked = s.youtube.fallback;
  qs("youtube-cookies").placeholder = s.youtube.cookies_set ? "(saved, paste to replace)" : "# Netscape HTTP Cookie File";
  window.youtubeEnabled = s.youtube.enabled;

  qs("spotify-enabled").checked = s.spotify.enabled;
  qs("spotify-client-id").value = s.spotify.client_id;
  qs("spotify-client-secret").placeholder = s.spotify.client_secret_set ? "(saved, paste to replace)" : "";
  window.spotifyConfigured = !!(s.spotify.client_id && s.spotify.client_secret_set);
  window.spotifyEnabled = s.spotify.enabled;
  if (window.syncSpotifyToggle) window.syncSpotifyToggle();

  qs("conv-enabled").checked = s.conversion.enabled;
  qs("conv-codec").value = s.conversion.codec;
  qs("conv-sampling-rate").value = s.conversion.sampling_rate;
  qs("conv-bit-depth").value = s.conversion.bit_depth;
  qs("conv-lossy-bitrate").value = s.conversion.lossy_bitrate;

  qs("tagging-enabled").checked = s.tagging.enabled;
  qs("tagging-tier").value = s.tagging.navidrome_version_tier;

  qs("fp-folder-format").value = s.filepaths.folder_format;
  qs("fp-track-format").value = s.filepaths.track_format;
  qs("fp-restrict-chars").checked = s.filepaths.restrict_characters;

  qs("dl-subfolder").value = s.downloads.subfolder;
  qs("dl-absolute-path").value = s.downloads.absolute_path || "";
}

function collectSettings() {
  return {
    tagging: {
      enabled: qs("tagging-enabled").checked,
      navidrome_version_tier: qs("tagging-tier").value,
    },
    navidrome: {
      enabled: qs("nav-enabled").checked,
      url: qs("nav-url").value.trim(),
      username: qs("nav-username").value.trim(),
      password: qs("nav-password").value,
    },
    qobuz: {
      enabled: qs("qobuz-enabled").checked,
      use_auth_token: qs("qobuz-use-token").checked,
      email_or_userid: qs("qobuz-email").value.trim(),
      password_or_token: qs("qobuz-password").value,
      quality: Number(qs("qobuz-quality").value),
    },
    tidal: {
      enabled: qs("tidal-enabled").checked,
      user_id: "",
      country_code: "",
      access_token: "",
      refresh_token: "",
      token_expiry: "",
      quality: Number(qs("tidal-quality").value),
    },
    deezer: {
      enabled: qs("deezer-enabled").checked,
      arl: qs("deezer-arl").value,
      quality: Number(qs("deezer-quality").value),
    },
    soundcloud: {
      enabled: qs("soundcloud-enabled").checked,
      client_id: qs("soundcloud-client-id").value,
      app_version: qs("soundcloud-app-version").value.trim(),
    },
    youtube: {
      enabled: qs("youtube-enabled").checked,
      fallback: qs("youtube-fallback").checked,
      cookies: qs("youtube-cookies").value,
    },
    spotify: {
      enabled: qs("spotify-enabled").checked,
      client_id: qs("spotify-client-id").value.trim(),
      client_secret: qs("spotify-client-secret").value,
    },
    conversion: {
      enabled: qs("conv-enabled").checked,
      codec: qs("conv-codec").value,
      sampling_rate: Number(qs("conv-sampling-rate").value),
      bit_depth: Number(qs("conv-bit-depth").value),
      lossy_bitrate: Number(qs("conv-lossy-bitrate").value),
    },
    filepaths: {
      folder_format: qs("fp-folder-format").value,
      track_format: qs("fp-track-format").value,
      restrict_characters: qs("fp-restrict-chars").checked,
    },
    downloads: {
      subfolder: qs("dl-subfolder").value.trim(),
      absolute_path: qs("dl-absolute-path").value.trim(),
    },
  };
}

function setNavDot(state) {
  const dot = qs("nav-status-dot");
  dot.classList.remove("ok", "bad");
  if (state) dot.classList.add(state);
}

function applyQobuzTokenMode() {
  const useToken = qs("qobuz-use-token").checked;
  qs("qobuz-id-label").textContent = useToken ? "User ID" : "Email or user ID";
  qs("qobuz-pwd-label").textContent = useToken ? "Auth token" : "Password";
  qs("qobuz-pwd-hint").textContent = useToken
    ? "From play.qobuz.com's user/login response, see the steps below"
    : "Your account password, hashed before it's sent";
  qs("qobuz-token-help").hidden = !useToken;
}

function initDesktopFolderPicker() {
  const tauri = window.__TAURI__;
  if (!tauri || !tauri.dialog) return;

  qs("dl-absolute-row").hidden = false;

  qs("dl-browse-btn").addEventListener("click", async () => {
    try {
      const picked = await tauri.dialog.open({ directory: true, multiple: false });
      if (picked) qs("dl-absolute-path").value = picked;
    } catch (err) {
      showToast(`Couldn't open folder picker: ${err.message || err}`, "bad");
    }
  });

  qs("dl-clear-btn").addEventListener("click", () => {
    qs("dl-absolute-path").value = "";
  });
}

function initSettings() {
  loadSettings()
    .then(applyQobuzTokenMode)
    .catch((err) => showToast(`Failed to load settings: ${err.message}`, "bad"));

  initDesktopFolderPicker();

  qs("qobuz-use-token").addEventListener("change", applyQobuzTokenMode);

  qs("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const resultEl = qs("settings-save-result");
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify(collectSettings()) });
      window.youtubeEnabled = qs("youtube-enabled").checked;
      qs("youtube-cookies").value = "";
      qs("spotify-client-secret").value = "";
      resultEl.textContent = "Saved";
      resultEl.className = "test-result ok";
      showToast("Settings saved", "ok");
    } catch (err) {
      resultEl.textContent = err.message;
      resultEl.className = "test-result bad";
    }
    setTimeout(() => (resultEl.textContent = ""), 4000);
  });

  qs("nav-test").addEventListener("click", async () => {
    const resultEl = qs("nav-test-result");
    resultEl.textContent = "Testing...";
    resultEl.className = "test-result";
    try {
      const body = {
        enabled: true,
        url: qs("nav-url").value.trim(),
        username: qs("nav-username").value.trim(),
        password: qs("nav-password").value,
      };
      const res = await api("/api/settings/navidrome/test", { method: "POST", body: JSON.stringify(body) });
      resultEl.textContent = res.message;
      resultEl.className = "test-result " + (res.ok ? "ok" : "bad");
      setNavDot(res.ok ? "ok" : "bad");
    } catch (err) {
      resultEl.textContent = err.message;
      resultEl.className = "test-result bad";
      setNavDot("bad");
    }
  });
}

initNav();
initSettings();
connectWebSocket();
