/* Settings > Library check: scan the downloaded library against the catalogue, then
   pick which unmatched albums/tracks to delete. Depends on app.js helpers
   (qs, api, showToast, escapeHtml, askConfirm). */

(() => {
  const esc = escapeHtml;
  const attr = (s) => esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const sel = new Set();
  let items = [];
  let timer = null;
  let fresh = false; // a check the user just started: open the review dialog when it ends

  const status = (text, cls = "") => {
    const el = qs("lib-status");
    el.textContent = text;
    el.className = `test-result ${cls}`;
  };
  const allPaths = () => items.flatMap((a) => a.tracks.map((t) => t.path));
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  function renderList() {
    qs("lib-list").innerHTML = items.length
      ? items
          .map((a) => {
            const on = a.tracks.filter((t) => sel.has(t.path)).length;
            return `<details class="lib-album">
              <summary>
                <input type="checkbox" data-album="${a.id}" ${on === a.tracks.length ? "checked" : ""} aria-label="Select album">
                <div class="lib-title"><b>${esc(a.album)}</b> · ${esc(a.artist)}<span>${esc(a.reason)}</span></div>
                <span class="lib-count">${on ? `${on}/` : ""}${plural(a.tracks.length, "track")}</span>
              </summary>
              <div class="lib-tracks">${a.tracks
                .map(
                  (t) =>
                    `<label class="lib-track"><input type="checkbox" data-track="${attr(t.path)}" ${sel.has(t.path) ? "checked" : ""}>${esc(t.title)}</label>`
                )
                .join("")}</div>
            </details>`;
          })
          .join("")
      : `<div class="lib-empty">Nothing left to review.</div>`;
    qs("lib-list").querySelectorAll("[data-album]").forEach((box) => {
      const a = items.find((x) => String(x.id) === box.dataset.album);
      const on = a.tracks.filter((t) => sel.has(t.path)).length;
      box.indeterminate = on > 0 && on < a.tracks.length;
    });
    const n = sel.size;
    const total = allPaths().length;
    qs("lib-del-sel").textContent = n ? `Delete selected (${n})` : "Delete selected";
    qs("lib-del-sel").disabled = !n;
    qs("lib-del-all").disabled = !total;
    qs("lib-summary").textContent = `${plural(items.length, "album")} (${plural(total, "file")}) didn't match the catalogue. Tick what to delete; anything unticked stays.`;
  }

  function open(newItems) {
    if (newItems) items = newItems;
    sel.clear();
    renderList();
    const dlg = qs("lib-dialog");
    if (!dlg.open) dlg.showModal();
  }

  async function refresh() {
    const s = await api("/api/library-check");
    items = s.items;
    return s;
  }

  async function poll() {
    clearTimeout(timer);
    let s;
    try {
      s = await api("/api/library-check");
    } catch (err) {
      status(err.message, "bad");
      qs("lib-start").disabled = false;
      return;
    }
    if (s.status === "running") {
      status(s.stage ? `${s.stage}${s.total ? ` (${s.done}/${s.total} artists)` : ""}…` : "Working…");
      qs("lib-start").disabled = true;
      timer = setTimeout(poll, 1200);
      return;
    }
    qs("lib-start").disabled = false;
    if (s.status === "error") {
      status(s.error, "bad");
      return;
    }
    if (s.status === "done") {
      const st = s.stats;
      const who = s.scope ? ` for ${s.scope}` : "";
      qs("lib-review").hidden = !s.items.length;
      if (s.scope && !st.albums) {
        status(`No downloaded albums found${who}.`);
        if (fresh) showToast(`No downloaded albums found${who}.`);
        fresh = false;
        return;
      }
      status(`${st.matched} of ${st.albums} albums match${who}, ${st.unmatched} don't.${st.skipped ? ` ${plural(st.skipped, "file")} skipped.` : ""}${st.unchecked ? ` ${plural(st.unchecked, "artist")} couldn't be checked (a source was unreachable), so none of their albums are listed. Run it again.` : ""}`, st.unmatched ? "" : "ok");
      if (s.items.length && fresh) open(s.items);
      else items = s.items;
      if (fresh && !s.items.length && !st.unchecked) showToast(`Everything downloaded${who} matches the catalogue.`, "ok");
      fresh = false;
    }
  }

  async function begin(artist) {
    try {
      fresh = true;
      status("Starting…");
      await api("/api/library-check", { method: "POST", body: JSON.stringify({ artist: artist || null }) });
      poll();
    } catch (err) {
      fresh = false;
      status(err.message, "bad");
      showToast(err.message, "bad");
    }
  }
  qs("lib-start").addEventListener("click", () => begin());

  // Used by the Monitor and artist pages.
  window.libTools = {
    check: (artist) => {
      showToast(`Checking ${artist} against the catalogue…`);
      return begin(artist);
    },
    async deleteArtist(name) {
      let sum;
      try {
        sum = await api(`/api/library/artist?name=${encodeURIComponent(name)}`);
      } catch (err) {
        showToast(err.message, "bad");
        return false;
      }
      if (!sum.files) {
        showToast(`No downloaded songs by ${name} found.`);
        return false;
      }
      const ok = await askConfirm({
        title: `Delete all songs by ${name}?`,
        message: `${plural(sum.files, "file")} in ${plural(sum.albums, "album")} will be permanently deleted from disk. This can't be undone, but you can download them again later.`,
        confirmLabel: "Delete all",
      });
      if (!ok) return false;
      try {
        const r = await api("/api/library/artist/delete", { method: "POST", body: JSON.stringify({ name }) });
        showToast(`Deleted ${plural(r.deleted, "file")} by ${name}`, "ok");
        return true;
      } catch (err) {
        showToast(err.message, "bad");
        return false;
      }
    },
  };
  qs("lib-review").addEventListener("click", async () => {
    try {
      await refresh();
      open();
    } catch (err) {
      showToast(err.message, "bad");
    }
  });

  qs("lib-list").addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.track !== undefined) {
      t.checked ? sel.add(t.dataset.track) : sel.delete(t.dataset.track);
    } else if (t.dataset.album !== undefined) {
      const a = items.find((x) => String(x.id) === t.dataset.album);
      a.tracks.forEach((tr) => (t.checked ? sel.add(tr.path) : sel.delete(tr.path)));
    } else return;
    // Keep open albums open across the re-render.
    const openIds = [...qs("lib-list").querySelectorAll("details[open] [data-album]")].map((b) => b.dataset.album);
    renderList();
    openIds.forEach((id) => {
      const box = qs("lib-list").querySelector(`[data-album="${id}"]`);
      if (box) box.closest("details").open = true;
    });
  });
  qs("lib-list").addEventListener("click", (e) => {
    if (e.target.matches("input[type=checkbox]")) e.stopPropagation();
  });

  qs("lib-all").addEventListener("click", () => {
    allPaths().forEach((p) => sel.add(p));
    renderList();
  });
  qs("lib-none").addEventListener("click", () => {
    sel.clear();
    renderList();
  });
  qs("lib-close").addEventListener("click", () => qs("lib-dialog").close());

  async function remove(paths, message, label) {
    if (!(await askConfirm({ title: "Delete from your library?", message, confirmLabel: label }))) return;
    try {
      const r = await api("/api/library-check/delete", { method: "POST", body: JSON.stringify({ paths }) });
      showToast(`Deleted ${plural(r.deleted, "file")}`, "ok");
      await refresh();
      sel.clear();
      renderList();
      qs("lib-review").hidden = !items.length;
      if (!items.length) qs("lib-dialog").close();
    } catch (err) {
      showToast(err.message, "bad");
    }
  }

  qs("lib-del-sel").addEventListener("click", () =>
    remove([...sel], `${plural(sel.size, "file")} will be permanently deleted from disk. This can't be undone, but you can download them again later.`, "Delete selected")
  );
  qs("lib-del-all").addEventListener("click", () =>
    remove(null, `All ${plural(allPaths().length, "file")} listed here will be permanently deleted from disk, including albums you haven't ticked.`, "Delete all")
  );

  // Pick up a check that was already running or finished before this page load.
  api("/api/library-check")
    .then((s) => {
      if (s.status === "running") poll();
      else if (s.status === "done") {
        items = s.items;
        qs("lib-review").hidden = !items.length;
      }
    })
    .catch(() => {});
})();
