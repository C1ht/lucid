/* Custom tags dialog: pick different tags (artist, album, cover...) for one
   download, from the "..." menu on a track. Works for any source; artist and
   album suggestions come from Qobuz so the spelling matches what a Qobuz
   download of that artist would be tagged with. Depends on app.js helpers
   (qs, api, showToast, escapeHtml). Opened by browse.js via openTagEditor. */

(() => {
  const esc = escapeHtml;
  const dlg = qs("tags-dialog");
  const form = qs("tags-form");
  const input = {
    title: qs("tg-title"),
    artist: qs("tg-artist"),
    album: qs("tg-album"),
    album_artist: qs("tg-album-artist"),
    year: qs("tg-year"),
    track_number: qs("tg-track"),
    genre: qs("tg-genre"),
  };

  let target = null;
  let initial = {};
  let cover = { id: null, shown: null };
  let coverToken = 0;

  /* ---------- cover ---------- */

  function showCover(url) {
    const box = qs("tags-cover-img");
    box.innerHTML = url ? `<img alt="" src="${esc(url)}">` : "";
    box.querySelector("img")?.addEventListener("error", (e) => e.target.remove());
  }

  function resetCover() {
    coverToken += 1;
    cover = { id: null, shown: null };
    showCover(target.prefill.cover);
    qs("tags-cover-reset").hidden = true;
    qs("tags-cover-img").classList.remove("busy");
  }

  async function setCover(preview, request) {
    const mine = ++coverToken;
    const box = qs("tags-cover-img");
    showCover(preview);
    box.classList.add("busy");
    try {
      const res = await request();
      if (mine !== coverToken) return;
      cover = { id: res.cover_id, shown: preview };
      qs("tags-cover-reset").hidden = false;
    } catch (err) {
      if (mine !== coverToken) return;
      showToast(err.message, "bad");
      showCover(cover.shown || target.prefill.cover);
    } finally {
      if (mine === coverToken) box.classList.remove("busy");
    }
  }

  qs("tags-cover-pick").addEventListener("click", () => qs("tags-cover-file").click());
  qs("tags-cover-reset").addEventListener("click", resetCover);
  qs("tags-cover-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    setCover(URL.createObjectURL(file), () =>
      api("/api/cover", { method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file })
    );
  });

  /* ---------- suggestions ---------- */

  function suggest(field, list, fetchItems, render, pick) {
    let items = [];
    let active = -1;
    let timer = null;
    let seq = 0;

    const hide = () => {
      list.hidden = true;
      items = [];
      active = -1;
    };
    const mark = () => [...list.children].forEach((li, i) => li.classList.toggle("active", i === active));
    const choose = (i) => {
      const item = items[i];
      hide();
      if (item) pick(item);
    };

    field.addEventListener("input", () => {
      clearTimeout(timer);
      const q = field.value.trim();
      if (q.length < 2) return hide();
      timer = setTimeout(async () => {
        const mine = ++seq;
        try {
          const found = await fetchItems(q);
          if (mine !== seq || document.activeElement !== field) return;
          items = found;
          active = -1;
          list.innerHTML = found.map((it, i) => `<li data-i="${i}">${render(it)}</li>`).join("");
          list.hidden = !found.length;
        } catch (_) {
          hide(); // Qobuz not set up or offline: typing still works, just no suggestions
        }
      }, 250);
    });
    field.addEventListener("keydown", (e) => {
      if (list.hidden) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active = (active + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        mark();
      } else if (e.key === "Enter" && active >= 0) {
        e.preventDefault();
        choose(active);
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        hide();
      }
    });
    field.addEventListener("blur", () => setTimeout(hide, 120));
    // mousedown (not click) so the field keeps focus and blur doesn't hide the list first
    list.addEventListener("mousedown", (e) => {
      const li = e.target.closest("li");
      if (!li) return;
      e.preventDefault();
      choose(Number(li.dataset.i));
    });
  }

  const thumb = (url, cls = "") =>
    `<span class="sg-img ${cls}" ${url ? `style="background-image:url('${esc(url)}')"` : ""}></span>`;

  suggest(
    input.artist,
    qs("tg-artist-list"),
    async (q) => (await api(`/api/browse/suggest?kind=artist&q=${encodeURIComponent(q)}`)).items,
    (a) => `${thumb(a.image)}<span class="sg-main"><span class="sg-title">${esc(a.name)}</span></span>`,
    (a) => {
      input.artist.value = a.name;
    }
  );

  suggest(
    input.album,
    qs("tg-album-list"),
    async (q) =>
      (await api(`/api/browse/suggest?kind=album&q=${encodeURIComponent(q)}&artist=${encodeURIComponent(input.artist.value)}`)).items,
    (a) =>
      `${thumb(a.cover, "sq")}<span class="sg-main"><span class="sg-title">${esc(a.title)}</span><span class="sg-sub">${esc(
        [a.artist, a.year].filter(Boolean).join(" · ")
      )}</span></span>`,
    (a) => {
      input.album.value = a.title;
      if (a.year) input.year.value = a.year;
      if (a.genre && !input.genre.value.trim()) input.genre.value = a.genre;
      if (!input.artist.value.trim()) input.artist.value = a.artist;
      if (!input.album_artist.value.trim() && a.artist !== input.artist.value.trim()) input.album_artist.value = a.artist;
      if (a.cover_large) {
        setCover(a.cover_large, () => api("/api/cover/fetch", { method: "POST", body: JSON.stringify({ url: a.cover_large }) }));
      }
    }
  );

  /* ---------- open / submit ---------- */

  window.openTagEditor = (t) => {
    target = t;
    const p = t.prefill || {};
    initial = {
      title: p.title || "",
      artist: p.artist || "",
      album: p.album || "",
      album_artist: "",
      year: p.year || "",
      track_number: p.track_number ? String(p.track_number) : "",
      genre: p.genre || "",
    };
    for (const key of Object.keys(input)) input[key].value = initial[key];
    qs("tags-sub").textContent = t.label;
    resetCover();
    dlg.showModal();
    input.artist.focus();
    input.artist.select();
  };

  function collect() {
    const tags = {};
    for (const key of Object.keys(input)) {
      const value = input[key].value.trim();
      if (value && value !== initial[key]) tags[key] = value;
    }
    // A new artist also becomes the album artist unless that was set separately,
    // otherwise Navidrome would still file the track under the old one.
    if (tags.artist && !tags.album_artist) tags.album_artist = tags.artist;
    if (tags.track_number) tags.track_number = Number(tags.track_number);
    if (cover.id) tags.cover_id = cover.id;
    return Object.keys(tags).length ? tags : null;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const tn = input.track_number.value.trim();
    if (tn && !/^\d{1,3}$/.test(tn)) return showToast("Track number must be a number", "bad");
    const yr = input.year.value.trim();
    if (yr && !/^\d{4}$/.test(yr)) return showToast("Year must be 4 digits", "bad");
    if (qs("tags-cover-img").classList.contains("busy")) return showToast("Hang on, the cover is still loading", "bad");

    const custom_tags = collect();
    const ok = qs("tags-ok");
    ok.disabled = true;
    try {
      if (target.source === "url") {
        await api("/api/download/url", { method: "POST", body: JSON.stringify({ url: target.url, custom_tags }) });
      } else {
        await api("/api/download", {
          method: "POST",
          body: JSON.stringify({
            source: target.source,
            media_type: "track",
            id: target.id,
            desc: target.label,
            custom_tags,
          }),
        });
      }
      showToast(`Queued: ${target.label}`, "ok");
      dlg.close();
    } catch (err) {
      showToast(`Failed to queue: ${err.message}`, "bad");
    } finally {
      ok.disabled = false;
    }
  });

  qs("tags-cancel").addEventListener("click", () => dlg.close());
})();
