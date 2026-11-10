async function loadReview() {
  const data = await api("/api/review");
  state.reviews = data.jobs || [];
  state.reviewerConfigured = !!data.reviewer?.configured;
  const signature = JSON.stringify(state.reviews);
  if (
    document.activeElement?.closest("#reviews") ||
    signature === state.reviewSignature
  )
    return;
  state.reviewSignature = signature;
  $("#reviews").innerHTML = state.reviews.length
    ? state.reviews.map(renderReviewJob).join("")
    : "";
  renderNeedsEmpty();
  for (const job of document.querySelectorAll("[data-review-job]")) {
    updateReviewSelection(job);
    applyReviewFilter(job);
  }
}
// Review choices that survive the five-second refresh.
const reviewSelections = {};
const albumDrafts = {};
const reviewOpen = {};
const trackOpen = {};
const albumOverrides = {};
const reviewFilter = {};
const ALBUM_FIELDS = ["artist", "album", "genre", "year"];
const STATUS_LABELS = { ready: "Ready", check: "Needs you", duplicate: "In library" };
const answers = {};
function questionText(q, file) {
  if (q.kind === "missing")
    return `This file has no ${q.fields.join(" or ")}. Type ${q.fields.length > 1 ? "them" : "it"} below to add it.`;
  if (q.kind === "mismatch")
    return `You searched for “${q.requested}”, but the file says “${q.found}”.`;
  if (q.kind === "long")
    return `One ${Math.round(q.duration / 60)}-minute recording, probably a whole album or mix.`;
  if (q.kind === "version") {
    const mine = q.library.map((d) => [d.format, d.bitrate ? `${d.bitrate} kbps` : "", duration(d.duration)].filter(Boolean).join(" · ")).join("; ");
    const theirs = [file.format, file.bitrate ? `${file.bitrate} kbps` : "", duration(file.duration)].filter(Boolean).join(" · ");
    return `You already have a version of this (${mine}). This one is ${theirs}, so it may be a different recording.`;
  }
  return "";
}
function questionAnswers(q) {
  if (q.kind === "missing") return [["add", "Add"]];
  if (q.kind === "mismatch") return [["add", `Add as “${q.found}”`]];
  if (q.kind === "long") return [["add", "Add as one track"]];
  if (q.kind === "version")
    return [
      ...(q.library.every((d) => d.replaceable) ? [["replace", "Replace yours"]] : []),
      ["add", "Keep both"],
    ];
  return [["add", "Add"]];
}
function renderQuestions(file, review) {
  const picked = answers[file.id] || {};
  const yours = review.questions.find((q) => q.kind === "version")?.library || [];
  return `<div class="questions">${review.questions
    .map(
      (q) =>
        `<div class="question" data-kind="${esc(q.kind)}"><p>${esc(questionText(q, file))}${q.kind === "version" ? yours.filter((d) => d.id).map((d) => ` ${playButton(d.id, [d.title, d.album].filter(Boolean).join(" · ") || "Your version", d.artist, "▶ Play yours")}`).join("") : ""}</p><div class="answers">${questionAnswers(q)
          .map(([value, label]) => `<button type="button" class="${picked[q.kind] === value ? "" : "quiet"}" data-answer="${value}" data-kind="${esc(q.kind)}">${esc(label)}</button>`)
          .join("")}<button type="button" class="quiet" data-answer="skip">Skip</button></div></div>`,
    )
    .join("")}</div>`;
}
function reviewValue(file, name) {
  const value = tagValue(
    state.reviewEdits[file.id]?.[name] ??
      (file.proposed || file.proposed_tags)?.[name] ??
      file[name],
  );
  // Number inputs show nothing for dates like 2024-05-01; keep the year.
  return name === "year" && value ? value.match(/\d{4}/)?.[0] || "" : value;
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
  if (file.plan) {
    // The server decides what is automatic; only its questions need a person.
    const { action, questions = [], reason } = file.plan;
    return {
      status: action === "ask" ? "check" : action === "skip" ? "duplicate" : "ready",
      questions,
      skipReason: reason,
      replaces: action === "replace",
      reasons: [],
      notes: warnings,
    };
  }
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
function jobTitle(job) {
  const request = job.request || {};
  const artist = request.requested_artist;
  const name =
    request.kind === "album" || !request.requested_title
      ? request.requested_album || request.requested_title
      : request.requested_title;
  if (artist && name) return `${artist} — ${name}`;
  if (job.source === "existing" && (job.files || []).length > 1) {
    const counts = {};
    for (const file of job.files) {
      const name = tagValue(file.proposed?.artist ?? file.artist).trim();
      if (name) counts[name] = (counts[name] || 0) + 1;
    }
    const artists = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
    if (artists.length > 1)
      return `${artists.slice(0, 3).join(", ")}${artists.length > 3 ? ` +${artists.length - 3} more` : ""}`;
    if (artists.length === 1) {
      const albums = [...new Set(job.files.map((f) => tagValue(f.proposed?.album ?? f.album)).filter(Boolean))];
      return albums.length === 1 ? `${artists[0]} — ${albums[0]}` : artists[0];
    }
  }
  return job.label || "Download";
}
function jobOrigin(job) {
  const request = job.request || {};
  const title = jobTitle(job);
  const folder = String(request.filename || "")
    .split(/[\\/]/)
    .slice(-3, -1)
    .join(" / ");
  const parts =
    job.source === "soulseek"
      ? [`Soulseek${request.username ? ` from ${request.username}` : ""}`, folder]
      : job.source === "torrent"
        ? [`Torrent${request.provider ? ` from ${request.provider}` : ""}`]
      : job.source === "existing"
        ? ["Inbox", job.label !== title ? String(job.label || "").replace(/ · \d+ files$/, "") : ""]
        : [sourceLabel(job.source), request.uploader];
  const found = request.source_title && request.source_title !== title ? `found as “${request.source_title}”` : "";
  return [...parts, found, job.created_at ? new Date(job.created_at).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : ""].filter(Boolean);
}
function reviewGroups(job, files) {
  if (!job.review_summary?.mixed) return [{ key: "all", files, fields: files.length > 1 }];
  const byRelease = new Map();
  for (const file of files) {
    const album = reviewValue(file, "album").trim();
    const artist = tagValue(file.proposed?.album_artist || file.album_artist) || reviewValue(file, "artist");
    const key = album ? `${artist}\u0000${album}`.toLowerCase() : `single\u0000${file.id}`;
    if (!byRelease.has(key)) byRelease.set(key, { key, artist, album, files: [] });
    byRelease.get(key).files.push(file);
  }
  const albums = [...byRelease.values()].filter((group) => group.files.length > 1);
  const others = files.filter((file) => !albums.some((group) => group.files.includes(file)));
  albums.sort((a, b) => a.artist.localeCompare(b.artist) || a.album.localeCompare(b.album));
  return [
    ...albums.map((group) => ({ ...group, fields: true, heading: `${group.artist || "Unknown artist"} — ${group.album}` })),
    ...(others.length ? [{ key: "others", files: others, fields: false, heading: albums.length ? "Other tracks" : "" }] : []),
  ];
}
function sortTracks(files) {
  if (files.length > 1 && files.every((file) => Number(file.track_number) > 0))
    files.sort((a, b) => (Number(a.disc_number) || 1) - (Number(b.disc_number) || 1) || Number(a.track_number) - Number(b.track_number));
  return files;
}
function renderReviewJob(job) {
  const files = sortTracks([...(job.files || [])]);
  const multi = files.length > 1;
  const adviceFor = (file) =>
    job.advice?.result?.files?.find((entry) => entry.id === file.id);
  const reviews = Object.fromEntries(files.map((file) => [file.id, trackReview(file, adviceFor(file))]));
  const counts = { check: 0, ready: 0, duplicate: 0 };
  for (const review of Object.values(reviews)) counts[review.status]++;
  const summary = job.review_summary;
  const status = summary?.status === "check" || counts.check ? "check" : "ready";
  const open = reviewOpen[job.id] ?? state.reviews.length <= 3;
  const big = files.length > 8;
  const mixedStatus = multi && counts.check && counts.ready;
  const filter = reviewFilter[job.id] || (mixedStatus ? "check" : "all");
  const groups = reviewGroups(job, files);
  let index = 0;
  const groupHtml = groups
    .map((group) => {
      const groupFiles = sortTracks([...group.files]);
      const fields = group.fields
        ? `<div class="album-fields">${ALBUM_FIELDS.map((name) => {
            const values = [...new Set(groupFiles.map((file) => reviewValue(file, name)))];
            const value = albumDrafts[job.id]?.[group.key]?.[name] ?? (values.length === 1 ? values[0] : "");
            return `<label>${tagLabel(name)}<input data-album-field="${name}" value="${esc(value)}" ${tagInputAttributes(name)} placeholder="${values.length > 1 ? "Mixed: typing sets every track" : ""}"></label>`;
          }).join("")}</div>`
        : "";
      const head = group.heading
        ? `<div class="group-head"><strong>${esc(group.heading)}</strong><span class="muted">${groupFiles.length} track${groupFiles.length === 1 ? "" : "s"}</span><button type="button" class="quiet" data-add-group>Add these</button></div>`
        : "";
      return `<div class="review-group" data-group="${esc(group.key)}">${head}${fields}${groupFiles
        .map((file) => renderReviewFile(file, index++, adviceFor(file), reviews[file.id], multi, big))
        .join("")}</div>`;
    })
    .join("");
  const filters = big || mixedStatus
    ? `<div class="review-tools"><button type="button" class="quiet" data-play-all>▶ Play all</button><div class="segmented" role="group" aria-label="Show tracks">${[
        ["all", `All ${files.length}`],
        ["check", `Needs you ${counts.check}`],
        ["ready", `Ready ${counts.ready}`],
        ["duplicate", `In library ${counts.duplicate}`],
      ]
        .filter(([name]) => name === "all" || counts[name])
        .map(([name, label]) => `<button type="button" data-filter="${name}" aria-pressed="${filter === name}">${label}</button>`)
        .join("")}</div></div>`
    : multi
      ? `<div class="review-tools"><button type="button" class="quiet" data-play-all>▶ Play all</button></div>`
      : "";
  const [origin, ...details] = jobOrigin(job);
  const link = job.request?.source_url ? ` <a href="${safeLink(job.request.source_url)}" target="_blank" rel="noopener">Open source ↗</a>` : "";
  return `<article class="card review-card" data-review-job="${esc(job.id)}" data-default-filter="${esc(filter)}"><details class="review-disclosure" ${open ? "open" : ""}><summary><span class="review-title"><strong>${esc(jobTitle(job))}</strong><span class="muted">${esc([origin, ...details, `${files.length} track${multi ? "s" : ""}`].join(" · "))}${link}</span></span>${adviceBadge(job)}<span class="status status-${status}">${status === "check" ? (counts.check ? `${counts.check} need${counts.check === 1 ? "s" : ""} you` : "Check") : "Ready"}</span></summary>${renderConcerns(job, summary)}${filters}<div class="track-list">${groupHtml}</div><div class="review-bar"><label class="checkbox" ${multi ? "" : "hidden"}><input type="checkbox" data-select-all>Include all</label><span class="muted" data-selected-count></span><details class="more-actions"><summary aria-label="More review actions">⋯</summary><div><button class="quiet" data-approve="${esc(job.id)}" data-keep="true">Add with original tags</button>${adviceAction(job)}</div></details>${job.request?.wishlist_id
    ? `<button class="quiet" data-reject="${esc(job.id)}" data-stop="true" title="Nothing is added, and its request stops: nothing else is downloaded for it.">Reject and stop</button><button class="quiet" data-reject="${esc(job.id)}" title="Nothing is added; its request tries its next copy.">Reject, try another copy</button>`
    : `<button class="quiet" data-reject="${esc(job.id)}" title="Nothing is added. The download is kept privately.">Reject</button>`}${multi && counts.check && counts.ready ? `<button class="quiet" data-add-ready>Add ready</button>` : ""}<button data-approve="${esc(job.id)}">Add</button></div></details></article>`;
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
function renderReviewFile(file, index, advice, review, multi, big) {
  const selected = reviewSelections[file.id] ?? !(multi && review.status === "duplicate");
  const open = trackOpen[file.id] ?? (review.questions
    ? review.questions.some((q) => q.kind === "missing")
    : (review.status === "check" && !big) || !multi);
  const originals = file.existing || file.existing_tags || {};
  const title = reviewValue(file, "title") || file.filename || "Track";
  const artist = reviewValue(file, "artist");
  const number = file.track_number
    ? `${file.disc_number > 1 ? `${file.disc_number}-` : ""}${file.track_number}`
    : index + 1;
  const duplicates = file.possible_duplicates || [];
  const fileDetails = [file.format, file.bitrate ? `${file.bitrate} kbps` : "", duration(file.duration)].filter(Boolean).join(" · ");
  const more = review.reasons.length > 1 ? `+${review.reasons.length - 1} more` : "";
  return `<section class="review-file ${selected ? "" : "track-unselected"} ${open ? "open" : ""}" data-review-file="${esc(file.id)}" data-status="${review.status}"><div class="track-row"><input type="checkbox" data-select-file="${esc(file.id)}" ${selected ? "checked" : ""} ${multi ? "" : "hidden"} title="Include this track. Tracks left out stay in Review." aria-label="Include ${esc(title)}"><button type="button" class="track-toggle" data-toggle-track aria-expanded="${open}"><span class="track-number">${esc(number)}</span><span class="track-name"><strong data-track-title>${esc(title)}</strong><span class="muted">${esc([artist, fileDetails].filter(Boolean).join(" · "))}</span></span></button>${playButton(file.id, title, artist)}<span class="status status-${review.status}">${STATUS_LABELS[review.status]}</span></div>${review.questions?.length ? renderQuestions(file, review) : review.status === "check" ? `<ul class="track-reasons" data-more="${esc(more)}">${review.reasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}</ul>` : ""}${review.replaces ? `<p class="track-note">Better quality than the copy in your library, which it replaces. Undo brings yours back.</p>` : ""}${review.skipReason === "not-better" ? `<p class="track-note">You already have this recording at the same or better quality.</p>` : ""}${review.skipReason === "repeat" ? `<p class="track-note">This download holds this song twice; its better copy is added instead.</p>` : ""}${review.status === "duplicate" && !["not-better", "repeat"].includes(review.skipReason) ? `<p class="track-note">Same audio as <strong>${esc(typeof file.duplicate_of === "string" ? file.duplicate_of : "a library track")}</strong>. Adding it reuses the existing file and tags.${file.duplicate_file_id ? ` ${playButton(file.duplicate_file_id, file.duplicate_of || "Library version", artist, "▶ Play yours")}` : ""}</p>` : ""}<div class="track-details"><div class="review-fields">${TAG_FIELDS.map((name) => reviewField(file, advice, name, originals)).join("")}</div>${duplicates.length ? `<div class="compare"><div><span class="muted">This download</span><span>${esc(fileDetails || "Unknown format")}</span>${playButton(file.id, title, artist)}</div>${duplicates.map((d) => `<div><span class="muted">In your library</span><span>${esc([d.title, d.album, d.format, d.bitrate ? `${d.bitrate} kbps` : "", duration(d.duration)].filter(Boolean).join(" · "))}</span>${d.id ? playButton(d.id, [d.title, d.album].filter(Boolean).join(" · ") || "Library version", d.artist, "▶ Play yours") : ""}</div>`).join("")}</div>` : ""}<p class="track-foot muted">${esc([file.filename, bytes(file.size), file.analysis_source ? `Analysis: ${analysisLabel(file.analysis_source)}` : "", advice?.status !== "check" ? advice?.reason : "", ...review.notes].filter(Boolean).join(" · "))} <a href="/api/files/${path(file.id)}/download">Download file</a></p></div></section>`;
}
function updateReviewSelection(container) {
  const inputs = [...container.querySelectorAll("[data-select-file]")];
  const count = inputs.filter((input) => input.checked).length;
  const all = container.querySelector("[data-select-all]");
  all.checked = count === inputs.length;
  all.indeterminate = count > 0 && count < inputs.length;
  const later = inputs.filter((input) => !input.checked && input.closest("[data-review-file]").dataset.status !== "duplicate").length;
  container.querySelector("[data-selected-count]").textContent =
    inputs.length > 1 ? `${count} of ${inputs.length} included${later ? ` · ${later} stay in Review` : ""}` : "";
  container
    .querySelectorAll("[data-approve]")
    .forEach((button) => (button.disabled = count === 0));
  container.querySelector("[data-approve]:not([data-keep])").textContent =
    inputs.length === 1 ? "Add to library" : `Add ${count} track${count === 1 ? "" : "s"}`;
  const ready = inputs.filter((input) => input.checked && input.closest("[data-review-file]").dataset.status === "ready").length;
  const addReady = container.querySelector("[data-add-ready]");
  if (addReady) {
    addReady.textContent = `Add ${ready} ready`;
    addReady.disabled = ready === 0;
  }
  container.querySelectorAll("[data-add-group]").forEach((button) => {
    const included = button.closest("[data-group]").querySelectorAll("[data-select-file]:checked").length;
    button.textContent = `Add these ${included}`;
    button.disabled = included === 0;
  });
  inputs.forEach((input) =>
    input
      .closest("[data-review-file]")
      .classList.toggle("track-unselected", !input.checked),
  );
}
function applyReviewFilter(container) {
  const filter = reviewFilter[container.dataset.reviewJob] || container.dataset.defaultFilter;
  container.dataset.filterActive = filter;
  container.querySelectorAll("button[data-filter]").forEach((button) => button.setAttribute("aria-pressed", button.dataset.filter === filter));
  container.querySelectorAll("[data-review-file]").forEach((section) => (section.hidden = filter !== "all" && section.dataset.status !== filter));
  container.querySelectorAll("[data-group]").forEach((group) => (group.hidden = !group.querySelector("[data-review-file]:not([hidden])")));
}
// Adds the included tracks in scope (a group, the ready ones, or all). Other
// tracks stay in Review, except identical library copies that were left out.
async function approveReview(id, keep, scope, replace) {
  const container = [...document.querySelectorAll("[data-review-job]")].find(
    (el) => el.dataset.reviewJob === id,
  );
  const sections = [...container.querySelectorAll("[data-review-file]")];
  const selected = sections.filter((el) => el.querySelector("[data-select-file]").checked && (!scope || scope(el)));
  if (!selected.length) throw new Error("Include at least one track.");
  const later = sections
    .filter((el) => !selected.includes(el) && !(el.dataset.status === "duplicate" && !el.querySelector("[data-select-file]").checked))
    .map((el) => el.dataset.reviewFile);
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
  container.querySelectorAll(".invalid").forEach((input) => input.classList.remove("invalid"));
  try {
    await api(`/api/jobs/${path(id)}/approve`, {
      files,
      selected_file_ids: files.map((file) => file.id),
      later_file_ids: later,
      replace,
      keep_existing: keep,
    });
  } catch (error) {
    const section = sections.find((el) => el.dataset.reviewFile === error.fileId);
    if (section) {
      // Show the track with the problem, even in a long or filtered list.
      reviewFilter[id] = "all";
      applyReviewFilter(container);
      section.classList.add("open");
      trackOpen[error.fileId] = true;
      const input = section.querySelector(`[data-field="${error.field}"]`);
      input?.classList.add("invalid");
      (input || section).scrollIntoView({ behavior: "smooth", block: "center" });
      input?.focus({ preventScroll: true });
    }
    throw error;
  }
  toast(`Adding ${files.length} track${files.length === 1 ? "" : "s"} to the library.${later.length ? ` ${later.length} stay in Review.` : ""}`);
  clearReviewEdits(id, new Set(later));
  await loadReview();
  await loadSummary();
}
function clearReviewEdits(id, keep = new Set()) {
  for (const file of state.reviews.find((j) => j.id === id)?.files || []) {
    if (keep.has(file.id)) continue;
    delete state.reviewEdits[file.id];
    delete reviewSelections[file.id];
    delete trackOpen[file.id];
    delete albumOverrides[file.id];
  }
  delete albumDrafts[id];
  delete reviewOpen[id];
  delete reviewFilter[id];
  state.reviewDirty = Object.keys(state.reviewEdits).length > 0;
  state.reviewSignature = "";
  if (
    document.activeElement?.closest("[data-review-job]")?.dataset.reviewJob ===
    id
  )
    document.activeElement.blur();
}
// An answer to the last open question adds the track; Skip leaves it out for good.
function answerQuestion(job, button) {
  const section = button.closest("[data-review-file]");
  const id = section.dataset.reviewFile;
  const jobId = job.dataset.reviewJob;
  if (button.dataset.answer === "skip")
    return busy(button, async () => {
      await api(`/api/jobs/${path(jobId)}/skip`, { file_ids: [id] });
      toast("Skipped. The file stays privately in the download.");
      state.reviewSignature = "";
      document.activeElement?.blur();
      await loadReview();
    });
  const file = state.reviews.flatMap((j) => j.files || []).find((f) => f.id === id);
  const questions = file?.plan?.questions || [];
  if (questions.some((q) => q.kind === "missing")) {
    const empty = ["artist", "title"]
      .map((name) => section.querySelector(`[data-field="${name}"]`))
      .find((input) => !input.value.trim());
    if (empty) {
      section.classList.add("open");
      empty.classList.add("invalid");
      empty.focus();
      return toast(`Type the ${empty.dataset.field} first.`);
    }
  }
  const picked = (answers[id] ??= {});
  picked[button.dataset.kind] = button.dataset.answer;
  button.parentElement.querySelectorAll("[data-answer]").forEach((b) => b.classList.toggle("quiet", b !== button));
  if (questions.some((q) => !picked[q.kind])) return;
  const version = questions.find((q) => q.kind === "version");
  const replace = picked.version === "replace" ? { [id]: version.library.map((d) => d.id) } : undefined;
  busy(button, () => approveReview(jobId, false, (el) => el === section, replace));
}
function syncReviewField(input) {
  const section = input.closest("[data-review-file]");
  state.reviewDirty = true;
  (state.reviewEdits[section.dataset.reviewFile] ??= {})[input.dataset.field] =
    input.value;
  const changed = input.value !== input.dataset.original;
  input.classList.toggle("changed", changed);
  input.classList.remove("invalid");
  input.parentElement.querySelector("[data-reset-field]").hidden = !changed;
  if (input.dataset.field === "title")
    section.querySelector("[data-track-title]").textContent =
      input.value || "Track";
}
$("#reviews").addEventListener("input", (e) => {
  const job = e.target.closest("[data-review-job]");
  const field = e.target.dataset.albumField;
  if (field && job) {
    // Album values follow every track in the group except fields edited on the track itself.
    const group = e.target.closest("[data-group]");
    ((albumDrafts[job.dataset.reviewJob] ??= {})[group.dataset.group] ??= {})[field] = e.target.value;
    group.querySelectorAll("[data-review-file]").forEach((section) => {
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
  const job = button?.closest("[data-review-job]");
  if (!job) return;
  const jobId = job.dataset.reviewJob;
  if (button.dataset.filter) {
    reviewFilter[jobId] = button.dataset.filter;
    applyReviewFilter(job);
    return;
  }
  if (button.hasAttribute("data-play-all")) {
    const tracks = [...job.querySelectorAll("[data-review-file]:not([hidden])")]
      .filter((section) => section.querySelector("[data-select-file]").checked)
      .map((section) => section.querySelector(".track-row [data-preview]").dataset);
    if (!tracks.length) return toast("Include a track to play.");
    playQueue(tracks.map((t) => ({ id: t.preview, title: t.title, artist: t.artist })));
    return;
  }
  if (button.dataset.answer) return answerQuestion(job, button);
  if (button.hasAttribute("data-add-ready"))
    return busy(button, () => approveReview(jobId, false, (el) => el.dataset.status === "ready"));
  if (button.hasAttribute("data-add-group")) {
    const group = button.closest("[data-group]");
    return busy(button, () => approveReview(jobId, false, (el) => group.contains(el)));
  }
  const section = button.closest("[data-review-file]");
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
document.addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  if (button.dataset.requestAdvice)
    busy(button, async () => {
      await api(`/api/jobs/${path(button.dataset.requestAdvice)}/advice`, {});
      state.reviewSignature = "";
      document.activeElement?.blur();
      await loadReview();
      toast("Review advice requested. Final approval is yours.");
    });
  else if (button.dataset.approve)
    busy(button, () => approveReview(button.dataset.approve, button.dataset.keep === "true"));
  else if (button.dataset.reject)
    busy(button, async () => {
      const stop = button.dataset.stop === "true";
      await api(`/api/jobs/${path(button.dataset.reject)}/reject`, { stop });
      toast(stop ? "Rejected, and its request stopped." : "Rejected. Nothing was added; a request tries its next copy.");
      clearReviewEdits(button.dataset.reject);
      await loadReview();
      await loadSummary();
    });
});
logoutHooks.push(() => {
  state.reviewDirty = false;
  state.reviewEdits = {};
  for (const store of [reviewSelections, albumDrafts, reviewOpen, trackOpen, albumOverrides, reviewFilter, answers])
    for (const key of Object.keys(store)) delete store[key];
  state.reviews = [];
  state.reviewSignature = "";
  state.reviewerConfigured = false;
  $("#reviews").innerHTML = "";
});
