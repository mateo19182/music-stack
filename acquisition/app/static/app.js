"use strict";
const $ = (s) => document.querySelector(s);
const state = {
  tab: "search",
  searchId: null,
  searchGeneration: 0,
  page: 1,
  jobs: [],
  reviews: [],
  reviewDirty: false,
  reviewEdits: {},
  authenticated: false,
  isAdmin: false,
  results: [],
  searchSort: "relevance",
  searchFormat: "",
  searchQuality: "",
  searchStatus: "complete",
  libraryGeneration: 0,
  libraryFiles: {},
  analysisRunning: false,
  reviewSignature: "",
  reviewerConfigured: false,
  queuedCandidates: new Set(),
};
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const path = (v) => encodeURIComponent(String(v));
const bytes = (n) => (n ? `${(Number(n) / 1048576).toFixed(1)} MB` : "");
const analysisLabel = (value) =>
  typeof value === "string"
    ? value
    : Object.entries(value || {})
        .map(
          ([name, source]) =>
            `${name.toUpperCase()}: ${typeof source === "string" ? source : JSON.stringify(source)}`,
        )
        .join(" · ");
const sourceLabel = (source) =>
  source === "youtube" || source === "yt-dlp"
    ? "yt-dlp"
    : source === "soulseek"
      ? "Soulseek"
      : String(source || "");
const duration = (n) =>
  n
    ? `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, "0")}`
    : "";
