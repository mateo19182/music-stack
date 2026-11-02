// Overview: what needs you, where every request stands, each list's progress and what arrived lately.
const NEEDS_LABELS = [
  ["reviews", "download to review", "downloads to review"],
  ["choices", "request waiting for a copy to be chosen", "requests waiting for a copy to be chosen"],
  ["checks", "uploader asking for a human check", "uploaders asking for a human check"],
  ["agent_requests", "agent asking for access", "agents asking for access"],
];
async function loadSummary() {
  const summary = await api("/api/requests/summary");
  const signature = JSON.stringify(summary);
  if (signature === state.summarySignature) return;
  state.summarySignature = signature;
  state.summary = summary;
  const needs = Object.values(summary.needs_you).reduce((a, b) => a + b, 0);
  const st = summary.statuses;
  const active = st.wanted + st.queued + st.downloading + st.processing;
  $("#nav-needs").textContent = needs ? needs.toLocaleString() : "";
  $("#nav-unrequested").textContent = summary.suggested ? summary.suggested.toLocaleString() : "";
  $("#nav-requests").textContent = active ? active.toLocaleString() : "";
  if (state.tab === "overview") renderOverview();
  if (state.tab === "requests") renderRequestTabs();
}
function renderOverview() {
  const s = state.summary;
  if (!s) return;
  const st = s.statuses;
  const needs = NEEDS_LABELS.filter(([key]) => s.needs_you[key]);
  const autoOff = state.autoAdd && !state.autoAdd.search;
  $("#overview-needs").innerHTML = needs.length
    ? `<div class="callout attention"><div><strong>${plural(needs.reduce((n, [k]) => n + s.needs_you[k], 0), "thing needs", "things need")} you</strong><ul>${needs.map(([key, one, many]) => `<li>${plural(s.needs_you[key], one, many)}</li>`).join("")}</ul></div><button data-tab="needs">Open</button></div>`
    : `<div class="callout"><div><strong>All clear</strong><p class="muted">Nothing needs you. Requests from you or your agent are searched, downloaded and${autoOff ? " then wait for your review" : " added on their own"}.</p></div></div>`;
  $("#overview-needs").insertAdjacentHTML("beforeend", autoOff
    ? `<p class="muted">Automatic adding is off, so every download waits for your review. <button class="link" data-tab="settings">Change in Settings</button></p>` : "");
  const step = (label, count, tab, group, hint = "") =>
    `<button class="step${count ? "" : " zero"}" data-tab="${tab}" ${group ? `data-group="${group}"` : ""} title="${esc(hint)}"><span class="step-count">${count.toLocaleString()}</span><span class="step-label">${esc(label)}</span></button>`;
  const missing = st.not_found + st.gave_up;
  $("#overview-pipeline").innerHTML = `<div class="pipeline">${[
    step("Not requested yet", s.suggested, "unrequested", "", "Saved in your lists, not in the library; nothing is searched until you choose"),
    step("Searching", st.wanted, "requests", "progress"),
    step("Choose a copy", st.choice, "needs"),
    step("Downloading", st.queued + st.downloading, "requests", "progress"),
    step("Processing", st.processing, "requests", "progress"),
    step("Review", s.needs_you.reviews, "needs"),
    step("In library", st.have, "requests", "have"),
  ].join('<span class="arrow" aria-hidden="true">→</span>')}</div><div class="pipeline side">${step("Missing", missing, "requests", "missing", "Searched; nobody had it or every copy failed")}${st.skipped ? step("Skipped", st.skipped, "requests", "skipped") : ""}${s.upgrading ? `<span class="muted">${plural(s.upgrading, "lossy request")} looking for a lossless copy weekly</span>` : ""}</div>`;
  const bar = (l) => {
    const t = l.total || 1;
    const active = WISH_GROUPS.progress.reduce((n, k) => n + l.statuses[k], 0);
    const missing = l.statuses.not_found + l.statuses.gave_up;
    const part = (n, cls, title) => (n ? `<span class="${cls}" data-share="${n / t}" title="${esc(title)}: ${n.toLocaleString()}"></span>` : "");
    return `<div class="bar">${part(l.statuses.have, "seg-have", "In library")}${part(active, "seg-active", "In progress")}${part(missing, "seg-missing", "Missing")}${part(l.suggested, "seg-todo", "Not requested yet")}</div>`;
  };
  const num = (n, tab, list, group = "") =>
    n ? `<button class="link" data-tab="${tab}" data-list="${esc(list)}" ${group ? `data-group="${group}"` : ""}>${n.toLocaleString()}</button>` : `<span class="muted">0</span>`;
  $("#overview-lists").innerHTML = s.lists.length
    ? `<table class="lists"><thead><tr><th>List</th><th class="bar-col"></th><th>In library</th><th>In progress</th><th>Missing</th><th>Not requested yet</th></tr></thead><tbody>${s.lists
        .map((l) => {
          const active = WISH_GROUPS.progress.reduce((n, k) => n + l.statuses[k], 0);
          return `<tr><th scope="row">${esc(l.name)}</th><td class="bar-col">${bar(l)}</td><td>${num(l.statuses.have, "requests", l.name, "have")}</td><td>${num(active, "requests", l.name, "progress")}</td><td>${num(l.statuses.not_found + l.statuses.gave_up, "requests", l.name, "missing")}</td><td>${num(l.suggested, "unrequested", l.name)}</td></tr>`;
        })
        .join("")}</tbody></table><p class="muted legend"><span class="key seg-have"></span>In library <span class="key seg-active"></span>In progress <span class="key seg-missing"></span>Missing <span class="key seg-todo"></span>Not requested yet</p>`
    : `<div class="empty">No lists yet.</div>`;
  // The CSP allows no inline style attributes; widths go through the CSSOM.
  for (const part of document.querySelectorAll("#overview-lists [data-share]")) part.style.width = `${100 * part.dataset.share}%`;
}
views.overview = {
  load: async () => {
    state.summarySignature = "";
    await Promise.all([
      loadSummary(),
      loadJobs(),
      api("/api/settings/auto-add").then((a) => (state.autoAdd = a)).catch(() => {}),
    ]);
    renderOverview();
  },
  poll: () => loadJobs(),
};
logoutHooks.push(() => {
  state.summary = null;
  state.summarySignature = "";
  for (const id of ["#overview-needs", "#overview-pipeline", "#overview-lists"]) $(id).innerHTML = "";
  for (const id of ["#nav-needs", "#nav-unrequested", "#nav-requests"]) $(id).textContent = "";
});
