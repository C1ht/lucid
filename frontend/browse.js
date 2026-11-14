/* Search + catalog browsing: results, album / artist / track / playlist pages,
   and opening pasted links directly. Depends on helpers from app.js
   (qs, api, showToast, escapeHtml). Qobuz, plus YouTube search when it's enabled in Settings. */

(() => {
  const esc = escapeHtml;
  const attr = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const svg = (inner, extra = "") =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ${extra}>${inner}</svg>`;
  const ICON = {
    download: svg('<path d="M12 4v10m0 0l-4-4m4 4l4-4M5 19h14"/>'),
    check: svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
    back: svg('<path d="M15 6l-6 6 6 6"/>'),
    play: svg('<path d="M8 5.5v13l11-6.5z" fill="currentColor"/>'),
    stop: svg('<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor"/>'),
    more: svg('<circle cx="5.5" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="18.5" cy="12" r="1.4" fill="currentColor"/>'),
    tag: svg('<path d="M4 4h8l8 8-8 8-8-8z"/><circle cx="8.5" cy="8.5" r="1.2" fill="currentColor"/>'),
    note: svg('<path d="M9 18V6l10-2v12"/><circle cx="7" cy="18" r="2.2"/><circle cx="17" cy="16" r="2.2"/>'),
  };

  const state = {
    query: "",
    results: null,
    filter: "all",
    current: null, // { type, id, data, scroll }
    stack: [],
    reqId: 0,
    artistFilter: "all",
    selecting: false,
    selection: new Set(),
    monitored: new Set(),
    spotifyOn: true, // per-search switch; only matters while Spotify is enabled in Settings
  };
  try {
    state.spotifyOn = localStorage.getItem("lucid.spotify") !== "off";
  } catch (_) {}

  const body = () => qs("browse-body");
  const scroller = () => document.querySelector(".content");

  /* ---------- formatting ---------- */

  function fmtDuration(sec) {
    if (!sec && sec !== 0) return "";
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  }

  function fmtTotal(sec) {
    if (!sec) return "";
    const h = Math.floor(sec / 3600);
    const m = Math.round((sec % 3600) / 60);
    return h ? `${h} hr ${m} min` : `${m} min`;
  }

  const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

  /* ---------- small components ---------- */

  const explicitBadge = (on) => (on ? '<span class="badge-e" title="Explicit">E</span>' : "");

  function cover(url, cls = "") {
    const img = url ? `<img src="${attr(url)}" alt="" loading="lazy">` : "";
    return `<div class="cover ${cls}"><span class="cover-ph">${ICON.note}</span>${img}</div>`;
  }

  function collage(urls, cls = "") {
    const list = (urls || []).slice(0, 4);
    if (list.length < 4) return cover(list[0], cls);
    const cells = list.map((u) => `<img src="${attr(u)}" alt="" loading="lazy">`).join("");
    return `<div class="cover collage ${cls}"><span class="cover-ph">${ICON.note}</span><div class="collage-grid">${cells}</div></div>`;
  }

  const dlButton = (kind, id, label, cls = "icon-btn dl-btn") =>
    `<button type="button" class="${cls}" data-dl="${attr(kind + ":" + id)}" data-label="${attr(label)}" title="Download">${ICON.download}</button>`;

  const playButton = (id, label, cls = "icon-btn pv-btn") =>
    `<button type="button" class="${cls}" data-preview="${attr(id)}" title="Preview ${attr(label)}" aria-label="Preview ${attr(label)}">${ICON.play}</button>`;

  /* What each track's "..." menu acts on, keyed by the button's data-more. */
  const menuTargets = new Map();
  const moreButton = (key, target, cls = "icon-btn more-btn") => {
    menuTargets.set(key, target);
    return `<button type="button" class="${cls}" data-more="${attr(key)}" title="More options" aria-label="More options">${ICON.more}</button>`;
  };
  const qobuzTarget = (t) => ({
    source: "qobuz",
    id: t.id,
    label: labelTrack(t),
    prefill: {
      title: t.title,
      artist: t.artist,
      album: t.album,
      year: (t.date || "").slice(0, 4),
      track_number: t.track_number,
      cover: t.cover_large || t.cover,
    },
  });

  const ytEnabled = () => !!window.youtubeEnabled;
  const spotifyAvailable = () => !!(window.spotifyEnabled && window.spotifyConfigured);
  const spotifyActive = () => spotifyAvailable() && state.spotifyOn;

  window.syncSpotifyToggle = () => {
    const btn = qs("spotify-toggle");
    btn.hidden = !spotifyAvailable();
    btn.setAttribute("aria-pressed", String(state.spotifyOn));
    btn.classList.toggle("on", state.spotifyOn);
  };

  const labelAlbum = (a) => `${a.title} by ${a.artist}`;
  const labelTrack = (t) => (t.artist ? `${t.artist} - ${t.title}` : t.title);

  function loadingHtml(text = "Loading…") {
    return `<div class="loading"><span class="spinner"></span>${esc(text)}</div>`;
  }

  function noticeHtml(message, withSettings = false) {
    const btn = withSettings ? '<button type="button" class="btn btn-outline btn-small" data-act="open-settings">Open Settings</button>' : "";
    return `<div class="notice"><p>${esc(message)}</p>${btn}</div>`;
  }

  /* ---------- result cards ---------- */

  function albumCard(a, opts = {}) {
    const selected = state.selection.has(a.id);
    const sub = opts.artistPage
      ? [a.year, a.tracks_count ? plural(a.tracks_count, "track") : ""].filter(Boolean).join(" · ")
      : [a.artist, a.year].filter(Boolean).join(" · ");
    const kindTag = opts.artistPage && a.kind && a.kind !== "album" ? `<span class="kind-tag">${a.kind === "ep" ? "EP" : "Single"}</span>` : "";
    const sel = state.selecting ? `<span class="sel ${selected ? "on" : ""}">${selected ? ICON.check : ""}</span>` : "";
    const quick = state.selecting ? "" : dlButton("album", a.id, labelAlbum(a), "quick-dl");
    return `
      <div class="card ${selected ? "selected" : ""}" data-open="album:${attr(a.id)}" role="link" tabindex="0">
        <div class="cover-wrap">${cover(a.cover)}${kindTag}${sel}${quick}</div>
        <div class="card-title" title="${attr(a.title)}">${esc(a.title)}${explicitBadge(a.explicit)}</div>
        <div class="card-sub">${esc(sub)}</div>
      </div>`;
  }

  function avatar(name, image, cls = "") {
    const initial = esc((name || "?").trim().charAt(0).toUpperCase());
    const img = image ? `<img src="${attr(image)}" alt="" loading="lazy">` : "";
    return `<div class="avatar ${cls}"><span class="avatar-initial">${initial}</span>${img}</div>`;
  }

  function artistCard(a) {
    const sub = a.albums_count ? plural(a.albums_count, "release") : "Artist";
    return `
      <div class="card card-artist" data-open="artist:${attr(a.id)}" ${a.spotify_id ? `data-spotify="${attr(a.spotify_id)}"` : ""} role="link" tabindex="0">
        ${avatar(a.name, a.image)}
        <div class="card-title" title="${attr(a.name)}">${esc(a.name)}</div>
        <div class="card-sub">${esc(sub)}</div>
      </div>`;
  }

  function trackResultRow(t) {
    const sub = [t.artist, t.album].filter(Boolean).join(" · ");
    return `
      <div class="row" data-open="track:${attr(t.id)}" role="link" tabindex="0">
        ${cover(t.cover, "thumb")}
        <div class="row-main">
          <div class="row-line"><span class="row-title">${esc(t.title)}${explicitBadge(t.explicit)}</span>${playButton(t.id, labelTrack(t))}</div>
          <div class="row-sub">${esc(sub)}</div>
        </div>
        <span class="row-time">${esc(fmtDuration(t.duration))}</span>
        ${moreButton("q:" + t.id, qobuzTarget(t))}
        ${dlButton("track", t.id, labelTrack(t))}
      </div>`;
  }

  function youtubeRow(v) {
    return `
      <div class="row">
        ${cover(v.thumbnail, "thumb")}
        <div class="row-main">
          <div class="row-title">${esc(v.title)}</div>
          <div class="row-sub">${esc(v.channel)}</div>
        </div>
        <span class="row-time">${esc(fmtDuration(v.duration))}</span>
        ${moreButton("yt:" + v.id, {
          source: "youtube",
          id: v.id,
          label: v.channel ? `${v.channel} - ${v.title}` : v.title,
          prefill: { title: v.title, artist: v.channel, cover: v.thumbnail },
        })}
        ${dlButton("yt", v.id, v.channel ? `${v.channel} - ${v.title}` : v.title)}
      </div>`;
  }

  function playlistRow(p) {
    const sub = [p.owner, p.tracks_count ? plural(p.tracks_count, "track") : ""].filter(Boolean).join(" · ");
    return `
      <div class="row" data-open="playlist:${attr(p.id)}" role="link" tabindex="0">
        ${collage(p.covers, "thumb")}
        <div class="row-main">
          <div class="row-title">${esc(p.name)}</div>
          <div class="row-sub">${esc(sub)}</div>
        </div>
        <span class="row-time">${esc(fmtTotal(p.duration))}</span>
        ${dlButton("playlist", p.id, p.name)}
      </div>`;
  }

  /* ---------- search results ---------- */

  const SECTIONS = [
    { key: "albums", chip: "Albums", title: "Albums", limit: 12 },
    { key: "artists", chip: "Artists", title: "Artists", limit: 6 },
    { key: "tracks", chip: "Songs", title: "Songs", limit: 6 },
    { key: "playlists", chip: "Playlists", title: "Playlists", limit: 4 },
    { key: "youtube", chip: "YouTube", title: "YouTube", limit: 8 },
  ];
  const activeSections = () => SECTIONS.filter((s) => s.key !== "youtube" || ytEnabled());

  function renderChips() {
    const r = state.results;
    const toolbar = qs("browse-toolbar");
    toolbar.hidden = !(state.current && state.current.type === "results" && r);
    if (toolbar.hidden) return;
    const chips = [{ key: "all", label: "Show all", count: null }].concat(
      activeSections().map((s) => ({ key: s.key, label: s.chip, count: r[s.key].length }))
    );
    qs("filter-chips").innerHTML = chips
      .map(
        (c) =>
          `<button type="button" class="chip ${state.filter === c.key ? "active" : ""}" data-filter="${c.key}">${esc(c.label)}${
            c.count == null ? "" : `<span class="chip-count">${c.count}</span>`
          }</button>`
      )
      .join("");
  }

  function sectionHtml(sec, items, showAll) {
    if (!items.length) return "";
    const shown = showAll ? items : items.slice(0, sec.limit);
    let inner;
    if (sec.key === "albums") inner = `<div class="grid">${shown.map((a) => albumCard(a)).join("")}</div>`;
    else if (sec.key === "artists") inner = `<div class="grid grid-artists">${shown.map(artistCard).join("")}</div>`;
    else if (sec.key === "tracks") inner = `<div class="rows">${shown.map(trackResultRow).join("")}</div>`;
    else if (sec.key === "youtube") inner = `<div class="rows">${shown.map(youtubeRow).join("")}</div>`;
    else inner = `<div class="rows">${shown.map(playlistRow).join("")}</div>`;
    const more =
      !showAll && items.length > shown.length
        ? `<button type="button" class="link-btn" data-act="show-filter:${sec.key}">Show all ${items.length}</button>`
        : "";
    return `<div class="section"><div class="section-head"><h2>${sec.title}</h2>${more}</div>${inner}</div>`;
  }

  function renderResults() {
    const r = state.results;
    renderChips();
    if (!r) {
      body().innerHTML = idleHtml();
      return;
    }
    const active = activeSections();
    const total = active.reduce((n, s) => n + r[s.key].length, 0);
    const warn = (r.errors || []).map((m) => noticeHtml(m, /Settings/i.test(m))).join("") + spotifyNote(r);
    if (!total) {
      body().innerHTML = warn || noticeHtml(`No results for “${r.query}”.`);
      return;
    }
    const sections =
      state.filter === "all"
        ? active.map((s) => sectionHtml(s, r[s.key], false))
        : active.filter((s) => s.key === state.filter).map((s) => sectionHtml(s, r[s.key], true));
    body().innerHTML = warn + (sections.join("") || noticeHtml("Nothing here."));
  }

  function spotifyNote(r) {
    const sp = r.spotify;
    if (!sp) return "";
    const parts = [];
    if (sp.albums_total) parts.push(`${sp.albums_found} of ${sp.albums_total} releases`);
    if (sp.tracks_total) parts.push(`${sp.tracks_found} of ${sp.tracks_total} songs`);
    const found = parts.length ? ` Found on Qobuz: ${parts.join(", ")}.` : "";
    return `<p class="spotify-note">Matched against Spotify, so releases filed under the wrong artist are left out.${found} <button type="button" class="link-btn" data-act="spotify-off">Search without Spotify</button></p>`;
  }

  function idleHtml() {
    return `<div class="idle">
      <p class="idle-title">Search ${spotifyActive() ? "Qobuz, checked against Spotify" : "Qobuz"}${ytEnabled() ? " and YouTube" : ""}</p>
      <p>Find artists, albums, songs and playlists, or paste a link to open it directly.</p>
    </div>`;
  }

  /* ---------- detail pages ---------- */

  const backBar = () =>
    state.stack.length
      ? `<button type="button" class="back-btn" data-act="back">${ICON.back}Back</button>`
      : "";

  const linkTo = (kind, id, text) =>
    id ? `<a class="link" data-open="${attr(kind + ":" + id)}" tabindex="0">${esc(text)}</a>` : esc(text);

  function trackTable(tracks, { showAlbum = false, numbered = false } = {}) {
    if (!tracks.length) return noticeHtml("No tracks to show.");
    const multiDisc = !numbered && new Set(tracks.map((t) => t.disc)).size > 1;
    let lastDisc = null;
    const rows = tracks
      .map((t, i) => {
        let head = "";
        if (multiDisc && t.disc !== lastDisc) {
          head = `<div class="disc-row">Disc ${t.disc}</div>`;
          lastDisc = t.disc;
        }
        const num = numbered ? i + 1 : t.track_number || "";
        const third = showAlbum ? t.album || "" : t.artist || "";
        return `${head}
        <div class="trow" data-open="track:${attr(t.id)}" role="link" tabindex="0">
          <span class="trow-num">${esc(num)}</span>
          <span class="trow-main"><span class="trow-line"><span class="trow-title">${esc(t.title)}${explicitBadge(t.explicit)}</span>${playButton(t.id, labelTrack(t))}</span>${
            showAlbum ? `<span class="trow-sub">${esc(t.artist)}</span>` : ""
          }</span>
          <span class="trow-artist-col">${esc(third)}</span>
          <span class="row-time">${esc(fmtDuration(t.duration))}</span>
          ${moreButton("q:" + t.id, qobuzTarget(t))}
          ${dlButton("track", t.id, labelTrack(t))}
        </div>`;
      })
      .join("");
    return `<div class="track-table">
      ${rows}
    </div>`;
  }

  function renderAlbum(d) {
    const meta = [d.genre, d.year, d.tracks_count ? plural(d.tracks_count, "track") : "", fmtTotal(d.duration), d.quality]
      .filter(Boolean)
      .map(esc)
      .join(" · ");
    body().innerHTML = `
      ${backBar()}
      <div class="detail-head">
        ${cover(d.cover_large || d.cover, "detail-cover")}
        <div class="detail-info">
          <div class="eyebrow">${d.kind === "ep" ? "EP" : d.kind === "single" ? "Single" : "Album"}</div>
          <h1 class="detail-title">${esc(d.title)}${explicitBadge(d.explicit)}</h1>
          <div class="detail-artist">${linkTo("artist", d.artist_id, d.artist)}</div>
          <div class="detail-meta">${meta}</div>
          <div class="detail-actions">
            <button type="button" class="btn btn-accent btn-icon" data-dl="album:${attr(d.id)}" data-label="${attr(labelAlbum(d))}">${ICON.download}Download album</button>
          </div>
        </div>
      </div>
      ${trackTable(d.tracks)}
      <div class="detail-foot">${[d.copyright, d.label && `Label: ${d.label}`, d.date && `Released ${d.date}`]
        .filter(Boolean)
        .map(esc)
        .join("<br>")}</div>`;
  }

  function renderPlaylist(d) {
    const meta = [d.owner ? `By ${esc(d.owner)}` : "", d.tracks_count ? esc(plural(d.tracks_count, "track")) : "", esc(fmtTotal(d.duration))]
      .filter(Boolean)
      .join(" · ");
    const desc = d.description
      ? `<p class="detail-desc clamp" id="desc">${esc(d.description)}</p><button type="button" class="link-btn" data-act="toggle-desc">Read more</button>`
      : "";
    body().innerHTML = `
      ${backBar()}
      <div class="detail-head">
        ${collage(d.covers, "detail-cover")}
        <div class="detail-info">
          <div class="eyebrow">Playlist</div>
          <h1 class="detail-title">${esc(d.name)}</h1>
          <div class="detail-meta">${meta}</div>
          ${desc}
          <div class="detail-actions">
            <button type="button" class="btn btn-accent btn-icon" data-dl="playlist:${attr(d.id)}" data-label="${attr(d.name)}">${ICON.download}Download playlist</button>
          </div>
        </div>
      </div>
      ${trackTable(d.tracks, { showAlbum: true, numbered: true })}`;
  }

  function renderTrack(d) {
    const rows = [
      ["Album", d.album_id ? linkTo("album", d.album_id, d.album) : esc(d.album || "")],
      ["Released", esc(d.date || "")],
      ["Label", esc(d.label || "")],
      ["Duration", esc(fmtDuration(d.duration))],
      ["Quality", esc(d.quality || "")],
      ["Composer", esc(d.composer || "")],
      ["ISRC", esc(d.isrc || "")],
      ["Copyright", esc(d.copyright || "")],
    ]
      .filter(([, v]) => v)
      .map(([k, v]) => `<div class="kv"><span class="kv-k">${k}</span><span class="kv-v">${v}</span></div>`)
      .join("");
    body().innerHTML = `
      ${backBar()}
      <div class="detail-head">
        ${cover(d.cover_large || d.cover, "detail-cover")}
        <div class="detail-info">
          <div class="eyebrow">Song</div>
          <h1 class="detail-title">${esc(d.title)}${explicitBadge(d.explicit)}</h1>
          <div class="detail-artist">${linkTo("artist", d.artist_id, d.artist)}</div>
          <div class="kv-grid">${rows}</div>
          <div class="detail-actions">
            <button type="button" class="btn btn-accent btn-icon" data-dl="track:${attr(d.id)}" data-label="${attr(labelTrack(d))}">${ICON.download}Download</button>
            ${playButton(d.id, labelTrack(d), "btn btn-outline btn-icon pv-btn pv-solid")}
            ${d.album_id ? `<button type="button" class="btn btn-outline" data-open="album:${attr(d.album_id)}">Open album</button>` : ""}
            ${moreButton("q:" + d.id, qobuzTarget({ ...d, cover: d.cover_large || d.cover }), "btn btn-outline btn-icon more-btn more-solid")}
          </div>
        </div>
      </div>`;
  }

  /* ---- artist page (grid + selection re-render in place) ---- */

  const ARTIST_FILTERS = [
    { key: "all", label: "All" },
    { key: "album", label: "Albums" },
    { key: "ep", label: "EPs" },
    { key: "single", label: "Singles" },
    { key: "appears", label: "Appears on" },
  ];

  function artistGroups(d) {
    const primary = d.albums.filter((a) => a.primary);
    // Some artists (labels, composers) have no releases credited as primary.
    const own = primary.length ? primary : d.albums;
    return {
      all: own,
      album: own.filter((a) => a.kind === "album"),
      ep: own.filter((a) => a.kind === "ep"),
      single: own.filter((a) => a.kind === "single"),
      appears: primary.length ? d.albums.filter((a) => !a.primary) : [],
    };
  }

  function renderArtist(d) {
    const g = artistGroups(d);
    const bio = d.bio
      ? `<p class="detail-desc clamp" id="desc">${esc(d.bio)}</p><button type="button" class="link-btn" data-act="toggle-desc">Read more</button>`
      : "";
    body().innerHTML = `
      ${backBar()}
      <div class="detail-head artist-head">
        ${avatar(d.name, d.image, "avatar-xl")}
        <div class="detail-info">
          <div class="eyebrow">Artist</div>
          <h1 class="detail-title">${esc(d.name)}</h1>
          <div class="detail-meta">${esc(plural(g.all.length, "release"))}${g.appears.length ? ` · appears on ${g.appears.length}` : ""}${
            d.followers ? ` · ${esc(d.followers.toLocaleString())} followers` : ""
          }</div>
          ${d.genres && d.genres.length ? `<div class="detail-meta">${esc(d.genres.slice(0, 4).join(", "))}</div>` : ""}
          ${bio}
        </div>
      </div>
      ${
        d.spotify
          ? `<p class="spotify-note">${
              d.spotify.id
                ? `Showing releases that Spotify lists for this artist.${
                    d.spotify.hidden ? ` ${plural(d.spotify.hidden, "release")} hidden as likely mis-filed.` : ""
                  }`
                : "This artist wasn't found on Spotify, so nothing is filtered."
            } <button type="button" class="link-btn" data-act="spotify-unfilter">Show everything</button></p>`
          : d.spotify_error
            ? `<p class="spotify-note">Spotify: ${esc(d.spotify_error)} Showing unfiltered Qobuz data.</p>`
            : ""
      }
      <div class="artist-bar" id="artist-bar"></div>
      <div id="artist-grid"></div>`;
    renderArtistControls();
    renderArtistGrid();
    api("/api/monitor")
      .then((m) => {
        state.monitored = new Set(m.artists.map((a) => a.id));
        if (state.current && state.current.type === "artist" && state.current.data === d) renderArtistControls();
      })
      .catch(() => {});
  }

  function renderArtistControls() {
    const d = state.current.data;
    const g = artistGroups(d);
    const chips = ARTIST_FILTERS.filter((f) => f.key === "all" || g[f.key].length)
      .map(
        (f) =>
          `<button type="button" class="chip ${state.artistFilter === f.key ? "active" : ""}" data-act="art-filter:${f.key}">${f.label}<span class="chip-count">${g[f.key].length}</span></button>`
      )
      .join("");
    const n = state.selection.size;
    const watching = state.monitored.has(d.id);
    const monBtn = `<button type="button" class="btn btn-small ${watching ? "btn-accent" : "btn-outline"}" data-act="monitor-toggle">${watching ? "Monitoring" : "Monitor"}</button>`;
    const actions = monBtn + (state.selecting
      ? `<button type="button" class="btn btn-outline btn-small" data-act="select-all">Select all</button>
         <button type="button" class="btn btn-accent btn-small btn-icon" data-act="dl-selected" ${n ? "" : "disabled"}>${ICON.download}Download ${n ? `(${n})` : "selected"}</button>
         <button type="button" class="btn btn-outline btn-small" data-act="select-toggle">Cancel</button>`
      : `<button type="button" class="btn btn-outline btn-small" data-act="select-toggle">Select</button>`);
    qs("artist-bar").innerHTML = `<div class="chips">${chips}</div><div class="artist-actions">${actions}</div>`;
  }

  function renderArtistGrid() {
    const d = state.current.data;
    const items = artistGroups(d)[state.artistFilter] || [];
    qs("artist-grid").innerHTML = items.length
      ? `<div class="grid">${items.map((a) => albumCard(a, { artistPage: true })).join("")}</div>`
      : noticeHtml("Nothing in this category.");
  }

  /* ---------- navigation ---------- */

  function setCurrent(view, push) {
    if (push && state.current) {
      state.current.scroll = scroller().scrollTop;
      state.stack.push(state.current);
    }
    state.current = view;
  }

  async function render() {
    stopPreview();
    closeMenu();
    const view = state.current;
    qs("search-title").hidden = !!view && !["results", "link"].includes(view.type);
    renderChips();
    if (!view) {
      body().innerHTML = idleHtml();
      return;
    }
    if (view.type === "results") {
      renderResults();
      return;
    }
    if (view.type === "link") {
      body().innerHTML = `${backBar()}${linkHtml(view.url)}`;
      return;
    }
    if (!view.data) {
      body().innerHTML = `${backBar()}${loadingHtml()}`;
      try {
        // Any way of reaching an artist page gets the Spotify treatment while it's on.
        const sp =
          view.type === "artist" && !view.plain && spotifyActive()
            ? `?spotify_on=true${view.spotifyId ? `&spotify_id=${encodeURIComponent(view.spotifyId)}` : ""}`
            : "";
        view.data = await api(`/api/browse/${view.type}/${encodeURIComponent(view.id)}${sp}`);
      } catch (err) {
        if (state.current === view) {
          body().innerHTML = `${backBar()}${noticeHtml(err.message, /Settings/i.test(err.message))}`;
        }
        return;
      }
    }
    if (state.current !== view) return;
    if (view.type === "artist") {
      state.artistFilter = view.artistFilter || "all";
      state.selecting = false;
      state.selection.clear();
      renderArtist(view.data);
    } else if (view.type === "album") renderAlbum(view.data);
    else if (view.type === "track") renderTrack(view.data);
    else if (view.type === "playlist") renderPlaylist(view.data);
  }

  async function open(kind, id, spotifyId) {
    setCurrent({ type: kind, id, data: null, scroll: 0, spotifyId: spotifyId || "" }, true);
    scroller().scrollTop = 0;
    await render();
  }

  async function back() {
    const prev = state.stack.pop();
    if (!prev) return;
    state.current = prev;
    await render();
    scroller().scrollTop = prev.scroll || 0;
  }

  /* ---------- search + pasted links ---------- */

  // Returns {kind, id} for a Qobuz link we can browse, {external:true} for any
  // other link (downloadable but not browsable), or null if it isn't a link.
  function parseLink(text) {
    const t = text.trim();
    if (!/^https?:\/\//i.test(t)) return null;
    let url;
    try {
      url = new URL(t);
    } catch (_) {
      return null;
    }
    if (!/(^|\.)qobuz\.com$/i.test(url.hostname)) return { external: true };
    const parts = url.pathname.split("/").filter(Boolean);
    const kinds = { album: "album", track: "track", playlist: "playlist", playlists: "playlist", artist: "artist", interpreter: "artist" };
    for (let i = 0; i < parts.length - 1; i++) {
      const kind = kinds[parts[i].toLowerCase()];
      if (!kind) continue;
      const id = parts[parts.length - 1];
      // Artist pages on the web are /interpreter/<slug>/<id>; only a numeric
      // trailing segment is a usable artist id.
      if (kind === "artist" && !/^\d+$/.test(id)) return { external: true };
      return { kind, id };
    }
    return { external: true };
  }

  function linkHtml(url) {
    return `<div class="notice link-notice">
      <p>This link can't be browsed, but it can still be downloaded directly.</p>
      <p class="link-url">${esc(url)}</p>
      <div class="link-actions">
        <button type="button" class="btn btn-accent btn-icon" data-act="dl-link" data-url="${attr(url)}">${ICON.download}Download link</button>
        ${moreButton("link:" + url, { source: "url", url, label: url, prefill: {} }, "btn btn-outline btn-icon more-btn more-solid")}
      </div>
    </div>`;
  }

  async function runSearch(raw) {
    const q = raw.trim();
    state.query = q;
    state.stack = [];
    state.reqId += 1;
    const mine = state.reqId;

    if (!q) {
      state.results = null;
      state.current = null;
      await render();
      return;
    }

    const link = parseLink(q);
    if (link && link.kind) {
      state.current = { type: link.kind, id: link.id, data: null, scroll: 0 };
      await render();
      return;
    }
    if (link) {
      state.current = { type: "link", url: q, scroll: 0 };
      await render();
      return;
    }
    if (q.length < 2) return;

    state.current = { type: "results", scroll: 0 };
    qs("search-title").hidden = false;
    body().innerHTML = loadingHtml("Searching…");
    qs("browse-toolbar").hidden = true;
    const enc = encodeURIComponent(q);
    const viaSpotify = spotifyActive();
    const catalogue = async () => {
      if (!viaSpotify) return api(`/api/browse/search?q=${enc}`);
      try {
        return await api(`/api/browse/spotify?q=${enc}`);
      } catch (err) {
        // Spotify trouble shouldn't break search: fall back to plain Qobuz and say so.
        const plain = await api(`/api/browse/search?q=${enc}`);
        plain.errors = [`Spotify: ${err.message} Showing unfiltered Qobuz results.`];
        return plain;
      }
    };
    const [qobuz, yt] = await Promise.allSettled([
      catalogue(),
      ytEnabled() ? api(`/api/browse/youtube?q=${enc}`) : Promise.resolve({ items: [] }),
    ]);
    if (mine !== state.reqId) return;
    if (qobuz.status === "rejected" && (yt.status === "rejected" || !yt.value.items.length)) {
      state.results = null;
      body().innerHTML = noticeHtml(qobuz.reason.message, /Settings/i.test(qobuz.reason.message));
      return;
    }
    const data = qobuz.status === "fulfilled" ? qobuz.value : { query: q, albums: [], artists: [], tracks: [], playlists: [] };
    data.youtube = yt.status === "fulfilled" ? yt.value.items : [];
    data.errors = data.errors || [];
    if (qobuz.status === "rejected") data.errors.push(qobuz.reason.message);
    if (yt.status === "rejected") data.errors.push(yt.reason.message);
    state.results = data;
    state.filter = "all";
    renderResults();
  }

  /* ---------- "..." menu ---------- */

  const menu = { el: null, btn: null };

  function closeMenu() {
    if (menu.btn) menu.btn.classList.remove("open");
    if (menu.el) menu.el.hidden = true;
    menu.btn = null;
  }

  function openMenu(btn) {
    if (menu.btn === btn) return closeMenu();
    closeMenu();
    const target = menuTargets.get(btn.dataset.more);
    if (!target) return;
    if (!menu.el) {
      menu.el = document.createElement("div");
      menu.el.className = "menu";
      menu.el.setAttribute("role", "menu");
      document.body.appendChild(menu.el);
    }
    // Extend this list for new per-track actions.
    menu.el.innerHTML = `<button type="button" class="menu-item" role="menuitem" data-menu-act="tags">${ICON.tag}Custom tags…</button>`;
    menu.el.onclick = (e) => {
      const item = e.target.closest("[data-menu-act]");
      if (!item) return;
      closeMenu();
      if (item.dataset.menuAct === "tags") window.openTagEditor(target);
    };
    menu.el.hidden = false;
    menu.btn = btn;
    btn.classList.add("open");
    const r = btn.getBoundingClientRect();
    const h = menu.el.offsetHeight;
    const w = menu.el.offsetWidth;
    const top = r.bottom + 6 + h > window.innerHeight ? r.top - h - 6 : r.bottom + 6;
    menu.el.style.top = `${Math.max(8, top)}px`;
    menu.el.style.left = `${Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))}px`;
    menu.el.querySelector(".menu-item").focus();
  }

  document.addEventListener("click", (e) => {
    if (menu.btn && !e.target.closest(".menu") && !e.target.closest("[data-more]")) closeMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && menu.btn) closeMenu();
  });
  window.addEventListener("resize", closeMenu);
  document.addEventListener("scroll", closeMenu, true);

  /* ---------- previews ---------- */

  const PREVIEW_SECONDS = 30;
  const preview = { audio: new Audio(), btn: null };

  function stopPreview() {
    const { audio, btn } = preview;
    audio.pause();
    audio.removeAttribute("src");
    preview.btn = null;
    if (btn) {
      btn.classList.remove("playing", "loading");
      btn.innerHTML = ICON.play;
    }
  }

  async function togglePreview(btn) {
    if (preview.btn === btn) return stopPreview();
    stopPreview();
    preview.btn = btn;
    btn.classList.add("playing", "loading");
    btn.innerHTML = '<span class="spinner spinner-sm"></span>';
    try {
      const { url } = await api(`/api/browse/preview/${encodeURIComponent(btn.dataset.preview)}`);
      if (preview.btn !== btn) return;
      preview.audio.src = url;
      await preview.audio.play();
      if (preview.btn !== btn) return;
      btn.classList.remove("loading");
      btn.innerHTML = ICON.stop;
    } catch (err) {
      if (preview.btn === btn) {
        stopPreview();
        showToast(`Preview failed: ${err.message}`, "bad");
      }
    }
  }

  preview.audio.addEventListener("ended", stopPreview);
  preview.audio.addEventListener("timeupdate", () => {
    if (preview.audio.currentTime >= PREVIEW_SECONDS) stopPreview();
  });

  /* ---------- downloads ---------- */

  async function queueDownload(kind, id, label) {
    const override = qs("tagging-override").value;
    const navidrome_tagging = override === "" ? null : override === "true";
    const yt = kind === "yt";
    await api("/api/download", {
      method: "POST",
      body: JSON.stringify({
        source: yt ? "youtube" : "qobuz",
        media_type: yt ? "track" : kind,
        id,
        desc: label,
        navidrome_tagging,
      }),
    });
  }

  async function handleDownload(btn) {
    const [kind, ...rest] = btn.dataset.dl.split(":");
    const id = rest.join(":");
    const label = btn.dataset.label || id;
    if (btn.dataset.busy) return;
    btn.dataset.busy = "1";
    try {
      await queueDownload(kind, id, label);
      showToast(`Queued: ${label}`, "ok");
      const original = btn.innerHTML;
      btn.classList.add("done");
      if (btn.classList.contains("icon-btn") || btn.classList.contains("quick-dl")) btn.innerHTML = ICON.check;
      setTimeout(() => {
        btn.classList.remove("done");
        btn.innerHTML = original;
        delete btn.dataset.busy;
      }, 1600);
    } catch (err) {
      delete btn.dataset.busy;
      showToast(`Failed to queue: ${err.message}`, "bad");
    }
  }

  async function downloadSelected() {
    const d = state.current.data;
    const chosen = d.albums.filter((a) => state.selection.has(a.id));
    let ok = 0;
    for (const a of chosen) {
      try {
        await queueDownload("album", a.id, labelAlbum(a));
        ok += 1;
      } catch (err) {
        showToast(`Failed to queue ${a.title}: ${err.message}`, "bad");
      }
    }
    if (ok) showToast(`Queued ${plural(ok, "album")}`, "ok");
    state.selecting = false;
    state.selection.clear();
    renderArtistControls();
    renderArtistGrid();
  }

  /* ---------- events ---------- */

  function onAction(el) {
    const [act, arg] = el.dataset.act.split(/:(.*)/s);
    switch (act) {
      case "back":
        return back();
      case "show-filter":
        state.filter = arg;
        renderResults();
        return;
      case "toggle-desc": {
        const p = qs("desc");
        const open = p.classList.toggle("clamp") === false;
        el.textContent = open ? "Show less" : "Read more";
        return;
      }
      case "spotify-off":
        state.spotifyOn = false;
        persistSpotify();
        return runSearch(state.query);
      case "spotify-unfilter": {
        const v = state.current;
        v.spotifyId = "";
        v.plain = true;
        v.data = null;
        return render();
      }
      case "art-filter":
        state.artistFilter = arg;
        state.current.artistFilter = arg;
        renderArtistControls();
        renderArtistGrid();
        return;
      case "monitor-toggle": {
        const d = state.current.data;
        const on = state.monitored.has(d.id);
        const req = on
          ? api(`/api/monitor/artists/${encodeURIComponent(d.id)}`, { method: "DELETE" })
          : api("/api/monitor/artists", { method: "POST", body: JSON.stringify({ id: d.id }) });
        return req
          .then((m) => {
            state.monitored = new Set(m.artists.map((a) => a.id));
            showToast(on ? `Stopped monitoring ${d.name}` : `Monitoring ${d.name}. Missing releases are being queued.`, "ok");
            renderArtistControls();
            document.dispatchEvent(new Event("monitor-changed"));
          })
          .catch((err) => showToast(err.message, "bad"));
      }
      case "select-toggle":
        state.selecting = !state.selecting;
        state.selection.clear();
        renderArtistControls();
        renderArtistGrid();
        return;
      case "select-all":
        artistGroups(state.current.data)[state.artistFilter].forEach((a) => state.selection.add(a.id));
        renderArtistControls();
        renderArtistGrid();
        return;
      case "dl-selected":
        return downloadSelected();
      case "open-settings":
        return document.querySelector('.nav-item[data-view="settings"]').click();
      case "dl-link":
        return api("/api/download/url", { method: "POST", body: JSON.stringify({ url: el.dataset.url }) })
          .then(() => showToast("Queued", "ok"))
          .catch((err) => showToast(`Failed to queue: ${err.message}`, "bad"));
    }
  }

  function onOpen(el) {
    const [kind, ...rest] = el.dataset.open.split(":");
    const id = rest.join(":");
    if (state.selecting && state.current.type === "artist" && kind === "album") {
      if (state.selection.has(id)) state.selection.delete(id);
      else state.selection.add(id);
      renderArtistControls();
      renderArtistGrid();
      return;
    }
    open(kind, id, el.dataset.spotify);
  }

  function persistSpotify() {
    try {
      localStorage.setItem("lucid.spotify", state.spotifyOn ? "on" : "off");
    } catch (_) {}
    window.syncSpotifyToggle();
  }

  function initBrowse() {
    const input = qs("search-query");
    window.syncSpotifyToggle();
    qs("spotify-toggle").addEventListener("click", () => {
      state.spotifyOn = !state.spotifyOn;
      persistSpotify();
      if (state.query && state.current && state.current.type === "results") runSearch(state.query);
      else if (!state.query) render();
    });
    const clear = qs("search-clear");
    let timer = null;

    input.addEventListener("input", () => {
      clear.hidden = input.value === "";
      clearTimeout(timer);
      const looksLikeLink = /^https?:\/\//i.test(input.value.trim());
      timer = setTimeout(() => runSearch(input.value), looksLikeLink ? 0 : 400);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        clearTimeout(timer);
        runSearch(input.value);
      }
    });
    window.resetSearch = () => {
      input.value = "";
      clear.hidden = true;
      clearTimeout(timer);
      runSearch("");
    };
    clear.addEventListener("click", () => {
      input.value = "";
      clear.hidden = true;
      runSearch("");
      input.focus();
    });

    qs("paste-toggle").addEventListener("click", () => {
      const form = qs("url-form");
      form.hidden = !form.hidden;
      if (!form.hidden) qs("url-input").focus();
    });
    qs("url-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const url = qs("url-input").value.trim();
      if (!url) return;
      try {
        await api("/api/download/url", { method: "POST", body: JSON.stringify({ url }) });
        showToast("Queued", "ok");
        qs("url-input").value = "";
      } catch (err) {
        showToast(`Failed to queue: ${err.message}`, "bad");
      }
    });

    qs("filter-chips").addEventListener("click", (e) => {
      const chip = e.target.closest("[data-filter]");
      if (!chip) return;
      state.filter = chip.dataset.filter;
      renderResults();
    });

    const root = body();
    root.addEventListener("click", (e) => {
      const more = e.target.closest("[data-more]");
      if (more) {
        e.stopPropagation();
        openMenu(more);
        return;
      }
      const pv = e.target.closest("[data-preview]");
      if (pv) {
        e.stopPropagation();
        togglePreview(pv);
        return;
      }
      const dl = e.target.closest("[data-dl]");
      if (dl) {
        e.stopPropagation();
        handleDownload(dl);
        return;
      }
      const act = e.target.closest("[data-act]");
      if (act) {
        onAction(act);
        return;
      }
      const opener = e.target.closest("[data-open]");
      if (opener) onOpen(opener);
    });
    root.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const opener = e.target.closest("[data-open]");
      if (opener && e.target === opener) onOpen(opener);
    });
    // Hide broken cover images (the placeholder beneath shows instead).
    root.addEventListener(
      "error",
      (e) => {
        if (e.target.tagName === "IMG") e.target.style.display = "none";
      },
      true
    );

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.stack.length && document.activeElement !== input) back();
    });

    render();
    input.focus();
  }

  initBrowse();
})();