const safeLink = (url) => {
  try {
    const u = new URL(url, location.origin);
    return ["http:", "https:"].includes(u.protocol) ? esc(u.href) : "#";
  } catch {
    return "#";
  }
};
let toastTimer;
function toast(message, error = false) {
  $("#toast").textContent = message;
  $("#toast").className = error ? "error" : "";
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 6000);
}
function loginView() {
  state.authenticated = false;
  state.isAdmin = false;
  $("#import-existing").hidden = true;
  $("#sharing-settings").hidden = true;
  $("#library-analysis").hidden = true;
  state.searchGeneration++;
  state.searchId = null;
  state.reviewDirty = false;
  state.reviewEdits = {};
  for (const key of Object.keys(reviewSelections)) delete reviewSelections[key];
  for (const store of [albumDrafts, reviewOpen, trackOpen, albumOverrides])
    for (const key of Object.keys(store)) delete store[key];
  state.reviews = [];
  state.reviewSignature = "";
  state.reviewerConfigured = false;
  state.queuedCandidates.clear();
  state.results = [];
  state.searchFormat = "";
  state.searchQuality = "";
  state.jobs = [];
  for (const id of ["results", "jobs", "reviews", "library"])
    $(`#${id}`).innerHTML = "";
  $("#app").hidden = true;
  $("#login-view").hidden = false;
  $("#player").pause();
  $("#player").removeAttribute("src");
  $("#player-bar").hidden = true;
}
async function api(url, body) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...(body !== undefined
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    if (response.status === 401) loginView();
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : typeof data.error === "string"
          ? data.error
          : `Request failed (${response.status})`,
    );
  }
  return data;
}
function formData(form) {
  return Object.fromEntries(new FormData(form));
}
async function busy(button, fn) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    await fn();
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = button.dataset.queued === "true";
  }
}
function showTab(tab) {
  if (["review", "inbox"].includes(tab)) tab = "activity";
  state.tab = tab;
  for (const name of ["search", "activity", "library"]) {
    $(`#${name}-view`).hidden = name !== tab;
    const navButton = $(`nav [data-tab="${name}"]`);
    navButton.classList.toggle("selected", name === tab);
    navButton.setAttribute("aria-current", name === tab ? "page" : "false");
  }
  if (tab === "activity")
    Promise.all([loadJobs(), loadReview()]).catch((e) =>
      toast(e.message, true),
    );
  if (tab === "library") {
    loadLibrary().catch((e) => toast(e.message, true));
    if (state.isAdmin) {
      loadSharing().catch(showSharingError);
      loadAnalysis().catch(() => {});
    }
  }
}
function meta(items) {
  return `<div class="meta">${items
    .filter((v) => v !== "" && v != null)
    .map((v) => `<span>${esc(v)}</span>`)
    .join("")}</div>`;
}
function downloads(file, editable = false) {
  return `<button class="quiet" data-preview="${esc(file.id)}" data-title="${esc(file.title || file.filename || "Track")}" data-artist="${esc(file.artist || "")}">Play</button><a href="/api/files/${path(file.id)}/download">Download</a><details class="more-actions"><summary aria-label="More actions for ${esc(file.title || "track")}">More</summary><div>${file.published && file.shared !== undefined ? (state.isAdmin ? `<label class="checkbox"><input type="checkbox" data-share-file="${esc(file.id)}" ${file.shared ? "checked" : ""}>Share on Soulseek</label><span class="error sharing-file-error" role="alert"></span>` : `<span class="muted">${file.shared ? "Selected for sharing" : "Private on Soulseek"}</span>`) : ""}${editable && state.isAdmin ? `<button class="quiet" data-edit-tags="${esc(file.id)}">Edit tags</button>` : ""}${file.album_id ? `<a href="/api/albums/${path(file.album_id)}/download">Album ZIP</a>` : ""}${file.navidrome_url ? `<a href="${safeLink(file.navidrome_url)}" target="_blank" rel="noopener">Open in Navidrome ↗</a>` : ""}<span class="muted">${esc([file.format, file.bitrate ? `${file.bitrate} kbps` : "", duration(file.duration), bytes(file.size)].filter(Boolean).join(" · "))}</span></div></details>`;
}
function resultSourceLink(result) {
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
function renderResults(data) {
  state.results = data.results || [];
  state.searchStatus = data.status || "complete";
  const formats = [
    ...new Set(
      state.results.flatMap(resultFiles).map(resultFormat).filter(Boolean),
    ),
  ].sort();
  if (state.searchFormat && !formats.includes(state.searchFormat))
    formats.push(state.searchFormat);
  const results = state.results.filter((result) =>
    resultFiles(result).every(
      (file) =>
        (!state.searchFormat || resultFormat(file) === state.searchFormat) &&
        matchesQuality(file, state.searchQuality),
    ),
  );
  const tools = `<div class="result-tools"><label>File type<select id="result-format"><option value="">All types</option>${formats.map((format) => `<option value="${esc(format)}">${esc(format.toUpperCase())}</option>`).join("")}</select></label><label>Quality<select id="result-quality"><option value="">Any quality</option><option value="lossless">Lossless formats</option><option value="128">128 kbps+ or lossless</option><option value="192">192 kbps+ or lossless</option><option value="256">256 kbps+ or lossless</option><option value="320">320 kbps+ or lossless</option><option value="unknown">Unknown quality</option></select></label><label>Order<select id="result-sort"><option value="relevance">Search order</option><option value="quality">Highest bitrate</option><option value="availability">Available first</option></select></label>${state.searchFormat || state.searchQuality ? '<button class="quiet" id="clear-result-filters">Clear filters</button>' : ""}<span class="muted">Reported quality. Unknown bitrates do not match a minimum.</span></div>`;
  if (state.searchSort === "quality")
    results.sort((a, b) => Number(b.bitrate || 0) - Number(a.bitrate || 0));
  if (state.searchSort === "availability")
    results.sort(
      (a, b) =>
        Number(b.free_slots > 0) - Number(a.free_slots > 0) ||
        Number(a.queue_length || 0) - Number(b.queue_length || 0),
    );
  $("#search-status").textContent =
    data.status === "failed"
      ? "Search failed"
      : `${results.length}${state.searchFormat || state.searchQuality ? ` of ${state.results.length}` : ""} result${state.results.length === 1 ? "" : "s"}${["pending", "queued", "running", "searching"].includes(data.status) ? " · Searching…" : ""}`;
  $("#results").innerHTML = results.length
    ? tools +
      `<div class="result-list">` +
      results
        .map(
          (r) =>
            `<article class="result-row"><div class="track-info"><h3>${esc(r.title || r.filename || r.album || "Untitled")}</h3>${meta([r.artist, r.album])}${searchLibraryHint(r)}${resultSourceLink(r)}<details class="source-details"><summary>${esc(sourceLabel(r.source))} · ${esc(r.username || r.uploader || r.provider || "Source details")}</summary>${meta([r.provider, r.queue_length != null ? `Queue ${r.queue_length}` : "", r.free_slots != null ? `${r.free_slots} free slots` : "", bytes(r.size)])}${r.folder_complete === false ? '<p class="muted">The full album folder is checked when queued.</p>' : ""}${r.files?.length ? `<ul class="file-list">${r.files.map((f) => `<li>${esc(f.filename)} ${esc(bytes(f.size))}</li>`).join("")}</ul>` : ""}</details></div><div class="quality">${meta([r.format, r.bitrate ? `${r.bitrate} kbps` : "", duration(r.duration)])}${r.files?.length ? `<span class="muted">${esc(r.file_count || r.files.length)} files</span>` : ""}</div><button class="quiet" data-enqueue="${esc(r.id)}" ${state.queuedCandidates.has(r.id) ? 'disabled data-queued="true"' : ""}>${state.queuedCandidates.has(r.id) ? "Queued" : "Queue"}</button></article>`,
        )
        .join("") +
      "</div>"
    : tools +
      `<div class="empty">${state.results.length ? "No results match these filters. Clear them to see all results." : ["pending", "queued", "running", "searching"].includes(data.status) ? "Searching available sources…" : "No results. Try an artist and title, or paste a link."}</div>`;
  $("#result-sort").value = state.searchSort;
  $("#result-format").value = state.searchFormat;
  $("#result-quality").value = state.searchQuality;
  if (data.error) toast(data.error, true);
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
async function loadJobs() {
  const data = await api("/api/jobs");
  state.jobs = data.jobs || [];
  const active = state.jobs.filter((j) =>
    [
      "queued",
      "downloading",
      "process_queued",
      "processing",
      "publish_queued",
      "publishing",
    ].includes(j.stage),
  ).length;
  $("#active-count").textContent = active ? `(${active})` : "";
  const visibleJobs = state.jobs.filter(
    (j) =>
      j.stage !== "review" &&
      ($("#show-history").checked ||
        !["published", "rejected", "cancelled"].includes(j.stage)),
  );
  $("#jobs").innerHTML = visibleJobs.length
    ? visibleJobs
        .map(
          (j) =>
            `<article class="card"><div class="card-head"><div><span class="badge ${esc(j.stage)}">${esc({ process_queued: "Waiting to process", publish_queued: "Waiting to publish", review: "Ready for review", published: "In library", queued: "Queued" }[j.stage] || j.stage.replaceAll("_", " "))}</span><h3>${esc(j.label || j.title || "Download")}</h3>${meta([sourceLabel(j.source), j.created_at ? new Date(j.created_at).toLocaleString() : ""])}</div></div>${["queued", "downloading", "process_queued", "processing", "publish_queued", "publishing"].includes(j.stage) ? `<progress ${Number.isFinite(Number(j.progress)) && j.progress != null ? `value="${Math.max(0, Math.min(100, Number(j.progress)))}" max="100"` : ""}></progress>` : ""}${j.detail ? `<p class="muted">${esc(typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail))}</p>` : ""}${j.error ? `<p class="error">${esc(j.error)}</p><p class="muted">${esc(j.resume_stage === "publishing" ? "Retry adding to the library. Your approved edits are saved." : j.failed_stage === "processing" || j.source === "existing" ? "The download is saved. Retry processing without downloading again." : "Retry this source to resume. If the uploader is unavailable, search for another copy.")}</p>` : ""}<div class="actions">${j.stage === "review" ? `<button data-tab="review">Review download</button>` : ["failed", "cancelled"].includes(j.stage) ? `<button data-retry="${esc(j.id)}" data-stage="${j.resume_stage === "publishing" ? "publishing" : j.failed_stage === "processing" || j.source === "existing" ? "processing" : "download"}">Retry ${j.resume_stage === "publishing" ? "publishing" : j.failed_stage === "processing" || j.source === "existing" ? "processing" : "download"}</button>` : ""}${["queued", "downloading", "process_queued", "processing"].includes(j.stage) ? `<button class="quiet" data-cancel="${esc(j.id)}">Cancel</button>` : ""}</div>${j.stage === "published" && j.skipped_count ? `<p class="muted">${j.skipped_count} unselected track${j.skipped_count === 1 ? "" : "s"} kept privately.</p>` : ""}${j.stage === "published" && (j.files || []).length ? `<details><summary>${j.files.length} published file${j.files.length === 1 ? "" : "s"}</summary>${j.files.map((f) => `<p class="muted">${esc(f.title || f.filename || "Track")}</p><div class="actions">${downloads(f)}</div>`).join("")}</details>` : ""}</article>`,
        )
        .join("")
    : '<div class="empty">No downloads in progress.</div>';
}
function filterOptions(name, values, invalidCount = 0) {
  const select = $(`#library-form [name="${name}"]`),
    selected = select.value;
  select.innerHTML =
    `<option value="">All ${name === "genre" ? "genres" : "keys"}</option>` +
    (values || [])
      .map((v) => `<option value="${esc(v)}">${esc(v)}</option>`)
      .join("") +
    (invalidCount
      ? `<option value="invalid">Not a key (${invalidCount})</option>`
      : "");
  select.value = selected;
}
const TAG_FIELDS = [
  "artist",
  "title",
  "album",
  "genre",
  "year",
  "mood",
  "bpm",
  "key",
];
function tagLabel(name) {
  return name === "bpm" ? "BPM" : name[0].toUpperCase() + name.slice(1);
}
function tagInputAttributes(name) {
  return {
    bpm: 'type="number" min="0" max="400" step="any"',
    year: 'type="number" min="1000" max="2100" step="1"',
    key: 'placeholder="8A or Am"',
    genre: 'placeholder="Separate with ;"',
    mood: 'placeholder="Separate with ;"',
  }[name] || "";
}
function keyLabel(file) {
  if (file.key_invalid) return `Key tag “${file.key_tag}” is not a key`;
  return file.key || "";
}
function tagEditor(file) {
  return `<form class="tag-editor" data-tag-form="${esc(file.id)}"><div class="review-fields">${TAG_FIELDS.map(
    (name) => {
      const value =
        name === "key"
          ? file.key || file.key_tag || ""
          : name === "genre"
            ? (file.genres || []).join("; ") || tagValue(file.genre)
            : tagValue(file[name]);
      return `<label>${tagLabel(name)}<input name="${name}" value="${esc(value)}" data-original="${esc(value)}" ${tagInputAttributes(name)}></label>`;
    },
  )
    .join(
      "",
    )}</div><p class="muted">Writes the tags into the file; the audio is unchanged. Changing the album tag does not move the file, and Navidrome groups albums by folder.</p><p class="error" role="alert"></p><div class="actions"><button type="submit">Save tags</button><button type="button" class="quiet" data-close-tags>Cancel</button></div></form>`;
}
async function loadLibrary() {
  const generation = ++state.libraryGeneration;
  const params = new URLSearchParams({
    ...formData($("#library-form")),
    page: String(state.page),
  });
  const data = await api(`/api/library?${params}`);
  if (generation !== state.libraryGeneration || !state.authenticated) return;
  $("#filter-summary").textContent = [
    "genre",
    "key",
    "bpm_min",
    "bpm_max",
  ].filter((name) => $(`#library-form [name="${name}"]`).value).length
    ? "· active"
    : "";
  filterOptions("genre", data.genres);
  filterOptions("key", data.keys, data.invalid_key_count);
  state.libraryFiles = Object.fromEntries(
    (data.files || []).map((f) => [f.id, f]),
  );
  $("#library-count").textContent = `${data.total || 0} files`;
  $("#library").innerHTML = (data.files || []).length
    ? `<div class="library-heading"><span>Track / artist</span><span>Album</span><span>Genre · year · mood · BPM · key</span><span></span></div>` +
      data.files
        .map(
          (f) =>
            `<article class="library-row"><div class="track-info"><h3>${esc(f.title || f.filename || "Untitled")}</h3><span class="muted">${esc(f.artist || "Unknown artist")}</span></div><span class="album-name muted">${esc(f.album || "")}</span><div class="music-tags${f.key_invalid ? " key-invalid" : ""}">${meta([(f.genres || []).join(", ") || f.genre, f.year, (f.mood || []).join(", "), f.bpm ? `${f.bpm} BPM` : "", keyLabel(f)])}</div><div class="actions">${downloads(f, true)}</div></article>`,
        )
        .join("")
    : '<div class="empty">No tracks match. Clear the filters to see your library.</div>';
  const pages = Math.max(
    1,
    Math.ceil((data.total || 0) / (data.page_size || 50)),
  );
  $("#pagination").innerHTML =
    `<button class="quiet" data-page="${state.page - 1}" ${state.page <= 1 ? "disabled" : ""}>Previous</button><span>Page ${state.page} of ${pages}</span><button class="quiet" data-page="${state.page + 1}" ${state.page >= pages ? "disabled" : ""}>Next</button>`;
}
$("#login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    $("#login-error").textContent = "";
    try {
      await api("/api/login", formData(e.target));
      e.target.reset();
      await start();
    } catch (error) {
      $("#login-error").textContent = error.message;
    }
  });
});
$("#logout").addEventListener("click", (e) =>
  busy(e.target, async () => {
    await api("/api/logout", {});
    loginView();
  }),
);
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    const input = formData(e.target);
    if (/^https?:\/\//i.test(input.query.trim())) {
      await api("/api/url", { url: input.query.trim(), kind: input.kind });
      toast("Link queued.");
      showTab("activity");
      return;
    }
    const generation = ++state.searchGeneration;
    $("#search-status").textContent = "Searching…";
    const data = await api("/api/search", input).catch((error) => {
      $("#search-status").textContent = "Search failed. Try again.";
      throw error;
    });
    state.searchId = data.id;
    renderResults(data);
    if (["pending", "queued", "running", "searching"].includes(data.status))
      setTimeout(() => pollSearch(generation), 1500);
  });
});
$("#url-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    await api("/api/url", formData(e.target));
    toast("URL queued.");
    showTab("activity");
  });
});
$("#library-form").addEventListener("submit", (e) => {
  e.preventDefault();
  state.page = 1;
  busy(e.submitter, loadLibrary);
});
$("#search-form [name=query]").addEventListener("input", (e) => {
  $("#search-form button[type=submit]").textContent = /^https?:\/\//i.test(
    e.target.value.trim(),
  )
    ? "Queue link"
    : "Search";
});
let libraryTimer;
$("#library-form").addEventListener("input", (e) => {
  if (e.target.name !== "q") return;
  clearTimeout(libraryTimer);
  libraryTimer = setTimeout(() => {
    state.page = 1;
    loadLibrary().catch((e) => toast(e.message, true));
  }, 350);
});
$("#library-form").addEventListener("change", (e) => {
  if (e.target.tagName !== "SELECT") return;
  clearTimeout(libraryTimer);
  state.page = 1;
  loadLibrary().catch((e) => toast(e.message, true));
});
document.addEventListener("keydown", (e) => {
  if (
    e.key !== "/" ||
    e.ctrlKey ||
    e.metaKey ||
    e.altKey ||
    /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) ||
    e.target.isContentEditable ||
    !state.authenticated
  )
    return;
  e.preventDefault();
  if (!["search", "library"].includes(state.tab)) showTab("search");
  $(
    state.tab === "library"
      ? "#library-form [name=q]"
      : "#search-form [name=query]",
  ).focus();
});
$("#reset-filters").addEventListener("click", () => {
  $("#library-form").reset();
  state.page = 1;
  loadLibrary().catch((e) => toast(e.message, true));
});
$("#show-history").addEventListener("change", () =>
  loadJobs().catch((e) => toast(e.message, true)),
);
document.addEventListener("change", (e) => {
  const fields = {
    "result-sort": "searchSort",
    "result-format": "searchFormat",
    "result-quality": "searchQuality",
  };
  if (fields[e.target.id]) {
    state[fields[e.target.id]] = e.target.value;
    renderResults({ results: state.results, status: state.searchStatus });
  }
});
$("#refresh-jobs").addEventListener("click", (e) =>
  busy(e.target, () => Promise.all([loadJobs(), loadReview()])),
);
document.addEventListener("click", (e) => {
  document.querySelectorAll(".more-actions[open]").forEach((menu) => {
    if (!menu.contains(e.target)) menu.open = false;
  });
  const button = e.target.closest("button");
  if (!button) return;
  if (button.id === "clear-result-filters") {
    state.searchFormat = "";
    state.searchQuality = "";
    renderResults({ results: state.results, status: state.searchStatus });
  } else if (button.dataset.tab) showTab(button.dataset.tab);
  else if (
    button.dataset.enqueue &&
    !state.queuedCandidates.has(button.dataset.enqueue)
  )
    busy(button, async () => {
      await api("/api/jobs", {
        candidate_id: button.dataset.enqueue,
        ...Object.fromEntries(
          ["artist", "title", "album"].map((name) => [
            name,
            $(`#search-form [name="${name}"]`).value,
          ]),
        ),
      });
      toast("Download queued.");
      button.textContent = "Queued";
      button.dataset.queued = "true";
      state.queuedCandidates.add(button.dataset.enqueue);
      await loadJobs();
    });
  else if (button.dataset.retry)
    busy(button, async () => {
      await api(`/api/jobs/${path(button.dataset.retry)}/retry`, {
        stage: button.dataset.stage,
      });
      await loadJobs();
    });
  else if (button.dataset.cancel)
    busy(button, async () => {
      await api(`/api/jobs/${path(button.dataset.cancel)}/cancel`, {});
      await loadJobs();
    });
  else if (button.dataset.editTags) {
    const row = button.closest(".library-row");
    button.closest(".more-actions").open = false;
    if (row.nextElementSibling?.dataset.tagForm) {
      row.nextElementSibling.querySelector("input").focus();
      return;
    }
    row.insertAdjacentHTML(
      "afterend",
      tagEditor(state.libraryFiles[button.dataset.editTags]),
    );
    row.nextElementSibling.querySelector("input").focus();
  } else if (button.dataset.closeTags !== undefined)
    button.closest("form").remove();
  else if (button.id === "start-analysis")
    busy(button, async () => {
      await api("/api/library/analysis", {});
      toast("Analyzing library files with missing tags.");
      await loadAnalysis();
    });
  else if (button.id === "cancel-analysis")
    busy(button, async () => {
      await api("/api/library/analysis/cancel", {});
      await loadAnalysis();
    });
  else if (button.dataset.requestAdvice)
    busy(button, async () => {
      await api(`/api/jobs/${path(button.dataset.requestAdvice)}/advice`, {});
      state.reviewSignature = "";
      document.activeElement?.blur();
      await loadReview();
      toast("Review advice requested. Final approval is yours.");
    });
  else if (button.dataset.approve)
    busy(button, () =>
      approveReview(button.dataset.approve, button.dataset.keep === "true"),
    );
  else if (button.dataset.reject)
    busy(button, async () => {
      await api(`/api/jobs/${path(button.dataset.reject)}/reject`, {});
      toast("Rejected. Source files retained.");
      clearReviewEdits(button.dataset.reject);
      await loadReview();
      await loadJobs();
    });
  else if (button.dataset.preview) {
    const player = $("#player");
    player.src = `/api/files/${path(button.dataset.preview)}/preview`;
    $("#player-bar").hidden = false;
    $("#player-title").textContent =
      button.dataset.title ||
      button.closest("article, section")?.querySelector("h3")?.textContent ||
      "Preview";
    $("#player-subtitle").textContent = button.dataset.artist || "";
    player.play().catch(() => toast("Press play to preview this file."));
  } else if (button.dataset.page) {
    state.page = Number(button.dataset.page);
    busy(button, async () => {
      await loadLibrary();
      $("#library-view").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
});
$("#close-player").addEventListener("click", () => {
  $("#player").pause();
  $("#player").removeAttribute("src");
  $("#player").load();
  $("#player-bar").hidden = true;
});
$("#player").addEventListener("error", () =>
  toast(
    "Audio preview could not be played. You can download the original file.",
    true,
  ),
);
async function start() {
  const user = await api("/api/me");
  state.authenticated = true;
  state.isAdmin = Boolean(user.is_admin ?? user.isAdmin);
  $("#import-existing").hidden = !state.isAdmin;
  $("#sharing-settings").hidden = !state.isAdmin;
  $("#library-analysis").hidden = !state.isAdmin;
  if (user.navidromeUrl) $("#listen").href = safeLink(user.navidromeUrl);
  $("#username").textContent = user.username;
  $("#login-view").hidden = true;
  $("#app").hidden = false;
  showTab("search");
  const results = await Promise.allSettled([loadJobs(), loadReview()]);
  for (const result of results)
    if (result.status === "rejected") toast(result.reason.message, true);
}
start().catch(() => loginView());
setInterval(() => {
  if (state.authenticated) {
    if (
      state.tab === "library" &&
      state.isAdmin &&
      !$("#sharing-enabled").disabled
    )
      loadSharing().catch(showSharingError);
    if (state.tab === "library" && state.analysisRunning)
      loadAnalysis()
        .then(() => state.analysisRunning || loadLibrary())
        .catch(() => {});
    loadReview().catch(() => {});
    loadJobs().catch((e) => {
      if (state.tab === "activity") toast(e.message, true);
    });
  }
}, 5000);

function tagValue(value) {
  return value == null
    ? ""
    : Array.isArray(value)
      ? value.join(", ")
      : String(value);
}
async function loadReview() {
  const data = await api("/api/review");
  state.reviews = data.jobs || [];
  state.reviewerConfigured = !!data.reviewer?.configured;
  $("#review-count").textContent = state.reviews.length
    ? `(${state.reviews.length})`
    : "";
  const signature = JSON.stringify(state.reviews);
  if (
    document.activeElement?.closest("#reviews") ||
    signature === state.reviewSignature
  )
    return;
  state.reviewSignature = signature;
  $("#reviews").innerHTML = state.reviews.length
    ? state.reviews.map(renderReviewJob).join("")
    : '<div class="empty">Nothing ready to add yet.</div>';
  for (const job of document.querySelectorAll("[data-review-job]"))
    updateReviewSelection(job);
}
// Review choices that survive the five-second refresh.
const reviewSelections = {};
const albumDrafts = {};
const reviewOpen = {};
const trackOpen = {};
const albumOverrides = {};
const ALBUM_FIELDS = ["artist", "album", "genre", "year"];
const STATUS_LABELS = { ready: "Ready", check: "Check", duplicate: "In library" };
function reviewValue(file, name) {
  return tagValue(
    state.reviewEdits[file.id]?.[name] ??
      (file.proposed || file.proposed_tags)?.[name] ??
      file[name],
  );
}
function catalogRecommendation(value) {
  return (
    {
      strong: "strong match",
      medium: "possible match",
      alternative: "alternative match",
    }[String(value || "").toLowerCase()] || "weak match"
  );
}
function trackReview(file, advice) {
  const innocuous = new Set(advice?.innocuous_warnings || []);
  const warnings = file.warnings || [];
  const reasons = warnings.filter((warning) => !innocuous.has(warning));
  if (advice?.status === "check" && advice.reason) reasons.push(advice.reason);
  if (file.possible_duplicates?.length)
    reasons.push("A different version is already in your library. Compare them below.");
  for (const name of ["artist", "title"])
    if (!reviewValue(file, name).trim()) reasons.push(`No ${name}.`);
  const duplicate = Boolean(file.duplicate_file_id || file.duplicate_of);
  return {
    status: duplicate ? "duplicate" : reasons.length ? "check" : "ready",
    reasons,
    notes: warnings.filter((warning) => innocuous.has(warning)),
  };
}
function fieldSuggestions(file, advice, name) {
  const current = reviewValue(file, name);
  const found = [];
  const add = (value, source) => {
    value = tagValue(value).trim();
    if (value && value !== current && !found.some((s) => s.value === value))
      found.push({ value, source });
  };
  if (["artist", "title", "album", "genre"].includes(name))
    add(advice?.suggested_tags?.[name], "AI advice");
  if (name === "artist" || name === "title")
    for (const match of (file.catalog_matches || []).slice(0, 3))
      add(match[name], `MusicBrainz, ${catalogRecommendation(match.recommendation)}`);
  return found.slice(0, 2);
}
function playButton(id, title, artist, label = "▶ Play") {
  return `<button type="button" class="quiet play" data-preview="${esc(id)}" data-title="${esc(title)}" data-artist="${esc(artist || "")}">${label}</button>`;
}
function reviewField(file, advice, name, originals) {
  const old = tagValue(originals[name]);
  const value = reviewValue(file, name);
  return `<div class="review-field"><label>${tagLabel(name)}<span class="field-input"><input data-field="${name}" data-original="${esc(old)}" value="${esc(value)}" ${tagInputAttributes(name)} class="${old !== value ? "changed" : ""}" aria-label="${esc(tagLabel(name))} for ${esc(file.title || "track")}"><button type="button" class="reset-field" data-reset-field title="Restore the file's original value: ${esc(old || "empty")}" aria-label="Restore original ${esc(name)}" ${old === value ? "hidden" : ""}>↺</button></span></label>${fieldSuggestions(
    file,
    advice,
    name,
  )
    .map(
      (s) =>
        `<span class="suggestion">Suggested: <strong>${esc(s.value)}</strong> <span class="muted">${esc(s.source)}</span> <button type="button" class="link" data-use-value="${esc(s.value)}">Use</button></span>`,
    )
    .join("")}</div>`;
}
function renderReviewJob(job) {
  const files = [...(job.files || [])];
  if (files.length > 1 && files.every((file) => Number(file.track_number) > 0))
    files.sort((a, b) => (Number(a.disc_number) || 1) - (Number(b.disc_number) || 1) || Number(a.track_number) - Number(b.track_number));
  const multi = files.length > 1;
  const adviceFor = (file) =>
    job.advice?.result?.files?.find((entry) => entry.id === file.id);
  const reviews = files.map((file) => trackReview(file, adviceFor(file)));
  const toCheck = reviews.filter((review) => review.status === "check").length;
  const summary = job.review_summary;
  const status = summary?.status === "check" || toCheck ? "check" : "ready";
  const open = reviewOpen[job.id] ?? state.reviews.length <= 3;
  const albumFields = multi
    ? `<div class="album-fields">${ALBUM_FIELDS.map((name) => {
        const values = [...new Set(files.map((file) => reviewValue(file, name)))];
        const value = albumDrafts[job.id]?.[name] ?? (values.length === 1 ? values[0] : "");
        return `<label>${tagLabel(name)}<input data-album-field="${name}" value="${esc(value)}" ${tagInputAttributes(name)} placeholder="${values.length > 1 ? "Mixed: typing sets every track" : ""}"></label>`;
      }).join("")}</div>`
    : "";
  return `<article class="card review-card" data-review-job="${esc(job.id)}"><details class="review-disclosure" ${open ? "open" : ""}><summary><span class="review-title"><strong>${esc(job.label || "Download")}</strong><span class="muted">${esc([sourceLabel(job.source), `${files.length} track${multi ? "s" : ""}`].filter(Boolean).join(" · "))}</span></span>${adviceBadge(job)}<span class="status status-${status}">${status === "check" ? (toCheck ? `${toCheck} to check` : "Check") : "Ready"}</span></summary>${renderConcerns(job, summary)}${albumFields}<div class="track-list">${files
    .map((file, index) => renderReviewFile(file, index, adviceFor(file), reviews[index], multi))
    .join("")}</div><div class="review-bar"><label class="checkbox" ${multi ? "" : "hidden"}><input type="checkbox" data-select-all>Include all</label><span class="muted" data-selected-count></span><details class="more-actions"><summary aria-label="More review actions">⋯</summary><div><button class="quiet" data-approve="${esc(job.id)}" data-keep="true">Add with original tags</button>${adviceAction(job)}</div></details><button class="quiet" data-reject="${esc(job.id)}" title="Nothing is added. The download is kept privately.">Reject</button><button data-approve="${esc(job.id)}">Add</button></div></details></article>`;
}
function adviceBadge(job) {
  const advice = job.advice;
  if (advice?.status === "complete")
    return `<span class="ai-badge" title="${esc(advice.result?.summary || "")}">AI: ${esc({ looks_fine: "looks fine", check: "check", skip_duplicate: "already in library" }[advice.result?.status] || "done")}</span>`;
  if (advice?.status === "queued" || advice?.status === "running")
    return '<span class="ai-badge" role="status">AI checking…</span>';
  if (advice?.status === "failed")
    return `<span class="ai-badge" title="${esc(advice.error || "")}">AI unavailable</span>`;
  return "";
}
function adviceAction(job) {
  const status = job.advice?.status;
  if (!state.reviewerConfigured || status === "queued" || status === "running")
    return "";
  return `<button class="quiet" data-request-advice="${esc(job.id)}">${status === "failed" ? "Retry AI advice" : status === "complete" ? "Ask AI again" : "Ask AI to check"}</button>`;
}
function renderConcerns(job, summary) {
  const items = summary?.concerns || [];
  const notes = [summary?.completeness, ...(summary?.notes || [])].filter(Boolean);
  const advice = job.advice?.status === "complete" && job.advice.result?.status !== "check" ? job.advice.result?.summary : "";
  if (!items.length && !notes.length && !advice) return "";
  return `<div class="concerns">${items.length ? `<ul>${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}${advice ? `<p class="muted">AI: ${esc(advice)}</p>` : ""}${notes.map((note) => `<p class="muted">${esc(note)}</p>`).join("")}</div>`;
}
function renderReviewFile(file, index, advice, review, multi) {
  const selected = reviewSelections[file.id] ?? !(multi && review.status === "duplicate");
  const open = trackOpen[file.id] ?? (review.status === "check" || !multi);
  const originals = file.existing || file.existing_tags || {};
  const title = reviewValue(file, "title") || file.filename || "Track";
  const artist = reviewValue(file, "artist");
  const number = file.track_number
    ? `${file.disc_number > 1 ? `${file.disc_number}-` : ""}${file.track_number}`
    : index + 1;
  const duplicates = file.possible_duplicates || [];
  const fileDetails = [file.format, file.bitrate ? `${file.bitrate} kbps` : "", duration(file.duration)].filter(Boolean).join(" · ");
  return `<section class="review-file ${selected ? "" : "track-unselected"} ${open ? "open" : ""}" data-review-file="${esc(file.id)}"><div class="track-row"><input type="checkbox" data-select-file="${esc(file.id)}" ${selected ? "checked" : ""} ${multi ? "" : "hidden"} title="Include this track" aria-label="Include ${esc(title)}"><button type="button" class="track-toggle" data-toggle-track aria-expanded="${open}"><span class="track-number">${esc(number)}</span><span class="track-name"><strong data-track-title>${esc(title)}</strong><span class="muted">${esc([artist, fileDetails].filter(Boolean).join(" · "))}</span></span></button>${playButton(file.id, title, artist)}<span class="status status-${review.status}">${STATUS_LABELS[review.status]}</span></div>${review.status === "check" ? `<ul class="track-reasons">${review.reasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}</ul>` : ""}${review.status === "duplicate" ? `<p class="track-note">Same audio as <strong>${esc(typeof file.duplicate_of === "string" ? file.duplicate_of : "a library track")}</strong>. Adding it reuses the existing file and tags.${file.duplicate_file_id ? ` ${playButton(file.duplicate_file_id, file.duplicate_of || "Library version", artist, "▶ Play yours")}` : ""}</p>` : ""}<div class="track-details"><div class="review-fields">${TAG_FIELDS.map((name) => reviewField(file, advice, name, originals)).join("")}</div>${duplicates.length ? `<div class="compare"><div><span class="muted">This download</span><span>${esc(fileDetails || "Unknown format")}</span>${playButton(file.id, title, artist)}</div>${duplicates.map((d) => `<div><span class="muted">In your library</span><span>${esc([d.title, d.album, d.format, d.bitrate ? `${d.bitrate} kbps` : "", duration(d.duration)].filter(Boolean).join(" · "))}</span>${d.id ? playButton(d.id, [d.title, d.album].filter(Boolean).join(" · ") || "Library version", d.artist, "▶ Play yours") : ""}</div>`).join("")}</div>` : ""}<p class="track-foot muted">${esc([file.filename, bytes(file.size), file.analysis_source ? `Analysis: ${analysisLabel(file.analysis_source)}` : "", advice?.status !== "check" ? advice?.reason : "", ...review.notes].filter(Boolean).join(" · "))} <a href="/api/files/${path(file.id)}/download">Download file</a></p></div></section>`;
}
function updateReviewSelection(container) {
  const inputs = [...container.querySelectorAll("[data-select-file]")];
  const count = inputs.filter((input) => input.checked).length;
  const all = container.querySelector("[data-select-all]");
  all.checked = count === inputs.length;
  all.indeterminate = count > 0 && count < inputs.length;
  container.querySelector("[data-selected-count]").textContent =
    inputs.length > 1 ? `${count} of ${inputs.length} included` : "";
  container
    .querySelectorAll("[data-approve]")
    .forEach((button) => (button.disabled = count === 0));
  container.querySelector("[data-approve]:not([data-keep])").textContent =
    inputs.length === 1 ? "Add to library" : `Add ${count} track${count === 1 ? "" : "s"}`;
  inputs.forEach((input) =>
    input
      .closest("[data-review-file]")
      .classList.toggle("track-unselected", !input.checked),
  );
}
async function approveReview(id, keep) {
  const container = [...document.querySelectorAll("[data-review-job]")].find(
    (el) => el.dataset.reviewJob === id,
  );
  const selected = [...container.querySelectorAll("[data-review-file]")].filter(
    (el) => el.querySelector("[data-select-file]").checked,
  );
  if (!selected.length) throw new Error("Include at least one track.");
  const files = selected.map((el) => ({
    id: el.dataset.reviewFile,
    metadata: Object.fromEntries(
      [...el.querySelectorAll("[data-field]")].map((input) => [
        input.dataset.field,
        ["bpm", "year"].includes(input.dataset.field)
          ? input.value === ""
            ? null
            : Number(input.value)
          : input.value,
      ]),
    ),
  }));
  await api(`/api/jobs/${path(id)}/approve`, {
    files,
    selected_file_ids: files.map((file) => file.id),
    keep_existing: keep,
  });
  toast(`Adding ${files.length} track${files.length === 1 ? "" : "s"} to the library.`);
  clearReviewEdits(id);
  await loadReview();
  await loadJobs();
}
function clearReviewEdits(id) {
  for (const file of state.reviews.find((j) => j.id === id)?.files || []) {
    delete state.reviewEdits[file.id];
    delete reviewSelections[file.id];
    delete trackOpen[file.id];
    delete albumOverrides[file.id];
  }
  delete albumDrafts[id];
  delete reviewOpen[id];
  state.reviewDirty = Object.keys(state.reviewEdits).length > 0;
  state.reviewSignature = "";
  if (
    document.activeElement?.closest("[data-review-job]")?.dataset.reviewJob ===
    id
  )
    document.activeElement.blur();
}
function syncReviewField(input) {
  const section = input.closest("[data-review-file]");
  state.reviewDirty = true;
  (state.reviewEdits[section.dataset.reviewFile] ??= {})[input.dataset.field] =
    input.value;
  const changed = input.value !== input.dataset.original;
  input.classList.toggle("changed", changed);
  input.parentElement.querySelector("[data-reset-field]").hidden = !changed;
  if (input.dataset.field === "title")
    section.querySelector("[data-track-title]").textContent =
      input.value || "Track";
}
$("#reviews").addEventListener("input", (e) => {
  const job = e.target.closest("[data-review-job]");
  const field = e.target.dataset.albumField;
  if (field && job) {
    // Album values follow every track except fields edited on the track itself.
    (albumDrafts[job.dataset.reviewJob] ??= {})[field] = e.target.value;
    job.querySelectorAll("[data-review-file]").forEach((section) => {
      if (albumOverrides[section.dataset.reviewFile]?.has(field)) return;
      const input = section.querySelector(`[data-field="${field}"]`);
      input.value = e.target.value;
      syncReviewField(input);
    });
    return;
  }
  const file = e.target.closest("[data-review-file]");
  if (!file || !e.target.dataset.field) return;
  (albumOverrides[file.dataset.reviewFile] ??= new Set()).add(e.target.dataset.field);
  syncReviewField(e.target);
});
$("#reviews").addEventListener("change", (e) => {
  const job = e.target.closest("[data-review-job]");
  if (!job) return;
  if (e.target.hasAttribute("data-select-all"))
    job.querySelectorAll("[data-select-file]").forEach((input) => {
      input.checked = e.target.checked;
      reviewSelections[input.dataset.selectFile] = input.checked;
    });
  else if (e.target.dataset.selectFile)
    reviewSelections[e.target.dataset.selectFile] = e.target.checked;
  else return;
  updateReviewSelection(job);
});
$("#reviews").addEventListener(
  "toggle",
  (e) => {
    if (e.target.classList.contains("review-disclosure"))
      reviewOpen[e.target.closest("[data-review-job]").dataset.reviewJob] =
        e.target.open;
  },
  true,
);
$("#reviews").addEventListener("click", (e) => {
  const button = e.target.closest("button");
  const section = button?.closest("[data-review-file]");
  if (!section) return;
  const id = section.dataset.reviewFile;
  if (button.hasAttribute("data-toggle-track")) {
    const open = !section.classList.contains("open");
    trackOpen[id] = open;
    section.classList.toggle("open", open);
    button.setAttribute("aria-expanded", open);
    return;
  }
  const input = button.closest(".review-field")?.querySelector("[data-field]");
  if (!input) return;
  if (button.hasAttribute("data-reset-field")) {
    input.value = input.dataset.original;
    albumOverrides[id]?.delete(input.dataset.field);
  } else if (button.dataset.useValue !== undefined) {
    input.value = button.dataset.useValue;
    (albumOverrides[id] ??= new Set()).add(input.dataset.field);
    button.closest(".suggestion").remove();
  } else return;
  syncReviewField(input);
  input.focus();
});

async function loadInbox() {
  if (!state.isAdmin) return;
  const data = await api("/api/inbox");
  $("#inbox-count").textContent =
    `${data.total ?? data.files?.length ?? 0} completed files`;
  $("#inbox-all").checked = false;
  $("#inbox").innerHTML = data.files?.length
    ? data.files
        .map(
          (f) =>
            `<label class="inbox-file"><input type="checkbox" name="paths" value="${esc(f.path)}"><span><strong>${esc(f.name)}</strong><small>${esc(f.path)}</small></span><span class="muted">${esc(bytes(f.size))}</span></label>`,
        )
        .join("")
    : '<div class="empty">No other completed files available.</div>';
}
$("#inbox-all").addEventListener("change", (e) => {
  document.querySelectorAll('#inbox [name="paths"]').forEach((input, index) => {
    input.checked = e.target.checked && index < 100;
  });
});
$("#refresh-inbox").addEventListener("click", (e) => busy(e.target, loadInbox));
$("#inbox-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    const paths = Array.from(
      document.querySelectorAll('#inbox [name="paths"]:checked'),
    ).map((input) => input.value);
    if (!paths.length) throw new Error("Select at least one file.");
    if (paths.length > 100)
      throw new Error("Select up to 100 files at a time.");
    await api("/api/import", { paths });
    toast("Selected files queued for preparation.");
    showTab("activity");
  });
});

function showSharingError(error) {
  $("#sharing-error").textContent = error.message;
}
async function loadSharing() {
  if (!state.isAdmin) return;
  const data = await api("/api/sharing");
  $("#sharing-enabled").checked = data.enabled;
  $("#sharing-error").textContent = data.error || "";
  $("#sharing-status").textContent =
    `${data.enabled ? "Sharing enabled" : "Sharing paused"}. ${data.shared_count ?? "All eligible"} files in the shared directory. ${data.excluded_count || 0} private track${data.excluded_count === 1 ? "" : "s"}.${data.scan_pending ? " Soulseek is updating its file list." : ""}`;
}
$("#sharing-enabled").addEventListener("change", async (e) => {
  const input = e.target;
  input.disabled = true;
  $("#sharing-error").textContent = "";
  try {
    await api("/api/sharing", { enabled: input.checked });
    await loadSharing();
    await loadLibrary();
  } catch (error) {
    input.checked = !input.checked;
    showSharingError(error);
  } finally {
    input.disabled = false;
  }
});
document.addEventListener("change", async (e) => {
  const input = e.target;
  if (!input.dataset.shareFile) return;
  const errorElement = input
    .closest(".more-actions")
    .querySelector(".sharing-file-error");
  errorElement.textContent = "";
  input.disabled = true;
  try {
    await api(`/api/files/${path(input.dataset.shareFile)}/sharing`, {
      shared: input.checked,
    });
    await loadSharing();
  } catch (error) {
    input.checked = !input.checked;
    errorElement.textContent = error.message;
  } finally {
    input.disabled = false;
  }
});
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!form.dataset.tagForm) return;
  e.preventDefault();
  const changes = Object.fromEntries(
    [...form.querySelectorAll("input")]
      .filter((input) => input.value !== input.dataset.original)
      .map((input) => [
        input.name,
        ["bpm", "year"].includes(input.name)
          ? input.value === ""
            ? null
            : Number(input.value)
          : input.value,
      ]),
  );
  if (!Object.keys(changes).length) {
    form.remove();
    return;
  }
  busy(e.submitter, async () => {
    form.querySelector(".error").textContent = "";
    try {
      const result = await api(
        `/api/files/${path(form.dataset.tagForm)}/tags`,
        changes,
      );
      toast(result.detail || "Tags saved.");
      await loadLibrary();
    } catch (error) {
      form.querySelector(".error").textContent = error.message;
    }
  });
});
async function loadAnalysis() {
  if (!state.isAdmin) return;
  const data = await api("/api/library/analysis");
  state.analysisRunning = data.status === "running";
  $("#start-analysis").hidden = state.analysisRunning;
  $("#cancel-analysis").hidden = !state.analysisRunning;
  const counts = `Added ${data.bpm_added || 0} BPM, ${data.key_added || 0} keys, ${data.genre_added || 0} genres, ${data.year_added || 0} years and ${data.mood_added || 0} moods; rewrote ${data.key_normalized || 0} keys as Camelot and ${data.genre_normalized || 0} genre spellings${data.audio_models === false ? "; audio models not installed, so no moods" : ""}${data.long_recordings ? `, ${data.long_recordings} long recordings skipped` : ""}${data.no_estimate ? `, ${data.no_estimate} without a confident estimate` : ""}${data.failed ? `, ${data.failed} failed` : ""}.`;
  $("#analysis-status").textContent =
    data.status === "running"
      ? `Checking ${data.checked || 0} of ${data.total || 0} tracks. ${counts}`
      : data.status === "idle"
        ? "Not run yet."
        : `Last run ${data.status}${data.finished_at ? ` ${new Date(data.finished_at).toLocaleString()}` : ""}: ${data.checked || 0} of ${data.total || 0} tracks checked. ${counts} ${data.detail || ""}`;
}
$("#library-analysis").addEventListener("toggle", (e) => {
  if (e.target.open) loadAnalysis().catch((error) => toast(error.message, true));
});
$("#import-existing").addEventListener("toggle", (e) => {
  if (e.target.open) loadInbox().catch((error) => toast(error.message, true));
});
