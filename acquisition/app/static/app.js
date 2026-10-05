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
  state.searchGeneration++;
  state.searchId = null;
  state.reviewDirty = false;
  state.reviewEdits = {};
  for (const key of Object.keys(reviewSelections)) delete reviewSelections[key];
  for (const key of Object.keys(albumDrafts)) delete albumDrafts[key];
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
    if (state.isAdmin) loadSharing().catch(showSharingError);
  }
}
function meta(items) {
  return `<div class="meta">${items
    .filter((v) => v !== "" && v != null)
    .map((v) => `<span>${esc(v)}</span>`)
    .join("")}</div>`;
}
function downloads(file) {
  return `<button class="quiet" data-preview="${esc(file.id)}" data-title="${esc(file.title || file.filename || "Track")}" data-artist="${esc(file.artist || "")}">Play</button><a href="/api/files/${path(file.id)}/download">Download</a><details class="more-actions"><summary aria-label="More actions for ${esc(file.title || "track")}">More</summary><div>${file.published && file.shared !== undefined ? (state.isAdmin ? `<label class="checkbox"><input type="checkbox" data-share-file="${esc(file.id)}" ${file.shared ? "checked" : ""}>Share on Soulseek</label><span class="error sharing-file-error" role="alert"></span>` : `<span class="muted">${file.shared ? "Selected for sharing" : "Private on Soulseek"}</span>`) : ""}${file.album_id ? `<a href="/api/albums/${path(file.album_id)}/download">Album ZIP</a>` : ""}${file.navidrome_url ? `<a href="${safeLink(file.navidrome_url)}" target="_blank" rel="noopener">Open in Navidrome ↗</a>` : ""}<span class="muted">${esc([file.format, file.bitrate ? `${file.bitrate} kbps` : "", duration(file.duration), bytes(file.size)].filter(Boolean).join(" · "))}</span></div></details>`;
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
function filterOptions(name, values) {
  const select = $(`#library-form [name="${name}"]`),
    selected = select.value;
  select.innerHTML =
    `<option value="">All ${name === "genre" ? "genres" : "keys"}</option>` +
    (values || [])
      .map((v) => `<option value="${esc(v)}">${esc(v)}</option>`)
      .join("");
  select.value = selected;
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
  filterOptions("key", data.keys);
  $("#library-count").textContent = `${data.total || 0} files`;
  $("#library").innerHTML = (data.files || []).length
    ? `<div class="library-heading"><span>Track / artist</span><span>Album</span><span>Genre · BPM · key</span><span></span></div>` +
      data.files
        .map(
          (f) =>
            `<article class="library-row"><div class="track-info"><h3>${esc(f.title || f.filename || "Untitled")}</h3><span class="muted">${esc(f.artist || "Unknown artist")}</span></div><span class="album-name muted">${esc(f.album || "")}</span><div class="music-tags">${meta([f.genre, f.bpm ? `${f.bpm} BPM` : "", f.key])}</div><div class="actions">${downloads(f)}</div></article>`,
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
  else if (button.dataset.suggestionFile)
    useSuggestion(
      button.dataset.suggestionFile,
      Number(button.dataset.suggestionIndex),
    );
  else if (button.dataset.requestAdvice)
    busy(button, async () => {
      await api(`/api/jobs/${path(button.dataset.requestAdvice)}/advice`, {});
      state.reviewSignature = "";
      document.activeElement?.blur();
      await loadReview();
      toast("Review advice requested. Final approval is yours.");
    });
  else if (button.dataset.applyAdvice) applyAdvice(button.dataset.applyAdvice);
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
  const expanded = new Set(
    Array.from(
      document.querySelectorAll(
        "[data-review-job]:has(.review-disclosure[open])",
      ),
    ).map((el) => el.dataset.reviewJob),
  );
  state.reviewSignature = signature;
  state.expandedReviews = expanded;
  $("#reviews").innerHTML = state.reviews.length
    ? state.reviews.map(renderReviewJob).join("")
    : '<div class="empty">Nothing ready to add yet.</div>';
  for (const job of document.querySelectorAll("[data-review-job]"))
    updateReviewSelection(job);
}
const reviewSelections = {};
const albumDrafts = {};
function reviewValue(file, name) {
  return tagValue(
    state.reviewEdits[file.id]?.[name] ??
      (file.proposed || file.proposed_tags)?.[name] ??
      file[name],
  );
}
function adviceLabel(status) {
  return (
    {
      looks_fine: "Looks fine",
      check: "Check before adding",
      skip_duplicate: "Already in your library",
    }[status] || "Review advice"
  );
}
function renderJobAdvice(job) {
  const advice = job.advice;
  if (advice?.status === "complete")
    return `<details class="review-advice"><summary>AI advice · ${esc(adviceLabel(advice.result?.status))}</summary><p>${esc(advice.result?.summary)}</p><span class="muted">Based on metadata, not listening. You decide what to add.</span></details>`;
  if (advice?.status === "queued" || advice?.status === "running")
    return '<p class="muted" role="status">Checking the metadata with OpenRouter… You can still review and add tracks yourself.</p>';
  if (advice?.status === "failed")
    return `<div class="review-advice"><p class="muted">${esc(advice.error || "Advice is unavailable. Manual review still works.")}</p><button class="quiet" data-request-advice="${esc(job.id)}">Retry advice</button></div>`;
  return state.reviewerConfigured
    ? `<div class="actions"><button class="quiet" data-request-advice="${esc(job.id)}">Get review advice</button><span class="muted">Final approval stays manual.</span></div>`
    : "";
}
function renderFileAdvice(file, advice) {
  if (!advice) return "";
  const tags = Object.entries(advice.suggested_tags || {}).filter(
    ([name, value]) => value !== reviewValue(file, name),
  );
  return `<details class="file-advice" ${advice.status === "check" ? "open" : ""}><summary>${esc(adviceLabel(advice.status))}${tags.length ? " · Suggested edits" : ""}</summary><p>${esc(advice.reason)}</p>${tags.length ? `<details><summary>Suggested tags</summary><dl>${tags.map(([name, value]) => `<dt>${esc(name)}</dt><dd>${esc(value || "Empty")}</dd>`).join("")}</dl><button class="quiet" data-apply-advice="${esc(file.id)}">Use suggested tags</button><p class="muted">Fills the form for your review. Does not add the track.</p></details>` : ""}</details>`;
}
function renderReviewWarnings(file, advice) {
  const innocuous = new Set(advice?.innocuous_warnings || []);
  const warnings = file.warnings || [];
  const small = warnings.filter((warning) => innocuous.has(warning));
  return (
    warnings
      .filter((warning) => !innocuous.has(warning))
      .map((warning) => `<p class="warning">${esc(warning)}</p>`)
      .join("") +
    (small.length
      ? `<details class="original-tags"><summary>${small.length} minor metadata note${small.length === 1 ? "" : "s"}</summary>${small.map((warning) => `<p class="muted">${esc(warning)}</p>`).join("")}</details>`
      : "")
  );
}
function applyAdvice(id) {
  const entry = state.reviews
    .flatMap((job) => job.advice?.result?.files || [])
    .find((file) => file.id === id);
  const section = [...document.querySelectorAll("[data-review-file]")].find(
    (element) => element.dataset.reviewFile === id,
  );
  if (!entry || !section) return;
  for (const [name, value] of Object.entries(entry.suggested_tags || {})) {
    if (
      !["artist", "title", "album", "genre"].includes(name) ||
      typeof value !== "string"
    )
      continue;
    const input = section.querySelector(`[data-field="${name}"]`);
    input.value = value;
    input.classList.add("changed");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
  section.querySelector(".track-tag-editor").open = true;
  toast("Suggested tags filled. Check them before adding the track.");
}
function renderDecision(job) {
  const summary = job.review_summary;
  if (!summary) return "";
  return `<div class="review-decision ${summary.status === "check" ? "needs-check" : ""}"><strong>${esc(summary.label)}</strong>${summary.concerns.length ? `<ul>${summary.concerns.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : '<p>No unresolved metadata or duplicate concerns found. Listen to confirm the recording.</p>'}${summary.completeness ? `<p>${esc(summary.completeness)}</p>` : ""}${summary.notes.length ? `<details><summary>Album details</summary><ul>${summary.notes.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></details>` : ""}<p class="muted">Checks describe the prepared files. Tag edits stay in the form until you approve.</p></div>`;
}
function renderReviewJob(job) {
  const files = [...(job.files || [])];
  if (files.length > 1 && files.every((file) => Number(file.track_number) > 0))
    files.sort((a, b) => (Number(a.disc_number) || 1) - (Number(b.disc_number) || 1) || Number(a.track_number) - Number(b.track_number));
  const open = state.reviews.length === 1 || state.expandedReviews?.has(job.id);
  const albumFields =
    files.length > 1
      ? `<div class="album-edit"><h4>Album tags</h4><div class="options">${[
          "artist",
          "album",
          "genre",
        ]
          .map((name) => {
            const values = [
              ...new Set(files.map((file) => reviewValue(file, name))),
            ];
            const value =
              albumDrafts[job.id]?.[name] ??
              (values.length === 1 ? values[0] : "");
            return `<label class="grow">${name[0].toUpperCase() + name.slice(1)}<input data-album-field="${name}" value="${esc(value)}" placeholder="${values.length > 1 ? "Mixed values" : ""}"></label>`;
          })
          .join(
            "",
          )}<button class="quiet" data-apply-album="${esc(job.id)}">Apply to selected tracks</button></div><p class="muted">Only filled album fields are applied. You can edit individual tracks below.</p></div>`
      : "";
  return `<article class="card review-card" data-review-job="${esc(job.id)}"><details class="review-disclosure" ${open ? "open" : ""}><summary><span><strong>${esc(job.label || "Download")}</strong><span class="muted">${esc(sourceLabel(job.source))} · ${files.length} track${files.length === 1 ? "" : "s"}${job.review_summary ? ` · ${esc(job.review_summary.label)}` : ""}</span></span><span class="review-summary-action">Check & add</span></summary><p class="muted">Listen, check the recording and edit its tags. Add the selected tracks when ready.</p>${renderDecision(job)}${renderJobAdvice(job)}${albumFields}<div class="review-selection-tools"><label class="checkbox"><input type="checkbox" data-select-all checked>Select all tracks</label><span class="muted" data-selected-count></span></div>${files
    .map((file, index) =>
      renderReviewFile(
        file,
        index,
        job.advice?.result?.files?.find((entry) => entry.id === file.id),
      ),
    )
    .join(
      "",
    )}<p class="muted">Unchecked tracks stay private in the retained download. They will not be added to the library.</p><div class="actions"><button data-approve="${esc(job.id)}">Add selected to library</button><button class="quiet" data-approve="${esc(job.id)}" data-keep="true">Add with original tags</button><button class="quiet" data-reject="${esc(job.id)}">Reject download</button></div></details></article>`;
}
function renderReviewFile(file, index, advice) {
  const selected = reviewSelections[file.id] !== false;
  const originals = file.existing || file.existing_tags || {};
  return `<section class="review-file ${selected ? "" : "track-unselected"}" data-review-file="${esc(file.id)}"><div class="review-track-head"><label class="checkbox"><input type="checkbox" data-select-file="${esc(file.id)}" ${selected ? "checked" : ""}><strong>${index + 1}. ${esc(file.title || file.filename || "Track")}</strong></label><button class="quiet" data-preview="${esc(file.id)}" data-title="${esc(file.title || "Track")}" data-artist="${esc(file.artist || "")}">Listen to download</button></div>${meta([file.track_number ? `Track ${file.track_number}${file.disc_number > 1 ? ` · Disc ${file.disc_number}` : ""}` : "", file.artist, file.album, file.format, file.bitrate ? `${file.bitrate} kbps` : "", duration(file.duration), bytes(file.size)])}${exactDuplicate(file)}${renderFileAdvice(file, advice)}${renderReviewWarnings(file, advice)}${reviewMatches(file)}<details class="track-tag-editor"><summary>Edit track tags</summary><div class="review-fields">${[
    "artist",
    "title",
    "album",
    "genre",
    "bpm",
    "key",
  ]
    .map((name) => {
      const old = tagValue(originals[name]);
      const value = reviewValue(file, name);
      return `<label>${name === "bpm" ? "BPM" : name[0].toUpperCase() + name.slice(1)}<input data-field="${name}" value="${esc(value)}" ${name === "bpm" ? 'type="number" min="0" max="400" step="any"' : ""} class="${old !== value ? "changed" : ""}" aria-label="${esc(name)} for ${esc(file.title || "track")}"></label>`;
    })
    .join(
      "",
    )}</div></details><details class="original-tags"><summary>Original tags and file details</summary><dl>${["artist", "title", "album", "genre", "bpm", "key"].map((name) => `<dt>${esc(name)}</dt><dd>${esc(tagValue(originals[name]) || "Empty")}</dd>`).join("")}</dl>${file.analysis_source ? `<p class="muted">Analysis source: ${esc(analysisLabel(file.analysis_source))}</p>` : ""}<a href="/api/files/${path(file.id)}/download">Download file</a></details></section>`;
}
function updateReviewSelection(container) {
  const inputs = [...container.querySelectorAll("[data-select-file]")];
  const count = inputs.filter((input) => input.checked).length;
  container.querySelector("[data-selected-count]").textContent =
    `${count} of ${inputs.length} selected`;
  const all = container.querySelector("[data-select-all]");
  all.checked = count === inputs.length;
  all.indeterminate = count > 0 && count < inputs.length;
  container
    .querySelectorAll("[data-approve]")
    .forEach((button) => (button.disabled = count === 0));
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
  if (!selected.length) throw new Error("Select at least one track to add.");
  const files = selected.map((el) => ({
    id: el.dataset.reviewFile,
    metadata: Object.fromEntries(
      [...el.querySelectorAll("[data-field]")].map((input) => [
        input.dataset.field,
        input.dataset.field === "bpm"
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
  toast("Selected tracks approved. Adding to the library.");
  clearReviewEdits(id);
  await loadReview();
  await loadJobs();
}
function clearReviewEdits(id) {
  for (const file of state.reviews.find((j) => j.id === id)?.files || []) {
    delete state.reviewEdits[file.id];
    delete reviewSelections[file.id];
  }
  delete albumDrafts[id];
  state.reviewDirty = Object.keys(state.reviewEdits).length > 0;
  state.reviewSignature = "";
  if (
    document.activeElement?.closest("[data-review-job]")?.dataset.reviewJob ===
    id
  )
    document.activeElement.blur();
}
$("#reviews").addEventListener("input", (e) => {
  const job = e.target.closest("[data-review-job]");
  if (e.target.dataset.albumField && job) {
    albumDrafts[job.dataset.reviewJob] ??= {};
    albumDrafts[job.dataset.reviewJob][e.target.dataset.albumField] =
      e.target.value;
  }
  const file = e.target.closest("[data-review-file]");
  if (!file || !e.target.dataset.field) return;
  state.reviewDirty = true;
  state.reviewEdits[file.dataset.reviewFile] ??= {};
  state.reviewEdits[file.dataset.reviewFile][e.target.dataset.field] =
    e.target.value;
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
$("#reviews").addEventListener("click", (e) => {
  const button = e.target.closest("[data-apply-album]");
  if (!button) return;
  const job = button.closest("[data-review-job]");
  const filled = [...job.querySelectorAll("[data-album-field]")].filter(
    (input) => input.value.trim(),
  );
  if (!filled.length) return toast("Fill an album field to apply it.");
  let count = 0;
  job.querySelectorAll("[data-review-file]").forEach((section) => {
    if (!section.querySelector("[data-select-file]").checked) return;
    count++;
    for (const source of filled) {
      const input = section.querySelector(
        `[data-field="${source.dataset.albumField}"]`,
      );
      input.value = source.value.trim();
      input.classList.add("changed");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
  job.querySelectorAll(".track-tag-editor").forEach((editor) => {
    if (editor.closest("[data-review-file]").querySelector("[data-select-file]").checked) editor.open = true;
  });
  toast(
    count
      ? `Album tags applied to ${count} selected track${count === 1 ? "" : "s"}.`
      : "Select tracks first.",
  );
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

function catalogRecommendation(value) {
  const labels = {
    none: "No confident match",
    low: "Weak match",
    medium: "Possible match",
    strong: "Strong match",
    alternative: "Alternative match",
  };
  return (
    labels[String(value || "").toLowerCase()] ||
    String(value || "Unrated match")
  );
}
function reviewMatches(file) {
  const duplicates = file.possible_duplicates || [];
  const matches = file.catalog_matches || [];
  return `${duplicates.length ? `<div class="match-panel"><h4>Compare library versions</h4><p class="muted">Same artist and title, different audio. Check the recording before adding another version.</p><div class="duplicate-comparison"><div><strong>This download</strong>${meta([file.album, file.format, file.bitrate ? `${file.bitrate} kbps` : "", duration(file.duration)])}<div class="actions"><button class="quiet" data-preview="${esc(file.id)}" data-title="${esc(file.title || "Track")}" data-artist="${esc(file.artist || "")}">Listen to download</button></div></div><div><strong>Already in library</strong>${duplicates.map((d) => `<p>${esc(d.artist || "Unknown artist")} · ${esc(d.title || "Untitled")}</p>${meta([d.album ? `Album: ${d.album}` : "", d.format, d.bitrate ? `${d.bitrate} kbps` : "", duration(d.duration)])}${d.id ? `<div class="actions"><button class="quiet" data-preview="${esc(d.id)}" data-title="${esc([d.title, d.album].filter(Boolean).join(" · ") || "Library version")}" data-artist="${esc(d.artist || "")}">Listen to library version</button></div>` : ""}`).join("")}</div></div></div>` : ""}${matches.length ? `<details class="match-panel"><summary>Catalog suggestions · ${matches.length}</summary><p class="muted">Check the recording and version before using a suggestion.</p>${matches.map((m, index) => `<div class="catalog-match"><div><strong>${esc(m.artist || "Unknown artist")} · ${esc(m.title || "Untitled")}</strong>${meta([catalogRecommendation(m.recommendation), duration(m.duration || m.length)])}</div><button class="quiet" data-suggestion-file="${esc(file.id)}" data-suggestion-index="${index}">Use suggestion</button></div>`).join("")}</details>` : ""}`;
}
function useSuggestion(id, index) {
  const file = state.reviews
    .flatMap((job) => job.files || [])
    .find((file) => String(file.id) === id);
  const match = file?.catalog_matches?.[index];
  const section = Array.from(
    document.querySelectorAll("[data-review-file]"),
  ).find((section) => section.dataset.reviewFile === id);
  if (!match || !section) return;
  for (const name of ["artist", "title"]) {
    if (!match[name]) continue;
    const input = section.querySelector(`[data-field="${name}"]`);
    input.value = match[name];
    input.classList.add("changed");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
  toast("Artist and title filled. Confirm the version before approving.");
}

function exactDuplicate(file) {
  if (!file.duplicate_of && !file.duplicate_file_id) return "";
  const original = file.duplicate_of;
  const label =
    typeof original === "object" && original
      ? [original.artist, original.title, original.album]
          .filter(Boolean)
          .join(" · ")
      : String(original || "Matching library track");
  return `<p class="warning">Identical audio already in your library: ${esc(label)}. Adding it will reuse the existing file and tags.</p>${file.duplicate_file_id ? `<div class="actions"><button class="quiet" data-preview="${esc(file.duplicate_file_id)}" data-title="${esc(label)}" data-artist="${esc(file.artist || "")}">Listen to library version</button></div>` : ""}`;
}

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
$("#import-existing").addEventListener("toggle", (e) => {
  if (e.target.open) loadInbox().catch((error) => toast(error.message, true));
});
