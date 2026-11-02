// Requests by where they stand. Counts come from the summary; each group loads only its own rows.
const WISH_LABELS = {
  wanted: "Searching", choice: "Choose a copy", queued: "Queued", downloading: "Downloading", processing: "Processing",
  review: "In review", have: "In library", not_found: "Nobody has it", gave_up: "Copies failed", skipped: "Skipped",
};
const WISH_GROUPS = {
  progress: ["wanted", "choice", "queued", "downloading", "processing", "review"],
  missing: ["not_found", "gave_up"], have: ["have"], skipped: ["skipped"],
};
const WISH_BADGE = { have: "published", review: "review", choice: "review", not_found: "failed", gave_up: "failed" };
const LIBRARY_ROWS = 200;
state.requestGroup = "progress";
state.listFilter = "";
function wishRow(i) {
  const job = i.job;
  const tried = i.tried.length
    ? `<details><summary>${i.tries} tr${i.tries === 1 ? "y" : "ies"}</summary>${i.tried
        .map((t) => `<p class="muted">${esc(sourceLabel(t.source))} · ${esc(String(t.provider || "").slice(0, 40))} · ${esc(t.outcome)}${t.error ? `: ${esc(t.error.slice(0, 140))}` : ""}</p>`)
        .join("")}</details>`
    : "";
  const next = i.next.length && !["have", "skipped"].includes(i.status)
    ? `<details ${i.status === "choice" ? "open" : ""}><summary>${i.candidates} cop${i.candidates === 1 ? "y" : "ies"} found${i.status === "choice" ? " · waiting for your or your agent's choice" : ""}</summary>${i.next
        .map((c) => `<p class="muted">${i.status === "choice" && c.id ? `<button class="quiet" data-wish-choose="${esc(i.id)}" data-copy="${esc(c.id)}">Get this</button> ` : ""}${esc(sourceLabel(c.source))} · ${esc(c.username || c.provider || "")} · ${esc(c.title || "")} · ${esc([c.format, c.bitrate ? `${c.bitrate} kbps` : "", c.file_count ? `${c.file_count} files` : "", c.seeders != null ? `${c.seeders} seeders` : ""].filter(Boolean).join(" · "))}${c.reason ? ` — ${esc(c.reason)}` : ""}</p>`)
        .join("")}</details>`
    : "";
  const rejected = (i.rejected || []).length && !["have", "skipped"].includes(i.status)
    ? `<details><summary>${i.rejected.length} turned down by the matcher</summary>${i.rejected
        .map((r) => `<p class="muted">${esc(sourceLabel(r.source))} · ${esc(String(r.name || "").split(/[\\/]/).pop())} — ${esc(r.reason)}</p>`)
        .join("")}</details>`
    : "";
  const when = i.searched_at ? `Searched ${relativeTime(i.searched_at)}` : "";
  const downloading = job && ["queued", "downloading"].includes(job.stage);
  const settled = ["not_found", "gave_up", "skipped"].includes(i.status);
  const actions = [
    job && job.stage === "review" ? `<button data-tab="needs">Review</button>` : "",
    downloading ? `<button class="quiet" data-cancel="${esc(job.id)}" title="Stop this copy; the next one is tried">Try another copy</button>` : "",
    (settled || i.status === "wanted") && !job ? `<button class="quiet" data-wish-retry="${esc(i.id)}">${i.kind === "link" ? "Try again" : "Search again"}</button>` : "",
    settled && i.kind !== "link" ? `<button class="quiet" data-wish-rename="${esc(i.id)}">Fix name</button>` : "",
    !["have", "skipped", "not_found", "gave_up"].includes(i.status) ? `<button class="quiet" data-wish-skip="${esc(i.id)}">Stop</button>` : "",
    `<button class="quiet" data-wish-remove="${esc(i.id)}">Remove</button>`,
  ].join("");
  const kind = { album: "Album", track: "Track", link: "Link" }[i.kind] || "";
  const title = i.kind === "link" ? i.name : `${i.artist ? `${i.artist} — ` : ""}${i.name}`;
  return `<article class="card wish-row" data-wish="${esc(i.id)}"><div class="card-head"><div><span class="badge ${WISH_BADGE[i.status] || ""}">${esc(WISH_LABELS[i.status] || i.status)}</span>${i.star ? ' <span class="badge">★</span>' : ""}<h3>${esc(title)}</h3>${meta([kind, i.list, i.upgrading ? "looking for lossless weekly" : "", i.note, when])}</div></div>${downloading && job.progress != null ? `<progress value="${Math.max(0, Math.min(100, Number(job.progress) || 0))}" max="100"></progress>` : ""}${next}${rejected}${tried}<div class="actions">${actions}</div></article>`;
}
function renameForm(i) {
  return `<form class="rename-form" data-rename="${esc(i.id)}"><label>Artist<input name="artist" value="${esc(i.artist)}" required maxlength="200" autocomplete="off"></label><label>${i.kind === "album" ? "Album" : "Title"}<input name="name" value="${esc(i.name)}" required maxlength="300" autocomplete="off"></label><button type="submit">Save and search</button><button type="button" class="quiet" data-rename-cancel>Cancel</button></form>`;
}
async function fetchGroup(group) {
  const list = state.listFilter;
  const q = $("#wish-filter-text").value.trim();
  const ask = (status, limit = 500) => api(`/api/requests?${new URLSearchParams({ status, limit, ...(q ? { q } : {}), ...(list ? { list } : {}) })}`);
  const pages = group === "progress" ? [ask("active")]
    : group === "have" ? [ask("have", LIBRARY_ROWS)]
    : WISH_GROUPS[group].map((s) => ask(s));
  const items = (await Promise.all(pages)).flatMap((p) => p.items);
  return items.filter((i) => !list || i.list === list);
}
async function loadWishlist() {
  const group = state.requestGroup;
  renderRequestTabs();
  if (group === "other") return loadJobs();
  const items = await fetchGroup(group);
  if (group !== state.requestGroup) return;
  state.wishItems = items;
  if (document.activeElement?.closest("#wishlist form")) return;   // don't redraw under a half-typed fix
  const byList = {};
  for (const i of items) (byList[i.list] ||= []).push(i);
  const capped = group === "have" && items.length >= LIBRARY_ROWS
    ? `<p class="muted">Showing the first ${LIBRARY_ROWS}. Filter by list or name, or browse everything in Library.</p>` : "";
  $("#wishlist").innerHTML = items.length
    ? capped + Object.entries(byList).map(([name, rows]) => `<div class="section-title"><h2>${esc(name)} <span class="muted">${rows.length}</span></h2></div>${rows.map(wishRow).join("")}`).join("")
    : `<div class="empty">${{ progress: "Nothing in progress. New requests from you or your agent show up here while they are searched and downloaded.", missing: "Nothing missing.", have: "Nothing here yet.", skipped: "Nothing skipped." }[group]}</div>`;
}
function renderRequestTabs() {
  const group = state.requestGroup;
  const s = state.summary;
  const counts = {};
  if (s) {
    const statuses = state.listFilter ? s.lists.find((l) => l.name === state.listFilter)?.statuses || {} : s.statuses;
    for (const [g, list] of Object.entries(WISH_GROUPS)) counts[g] = list.reduce((n, st) => n + (statuses[st] || 0), 0);
  }
  for (const button of document.querySelectorAll("[data-wish-tab]")) {
    const tab = button.dataset.wishTab;
    button.hidden = tab === "skipped" && !counts.skipped && group !== tab;
    button.setAttribute("aria-pressed", String(tab === group));
    const count = button.querySelector("span");
    if (count) count.textContent = counts[tab] ? counts[tab].toLocaleString() : "";
  }
  const select = $("#wish-filter-list");
  const names = (s?.lists || []).filter((l) => l.total > l.suggested).map((l) => l.name);
  if (select.dataset.names !== names.join("\n")) {
    select.dataset.names = names.join("\n");
    select.innerHTML = `<option value="">All lists</option>${names.map((n) => `<option>${esc(n)}</option>`).join("")}`;
    $("#wish-lists").innerHTML = names.map((n) => `<option value="${esc(n)}"></option>`).join("");
  }
  select.value = state.listFilter;
  const other = group === "other";
  $("#wishlist").hidden = other;
  $("#other-downloads").hidden = !other;
  $("#wish-filters").hidden = other;
  $("#retry-missing").hidden = group !== "missing" || !counts.missing;
}
for (const button of document.querySelectorAll("[data-wish-tab]"))
  button.addEventListener("click", () => {
    state.requestGroup = button.dataset.wishTab;
    $("#wishlist").innerHTML = "";
    loadWishlist().catch((error) => toast(error.message, true));
  });
