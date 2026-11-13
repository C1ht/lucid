/* Monitor view: daily-check schedule and the list of monitored artists.
   Depends on helpers from app.js (qs, api, showToast, escapeHtml). */

(() => {
  const esc = escapeHtml;

  const fmt = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never");

  function render(m) {
    qs("mon-enabled").checked = m.enabled;
    qs("mon-time").value = m.time;
    qs("mon-tz").textContent = m.timezone;
    qs("mon-last").textContent = m.last_run_at ? `Last check: ${fmt(m.last_run_at)}` : "";
    qs("mon-empty").hidden = m.artists.length > 0;
    qs("mon-list").innerHTML = m.artists
      .map((a) => {
        const status = a.last_error
          ? `<span class="mon-error">${esc(a.last_error)}</span>`
          : a.last_found
            ? `Last queued: ${esc(a.last_found.title)} (${fmt(a.last_found.at)})`
            : `Checked ${fmt(a.last_checked)}`;
        const sp = m.spotify_available
          ? `<label class="mon-spotify" title="Only monitor releases that Spotify lists for this artist"><input type="checkbox" data-spotify="${esc(a.id)}" ${a.spotify ? "checked" : ""}>Spotify only</label>`
          : "";
        return `<div class="mon-row">
          <div class="mon-info"><div class="mon-name">${esc(a.name)}</div><div class="mon-status">${status}</div></div>
          ${sp}
          <button type="button" class="btn btn-outline btn-small" data-remove="${esc(a.id)}">Remove</button>
        </div>`;
      })
      .join("");
  }

  const load = () => api("/api/monitor").then(render).catch((err) => showToast(err.message, "bad"));

  qs("mon-save").addEventListener("click", () =>
    api("/api/monitor/schedule", {
      method: "PUT",
      body: JSON.stringify({ enabled: qs("mon-enabled").checked, time: qs("mon-time").value }),
    })
      .then((m) => {
        render(m);
        showToast("Saved", "ok");
      })
      .catch((err) => showToast(err.message, "bad"))
  );

  qs("mon-check").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      render(await api("/api/monitor/check", { method: "POST" }));
      showToast("Check finished. Any new releases are in the Queue.", "ok");
    } catch (err) {
      showToast(err.message, "bad");
    }
    btn.disabled = false;
  });

  qs("mon-list").addEventListener("change", (e) => {
    const box = e.target.closest("[data-spotify]");
    if (!box) return;
    api(`/api/monitor/artists/${encodeURIComponent(box.dataset.spotify)}`, {
      method: "PUT",
      body: JSON.stringify({ spotify: box.checked }),
    })
      .then((m) => {
        render(m);
        showToast(box.checked ? "Only Spotify-listed releases will be monitored" : "Monitoring all Qobuz releases", "ok");
      })
      .catch((err) => showToast(err.message, "bad"));
  });

  qs("mon-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-remove]");
    if (!btn) return;
    api(`/api/monitor/artists/${encodeURIComponent(btn.dataset.remove)}`, { method: "DELETE" })
      .then(render)
      .catch((err) => showToast(err.message, "bad"));
  });

  document.addEventListener("monitor-changed", load);
  document.querySelector('.nav-item[data-view="monitor"]').addEventListener("click", load);
  load();
})();
