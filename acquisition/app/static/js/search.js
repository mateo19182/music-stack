// Quick downloads: candidate id -> {job, label, ready}; kept across reloads in this browser.
function loadQuick() {
  try {
    const day = Date.now() - 86400000;
    return new Map(JSON.parse(localStorage.getItem("acquire.quick") || "[]").filter(([, q]) => (q.at || 0) > day));
  } catch {
    return new Map();
  }
}
state.quick = loadQuick();
function saveQuick() {
  try {
    localStorage.setItem("acquire.quick", JSON.stringify([...state.quick]));
  } catch {}
}
function quickButton(r) {
  const quick = state.quick.get(r.id);
  if (quick?.ready)
    return `<a href="/api/jobs/${path(quick.job)}/zip">Download files</a>`;
  if (quick)
    return `<button class="quiet" disabled>${esc(quick.status || "Starting…")}</button>`;
  return `<button class="quiet" data-quick="${esc(r.id)}" title="Start this copy now and download its files here as soon as they arrive. They also go to your library.">Quick download</button>`;
}
const QUICK_READY = ["process_queued", "processing", "review", "publish_queued", "publishing", "published"];
async function pollQuick() {
  let changed = false;
  for (const [candidate, quick] of state.quick) {
    if (quick.ready) continue;
    let job;
    try {
      job = await api(`/api/jobs/${path(quick.job)}`);
    } catch {
      state.quick.delete(candidate);
      changed = true;
      continue;
    }
    if (QUICK_READY.includes(job.stage)) {
      quick.ready = true;
      const link = document.createElement("a");
      link.href = `/api/jobs/${path(quick.job)}/zip`;
      link.download = "";
      document.body.append(link);
      link.click();
      link.remove();
      toast(`${quick.label} downloaded. It also goes to your library.`);
    } else if (["failed", "cancelled", "rejected"].includes(job.stage)) {
      state.quick.delete(candidate);
      toast(`${quick.label}: ${job.error || "the download failed"}. Try another copy.`, true);
    } else {
      const status = job.stage === "queued" ? "Starting…" : `Downloading… ${Math.round(job.progress || 0)}%`;
      if (status === quick.status) continue;
      quick.status = status;
    }
    changed = true;
  }
  if (changed) {
    saveQuick();
    if (state.results.length) renderResults({ results: state.results, status: state.searchStatus });
  }
}
function resultSourceLink(result) {
  if (result.source === "torrent" && result.info_url) {
    const href = safeLink(result.info_url);
    return href === "#" ? "" : `<a class="result-source-link" href="${href}" target="_blank" rel="noopener noreferrer">${esc(result.provider || "Tracker")} page ↗</a>`;
  }
  if (!["youtube", "yt-dlp"].includes(result.source) || !result.url) return "";
  const href = safeLink(result.url);
  if (href === "#") return "";
  const host = new URL(result.url, location.origin).hostname;
  const label =
    host === "youtu.be" ||
    host === "youtube.com" ||
    host.endsWith(".youtube.com")
      ? "YouTube"
      : "Original source";
  return `<a class="result-source-link" href="${href}" target="_blank" rel="noopener noreferrer">${label} ↗</a>`;
}
function resultFiles(result) {
  return result.files?.length ? result.files : [result];
}
function resultFormat(file) {
  return String(
    file.format || (file.filename || "").split(".").slice(1).pop() || "",
  )
    .toLowerCase()
    .replace(/^\./, "");
}
function matchesQuality(file, quality) {
  const lossless = ["flac", "wav", "aiff", "aif", "alac"].includes(
    resultFormat(file),
  );
  const bitrate = Number(file.bitrate || 0);
  if (quality === "lossless") return lossless;
  if (quality === "unknown") return !lossless && bitrate <= 0;
  return !quality || lossless || bitrate >= Number(quality);
}
function searchLibraryHint(result) {
  const matches = result.library_matches || [];
  if (!matches.length) return "";
  const album = result.kind === "album";
  const label = album
    ? `${result.matched_track_count || 0} of ${result.listed_track_count || result.files?.length || 0} listed tracks have library matches`
    : result.library_match === "exact"
      ? "Already in library"
      : "Possible library match";
  return `<details class="source-details"><summary>${esc(label)}</summary><p class="muted">${album ? "Only listed files were checked. The full folder is confirmed when queued." : "Check the album and version. Matching tags do not prove identical audio."}</p>${matches
    .slice(0, 5)
    .map(
      (file) =>
        `<p>${esc(file.artist)} · ${esc(file.title)}</p>${meta([file.album, file.format, file.bitrate ? `${file.bitrate} kbps` : ""])}<div class="actions"><button class="quiet" data-preview="${esc(file.id)}" data-title="${esc(file.title)}" data-artist="${esc(file.artist)}">Play existing</button></div>`,
    )
    .join(
      "",
    )}${matches.length > 5 ? `<p class="muted">${matches.length - 5} more library matches.</p>` : ""}</details>`;
}
const LOSSLESS = new Set(["flac", "alac", "wav", "aiff", "ape", "wv"]);
const SEARCHING = ["pending", "queued", "running", "searching"];
function isLossless(r) {
  return LOSSLESS.has(resultFormat(r));
}
function qualityBadge(r) {
  const format = resultFormat(r).toUpperCase() || "?";
  if (isLossless(r)) return `<span class="q-badge lossless">${esc(format)}</span>`;
  const kbps = Number(r.bitrate || 0);
  return `<span class="q-badge${kbps >= 256 ? " good" : ""}">${esc(format)}${kbps ? ` <small>${kbps}</small>` : ""}</span>`;
}
function qualityScore(r) {
  return isLossless(r) ? 10000 : Number(r.bitrate || 0);
}
function availabilityText(r) {
  if (r.source === "torrent") return r.seeders != null ? `${r.seeders} seeders` : "";
  if (r.source !== "soulseek") return "";
  if (r.free_slots) return "Free slot";
  return r.queue_length ? `Queue ${r.queue_length}` : "Queued";
}
function resultFolder(r) {
  const parts = String(r.directory || r.filename || "").split(/[\\/]/).filter(Boolean);
  parts.pop();   // the file, or the album folder already shown as the title
  return parts.slice(r.kind === "album" ? -1 : -2).join(" / ");
}
function searchSource(r) {
  return r.source === "youtube" ? (r.provider === "YouTube Music" ? "YouTube Music" : "YouTube") : sourceLabel(r.source);
}
// Copies of one file or folder from different uploaders; "03. A - B" and "03 - A - B" are one name.
function copyKey(r) {
  if (r.source !== "soulseek") return r.id;
  const name = String(r.kind === "album" ? r.leaf_folder || r.album || r.title : r.title || r.filename)
    .toLowerCase().replace(/^\s*\d{1,3}(?:\s*[.\-_)]\s*|\s+)/, "").replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  return [r.kind, name, resultFormat(r), r.bitrate || "", r.file_count || 1, Math.round((r.duration || 0) / 5)].join("|");
}
function albumLength(r) {
  const count = r.file_count || r.files?.length || 0;
  const seconds = (r.files || []).reduce((sum, f) => sum + (Number(f.duration) || 0), 0) || (r.source === "soulseek" ? 0 : r.duration);
  return [`${count} track${count === 1 ? "" : "s"}`, seconds ? duration(seconds) : ""].filter(Boolean).join(", ");
}
function sortResults(results) {
  const order = {
    quality: (a, b) => qualityScore(b) - qualityScore(a) || (b.match || 0) - (a.match || 0),
    availability: (a, b) =>
      Number(!!b.free_slots) - Number(!!a.free_slots) ||
      Number(a.queue_length || 0) - Number(b.queue_length || 0) ||
      qualityScore(b) - qualityScore(a),
  }[state.searchSort];
  return order ? [...results].sort(order) : results;
}
function groupCopies(results) {
  const groups = new Map();
  for (const r of results) {
    const key = copyKey(r);
    groups.has(key) ? groups.get(key).push(r) : groups.set(key, [r]);
  }
  return [...groups.values()];
}
function getButtons(r, primary = true) {
  const queued = state.queuedCandidates.has(r.id);
  return `${primary ? quickButton(r) : ""}<button class="${primary ? "" : "quiet"}" data-enqueue="${esc(r.id)}" title="Request this copy. If it fails, the app tries other copies." ${queued ? 'disabled data-queued="true"' : ""}>${queued ? "Requested" : "Get"}</button>`;
}
function resultRow([r, ...copies]) {
  const title = r.kind === "album" ? r.album || r.title : r.title || r.filename;
  const where = r.source === "soulseek" ? resultFolder(r) : r.uploader || r.provider || "";
  const files = r.kind === "album" && r.files?.length
    ? `<details class="source-details"><summary>${esc(r.file_count || r.files.length)} files${r.folder_complete === false ? " listed" : ""}</summary><ul class="file-list">${r.files.map((f) => `<li>${esc(String(f.filename).split(/[\\/]/).pop())} <span class="muted">${esc([duration(f.duration), bytes(f.size)].filter(Boolean).join(" · "))}</span></li>`).join("")}</ul></details>`
    : "";
  const more = copies.length
    ? `<details class="source-details copies"><summary>${copies.length} more ${copies.length === 1 ? "copy" : "copies"}</summary>${copies
        .map((c) => `<div class="copy-row">${meta([c.username, availabilityText(c), bytes(c.size)])}${getButtons(c, false)}</div>`)
        .join("")}</details>`
    : "";
  return `<article class="result-row${(r.match ?? 1) < 1 ? " weak" : ""}">${qualityBadge(r)}<div class="track-info"><h3>${esc(title || "Untitled")}</h3>${meta([r.artist, r.kind === "album" ? "" : r.album, r.kind === "album" ? albumLength(r) : duration(r.duration), bytes(r.size)])}<div class="meta source-line"><span>${esc(searchSource(r))}${r.username ? ` · ${esc(r.username)}` : ""}</span>${availabilityText(r) ? `<span class="${r.free_slots || r.seeders >= 3 ? "ok" : ""}">${esc(availabilityText(r))}</span>` : ""}${where && where !== r.username ? `<span class="path" title="${esc(r.filename || "")}">${esc(where)}</span>` : ""}</div>${searchLibraryHint(r)}${resultSourceLink(r)}${files}${more}</div><div class="result-actions">${getButtons(r)}</div></article>`;
}
function renderResults(data) {
  state.results = data.results || [];
  state.searchStatus = data.status || "complete";
  if ("error" in data) state.searchError = data.error;
  $("#search-results").hidden = false;
  const searching = SEARCHING.includes(state.searchStatus);
  const formats = [...new Set(state.results.flatMap(resultFiles).map(resultFormat).filter(Boolean))].sort();
  if (state.searchFormat && !formats.includes(state.searchFormat)) formats.push(state.searchFormat);
  const shown = sortResults(
    state.results.filter((r) =>
      resultFiles(r).every((f) => (!state.searchFormat || resultFormat(f) === state.searchFormat) && matchesQuality(f, state.searchQuality)),
    ),
  );
  const strong = groupCopies(shown.filter((r) => (r.match ?? 1) >= 1));
  const weak = groupCopies(shown.filter((r) => (r.match ?? 1) < 1));
  const filtered = state.searchFormat || state.searchQuality;
  const sorts = [["relevance", "Best match"], ["quality", "Best quality"], ["availability", "Fastest"]];
  const tools = `<div class="result-tools"><div class="segmented" role="group" aria-label="Order">${sorts.map(([value, label]) => `<button type="button" data-result-sort="${value}" aria-pressed="${state.searchSort === value}">${label}</button>`).join("")}</div><label>Quality<select id="result-quality"><option value="">Any</option><option value="lossless">Lossless</option><option value="320">320 kbps+</option><option value="256">256 kbps+</option><option value="192">192 kbps+</option></select></label><label>Type<select id="result-format"><option value="">Any</option>${formats.map((f) => `<option value="${esc(f)}">${esc(f.toUpperCase())}</option>`).join("")}</select></label>${filtered ? '<button class="quiet" id="clear-result-filters">Clear filters</button>' : ""}</div>`;
  const sources = Object.entries(state.results.reduce((n, r) => ((n[searchSource(r)] = (n[searchSource(r)] || 0) + 1), n), {}));
  $("#search-status").innerHTML = searching
    ? `<span class="spinner" aria-hidden="true"></span>Searching${state.searchSource === "youtube" ? "" : "; Soulseek takes about half a minute"}…`
    : state.searchStatus === "failed"
      ? `<span class="error">${esc(state.searchError || "Search failed. Try again.")}</span>`
      : esc(`${strong.length} match${strong.length === 1 ? "" : "es"}${weak.length ? `, ${weak.length} partial` : ""}${sources.length ? ` · ${sources.map(([s, n]) => `${s} ${n}`).join(", ")}` : ""}${filtered ? ` · ${shown.length} of ${state.results.length} files shown` : ""}`);
  const warning = state.searchStatus !== "failed" && state.searchError ? `<p class="error">${esc(state.searchError)}</p>` : "";
  const list = (groups) => `<div class="result-list">${groups.map(resultRow).join("")}</div>`;
  $("#results").innerHTML = searching
    ? ""
    : warning +
      (state.results.length ? tools : "") +
      (strong.length
        ? list(strong) + (weak.length ? `<details class="weak-results"><summary>${weak.length} partial match${weak.length === 1 ? "" : "es"}: some words you searched are missing</summary>${list(weak)}</details>` : "")
        : weak.length
          ? `<p class="muted">No result has every word you searched. Closest first:</p>${list(weak)}`
          : `<div class="empty">${state.results.length ? "Nothing matches these filters." : "No results. Try fewer words, or the other kind (track or album)."}</div>`);
  if ($("#result-format")) {
    $("#result-format").value = state.searchFormat;
    $("#result-quality").value = state.searchQuality;
  }
}
async function pollSearch(generation) {
  if (
    !state.searchId ||
    generation !== state.searchGeneration ||
    !state.authenticated
  )
    return;
  try {
    const data = await api(`/api/search/${path(state.searchId)}`);
    if (generation !== state.searchGeneration) return;
    renderResults(data);
    if (["pending", "queued", "running", "searching"].includes(data.status))
      setTimeout(() => pollSearch(generation), 2000);
  } catch (e) {
    toast(e.message, true);
    $("#search-status").textContent =
      "Search interrupted. Please search again.";
  }
}
const LINK = /^(?:https?:\/\/|www\.|(?:open\.spotify|soundcloud|youtube|music\.youtube|vimeo)\.com\/|youtu\.be\/|[\w-]+\.bandcamp\.com)/i;
function searchForm() {
  return formData($("#search-form"));
}
async function runSearch() {
  const input = searchForm();
  if (!input.query.trim() && !["artist", "title", "album"].some((name) => input[name].trim())) {
    $("#search-form [name=query]").focus();
    return;
  }
  const generation = ++state.searchGeneration;
  state.searchSource = input.source;
  state.searchError = null;
  renderResults({ results: [], status: "searching" });
  const data = await api("/api/search", input).catch((error) => {
    renderResults({ results: [], status: "failed", error: error.message });
    throw error;
  });
  if (generation !== state.searchGeneration) return;
  state.searchId = data.id;
  renderResults(data);
  if (SEARCHING.includes(data.status)) setTimeout(() => pollSearch(generation), 1500);
}
async function readLink(text) {
  const url = /^https?:\/\//i.test(text) ? text : `https://${text}`;
  state.searchGeneration++;
  state.link = null;
  $("#search-results").hidden = true;
  $("#link-card").innerHTML = `<div class="link-card"><p class="muted"><span class="spinner" aria-hidden="true"></span>Reading the link…</p></div>`;
  try {
    state.link = { ...(await api("/api/links", { url })), requested: {} };
    renderLink();
  } catch (e) {
    $("#link-card").innerHTML = `<div class="link-card"><p class="error">${esc(e.message)}</p></div>`;
  }
}
function linkTracks() {
  return [...document.querySelectorAll("[data-link-track]:checked")].map((box) => state.link.tracks[Number(box.dataset.linkTrack)]);
}
function renderLink() {
  const l = state.link;
  if (!l) return ($("#link-card").innerHTML = "");
  const playlist = l.kind === "playlist";
  const count = l.track_count || l.tracks.length;
  const total = l.tracks.reduce((sum, t) => sum + (t.duration || 0), 0) || l.duration;
  const done = (key, label) => (l.requested[key] ? `<button disabled>${label === "Download as is" ? "Downloading" : "Requested"}</button>` : "");
  const action = (key, label, attrs, quiet = false) => done(key, label) || `<button type="button" class="${quiet ? "quiet" : ""}" ${attrs}>${esc(label)}</button>`;
  const fields = playlist
    ? ""
    : `<div class="options link-fields"><label class="grow">Artist<input id="link-artist" value="${esc(l.artist)}" /></label><label class="grow">${l.kind === "album" ? "Album" : "Title"}<input id="link-name" value="${esc(l.kind === "album" ? l.album || l.title : l.title)}" /></label></div>`;
  const tracks = l.tracks.length
    ? playlist
      ? `<div class="link-tracks"><label class="checkbox"><input type="checkbox" id="link-all" checked />All ${l.tracks.length} tracks</label><ol>${l.tracks.map((t, i) => `<li><label class="checkbox"><input type="checkbox" data-link-track="${i}" checked /><span>${esc(t.artist)} · ${esc(t.title)}</span><span class="muted">${esc(duration(t.duration))}</span></label></li>`).join("")}</ol></div>`
      : `<details class="link-tracks"><summary>${l.tracks.length} tracks</summary><ol>${l.tracks.map((t) => `<li>${esc(t.title)} <span class="muted">${esc(duration(t.duration))}</span></li>`).join("")}</ol></details>`
    : "";
  const selected = playlist ? linkTracks().length || (document.querySelector("[data-link-track]") ? 0 : l.tracks.length) : 0;
  const actions = playlist
    ? (l.tracks.length ? action("tracks", `Find best copies of ${selected} track${selected === 1 ? "" : "s"}`, `data-link="tracks" ${selected ? "" : "disabled"}`) : "")
    : action("best", "Find best copy", 'data-link="best" title="The app searches every source and takes the best copy, lossless first."') +
      action("search", "See copies", 'data-link="search" title="Search with these names and choose a copy yourself."', true);
  $("#link-card").innerHTML = `<article class="link-card"><div class="link-head"><span class="badge">${esc(l.provider)} · ${esc({ track: "Track", album: "Album", playlist: "Playlist" }[l.kind])}</span><button type="button" class="quiet" data-link="close" aria-label="Close link">✕</button></div><h2>${esc(l.title || "Untitled")}</h2>${meta([playlist ? "" : l.artist, l.year, count > 1 ? `${count} tracks` : "", duration(total)])}${fields}${tracks}<div class="actions">${actions}${l.downloadable ? action("as-is", "Download as is", `data-link="as-is" title="Download exactly this ${l.kind} from ${esc(l.provider)} (usually lossy)."`, true) : ""}${Object.keys(l.requested).length ? '<button type="button" class="link" data-tab="activity">Follow it in Requests →</button>' : ""}</div>${playlist && l.tracks.length ? `<p class="muted">Each track becomes a request in the list “${esc(l.title.slice(0, 120))}”.</p>` : ""}</article>`;
}
async function linkAction(button) {
  const l = state.link;
  const which = button.dataset.link;
  if (which === "close") {
    state.link = null;
    $("#search-form [name=query]").value = "";
    queryIsLink();
    return renderLink();
  }
  const artist = $("#link-artist")?.value.trim() || l.artist;
  const name = $("#link-name")?.value.trim() || l.title;
  if (which === "search") {
    const field = (name) => $(`#search-form [name=${name}]`);
    showPrecise(true);
    field("query").value = "";
    queryIsLink();
    field("artist").value = artist;
    field("title").value = l.kind === "album" ? "" : name;
    field("album").value = l.kind === "album" ? name : l.album || "";
    $(`#search-form [name=kind][value=${l.kind === "album" ? "album" : "track"}]`).checked = true;
    return runSearch();
  }
  if (which === "best")
    await api("/api/requests", l.kind === "album" ? { kind: "album", artist, album: name } : { kind: "track", artist, title: name, album: l.album || "" });
  else if (which === "tracks") {
    const items = linkTracks().map((t) => ({ kind: "track", artist: t.artist, title: t.title }));
    for (let i = 0; i < items.length; i += 500)
      await api("/api/requests/import", { list: l.title.slice(0, 120) || "Requests", items: items.slice(i, i + 500) });
  } else await api("/api/requests", { kind: "link", url: l.url, link_kind: l.kind === "track" ? "track" : "album" });
  l.requested[which] = true;
  toast(which === "as-is" ? "Downloading the link as given." : "Requested. The app looks for the best copy.");
  renderLink();
}
function showPrecise(open) {
  $("#search-form .precise").hidden = !open;
  $("#precise-toggle").setAttribute("aria-expanded", open);
  $("#precise-toggle").textContent = open ? "One search box" : "Artist and title separately";
  if (!open) for (const name of ["artist", "title", "album"]) $(`#search-form [name=${name}]`).value = "";
}
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const query = $("#search-form [name=query]").value.trim();
  if (LINK.test(query)) return readLink(query);
  state.link = null;
  renderLink();
  busy(e.submitter || $("#search-form button[type=submit]"), runSearch);
});
function queryIsLink() {
  const link = LINK.test($("#search-form [name=query]").value.trim());
  $("#search-form button[type=submit]").textContent = link ? "Read link" : "Search";
  $("#search-form").classList.toggle("has-link", link);
  return link;
}
$("#search-form [name=query]").addEventListener("input", (e) => {
  // The separate fields name what a result is requested as; a new search in the box replaces them.
  if (!$("#search-form .precise").hidden) showPrecise(false);
  if (queryIsLink() && e.inputType === "insertFromPaste") readLink(e.target.value.trim());
});
$("#search-form").addEventListener("change", (e) => {
  if (e.target.name === "source" && e.target.value === "torrent")
    $("#search-form [name=kind][value=album]").checked = true;
  if (e.target.name === "kind" && e.target.value === "track" && $("#search-form [name=source][value=torrent]").checked)
    $("#search-form [name=source][value=all]").checked = true;
});
$("#precise-toggle").addEventListener("click", () => showPrecise($("#search-form .precise").hidden));
$("#link-card").addEventListener("change", (e) => {
  if (e.target.id === "link-all")
    document.querySelectorAll("[data-link-track]").forEach((box) => (box.checked = e.target.checked));
  if (e.target.id === "link-all" || e.target.dataset.linkTrack) {
    const selected = linkTracks().length;
    const button = $('#link-card [data-link="tracks"]');
    if (button) {
      button.textContent = `Find best copies of ${selected} track${selected === 1 ? "" : "s"}`;
      button.disabled = !selected;
    }
  }
});
$("#link-card").addEventListener("click", (e) => {
  const button = e.target.closest("button[data-link]");
  if (button) busy(button, () => linkAction(button));
});
document.addEventListener("change", (e) => {
  const fields = {
    "result-format": "searchFormat",
    "result-quality": "searchQuality",
  };
  if (fields[e.target.id]) {
    state[fields[e.target.id]] = e.target.value;
    renderResults({ results: state.results, status: state.searchStatus });
  }
});
document.addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  if (button.dataset.resultSort) {
    state.searchSort = button.dataset.resultSort;
    renderResults({ results: state.results, status: state.searchStatus });
  } else if (button.id === "clear-result-filters") {
    state.searchFormat = "";
    state.searchQuality = "";
    renderResults({ results: state.results, status: state.searchStatus });
  } else if (button.dataset.quick && !state.quick.has(button.dataset.quick))
    busy(button, async () => {
      const result = state.results.find((r) => r.id === button.dataset.quick) || {};
      const item = await api("/api/requests", { candidate_id: button.dataset.quick, quick: true, ...preciseFields() });
      state.quick.set(button.dataset.quick, {
        job: item.quick_job_id,
        at: Date.now(),
        label: result.title || result.album || result.filename || "Download",
      });
      state.queuedCandidates.add(button.dataset.quick);
      saveQuick();
      toast("Started. The files download here as soon as they arrive.");
      renderResults({ results: state.results, status: state.searchStatus });
    });
  else if (button.dataset.enqueue && !state.queuedCandidates.has(button.dataset.enqueue))
    busy(button, async () => {
      await api("/api/requests", { candidate_id: button.dataset.enqueue, ...preciseFields() });
      toast("Requested. This copy is tried first; if it fails, others are searched.");
      button.textContent = "Requested";
      button.dataset.queued = "true";
      state.queuedCandidates.add(button.dataset.enqueue);
    });
});
function preciseFields() {
  return Object.fromEntries(["artist", "title", "album"].map((name) => [name, $(`#search-form [name="${name}"]`).value]));
}
setInterval(() => {
  if (state.authenticated && state.quick.size) pollQuick().catch(() => {});
}, 3000);
views.search = {};
logoutHooks.push(() => {
  state.searchGeneration++;
  state.searchId = null;
  state.queuedCandidates.clear();
  state.results = [];
  state.link = null;
  state.searchFormat = "";
  state.searchQuality = "";
  $("#link-card").innerHTML = "";
  $("#results").innerHTML = "";
  $("#search-results").hidden = true;
});