$("#wish-filter-list").addEventListener("change", (e) => {
  state.listFilter = e.target.value;
  loadWishlist().catch((error) => toast(error.message, true));
});
let wishFilterTimer;
$("#wish-filter-text").addEventListener("input", () => {
  clearTimeout(wishFilterTimer);
  wishFilterTimer = setTimeout(() => loadWishlist().catch((error) => toast(error.message, true)), 300);
});
$("#retry-missing").addEventListener("click", (e) =>
  busy(e.target, async () => {
    const { count } = await api("/api/requests/retry", { status: "missing", ...(state.listFilter ? { list: state.listFilter } : {}) });
    toast(`${plural(count, "request")} will be searched again, a few at a time.`);
    await Promise.all([loadSummary(), loadWishlist()]);
  }),
);
$("#wish-kind").addEventListener("change", (e) => {
  $("#wish-name-label").textContent = e.target.value === "album" ? "Album" : "Title";
});
$("#wishlist-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  busy(form.querySelector("button"), async () => {
    const kind = $("#wish-kind").value;
    const item = await api("/api/requests", { kind, artist: $("#wish-artist").value, [kind === "album" ? "album" : "title"]: $("#wish-album").value, list: $("#wish-list").value });
    toast(`Requested ${item.name}. It will be searched shortly.`);
    $("#wish-artist").value = "";
    $("#wish-album").value = "";
    state.requestGroup = "progress";
    await Promise.all([loadSummary(), loadWishlist()]);
  });
});
document.addEventListener("click", (e) => {
  const rename = e.target.closest("[data-wish-rename]");
  if (rename) {
    const card = rename.closest(".wish-row");
    const item = state.wishItems?.find((i) => i.id === rename.dataset.wishRename);
    if (item && !card.querySelector("form")) card.insertAdjacentHTML("beforeend", renameForm(item));
    card.querySelector("form input")?.focus();
    return;
  }
  if (e.target.closest("[data-rename-cancel]")) return e.target.closest("form").remove();
  const button = e.target.closest("[data-wish-retry],[data-wish-skip],[data-wish-remove],[data-wish-choose]");
  if (!button) return;
  const [action, id] = button.dataset.wishRetry ? ["retry", button.dataset.wishRetry]
    : button.dataset.wishSkip ? ["skip", button.dataset.wishSkip]
    : button.dataset.wishChoose ? ["choose", button.dataset.wishChoose] : ["remove", button.dataset.wishRemove];
  busy(button, async () => {
    await api(`/api/requests/${path(id)}/${action}`, action === "choose" ? { candidate_id: button.dataset.copy } : {});
    toast({ retry: "Searching again shortly.", skip: "Stopped.", remove: "Request removed.", choose: "Getting that copy." }[action]);
    state.choicesSignature = null;
    await Promise.all([loadSummary(), state.tab === "needs" ? loadChoices() : loadWishlist()]);
  });
});
document.addEventListener("submit", (e) => {
  const form = e.target.closest("[data-rename]");
  if (!form) return;
  e.preventDefault();
  busy(form.querySelector("button[type=submit]"), async () => {
    const item = await api(`/api/requests/${path(form.dataset.rename)}/rename`, formData(form));
    toast(`Saved as ${item.artist} — ${item.name}; searching again.`);
    form.remove();
    await Promise.all([loadSummary(), loadWishlist()]);
  });
});
views.requests = { load: loadWishlist, poll: () => (state.requestGroup === "progress" || state.requestGroup === "other" ? loadWishlist() : Promise.resolve()) };
logoutHooks.push(() => {
  state.wishItems = [];
  $("#wishlist").innerHTML = "";
});
