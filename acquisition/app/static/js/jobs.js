async function loadJobs() {
  const data = await api("/api/jobs");
  state.jobs = data.jobs || [];
  const visibleJobs = state.jobs.filter(
    (j) =>
      j.stage !== "review" &&
      !j.request?.wishlist_id &&
      ($("#show-history").checked ||
        !["published", "rejected", "cancelled"].includes(j.stage)),
  );
  renderRecent();
  $("#jobs").innerHTML = visibleJobs.length
    ? visibleJobs
        .map(
          (j) =>
            `<article class="card"><div class="card-head"><div><span class="badge ${esc(j.stage)}">${esc({ process_queued: "Waiting to process", publish_queued: "Waiting to publish", review: "Ready for review", published: "In library", queued: "Queued", undone: "Taken back" }[j.stage] || j.stage.replaceAll("_", " "))}</span><h3>${esc(jobTitle(j))}</h3>${meta([sourceLabel(j.source), j.created_at ? new Date(j.created_at).toLocaleString() : ""])}</div></div>${["queued", "downloading", "process_queued", "processing", "publish_queued", "publishing"].includes(j.stage) ? `<progress ${Number.isFinite(Number(j.progress)) && j.progress != null ? `value="${Math.max(0, Math.min(100, Number(j.progress)))}" max="100"` : ""}></progress>` : ""}${j.detail ? `<p class="muted">${esc(typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail))}</p>` : ""}${j.error ? `<p class="error">${esc(j.error)}</p><p class="muted">${esc(j.resume_stage === "publishing" ? "Retry adding to the library. Your approved edits are saved." : j.failed_stage === "processing" || j.source === "existing" ? "The download is saved. Retry processing without downloading again." : "Retry this source to resume. If the uploader is unavailable, search for another copy.")}</p>` : ""}<div class="actions">${j.stage === "review" ? `<button data-tab="review">Review download</button>` : ["failed", "cancelled"].includes(j.stage) ? `<button data-retry="${esc(j.id)}" data-stage="${j.resume_stage === "publishing" ? "publishing" : j.failed_stage === "processing" || j.source === "existing" ? "processing" : "download"}">Retry ${j.resume_stage === "publishing" ? "publishing" : j.failed_stage === "processing" || j.source === "existing" ? "processing" : "download"}</button>` : ""}${["queued", "downloading", "process_queued", "processing"].includes(j.stage) ? `<button class="quiet" data-cancel="${esc(j.id)}">Cancel</button>` : ""}</div>${j.stage === "published" && j.skipped_count ? `<p class="muted">${j.skipped_count} unselected track${j.skipped_count === 1 ? "" : "s"} kept privately.</p>` : ""}${j.stage === "published" && (j.files || []).length ? `<details><summary>${j.files.length} published file${j.files.length === 1 ? "" : "s"}</summary>${j.files.map((f) => `<p class="muted">${esc(f.title || f.filename || "Track")}</p><div class="actions">${downloads(f)}</div>`).join("")}</details>` : ""}</article>`,
        )
        .join("")
    : '<div class="empty">Nothing here.</div>';
}
const RECENT_DAYS = 7;
function addedBy(who) {
  who = String(who || "");
  if (who === "auto") return "Added automatically";
  if (who.startsWith("model:")) return "Added by the review model";
  if (who.startsWith("agent:")) return `Added by your agent (${who.slice(6)})`;
  return "Added by you";
}
function renderRecent() {
  const since = Date.now() - RECENT_DAYS * 86400000;
  const recent = state.jobs.filter(
    (j) =>
      (j.stage === "published" || (j.stage === "rejected" && j.auto_skipped)) &&
      new Date(j.created_at).getTime() >= since,
  );
  $("#recent").innerHTML = recent.length
    ? recent
        .map((j) => {
          const files = j.files || [];
          const how = j.stage === "rejected" ? "Skipped: every track is already in your library" : addedBy(j.approved_by);
          const replaced = (j.replaced || []).length;
          return `<article class="recent"><div class="recent-head"><div><strong>${esc(jobTitle(j))}</strong><span class="muted">${esc([how, j.stage === "published" ? `${files.length} track${files.length === 1 ? "" : "s"}` : "", replaced ? `replaced ${replaced} older cop${replaced === 1 ? "y" : "ies"}` : "", new Date(j.created_at).toLocaleDateString(undefined, { day: "numeric", month: "short" })].filter(Boolean).join(" · "))}</span></div>${j.stage === "published" ? `<button class="quiet" data-undo-job="${esc(j.id)}">Undo${files.length > 1 ? " all" : ""}</button>` : ""}</div>${files.length > 1 ? `<details><summary>Show tracks</summary>${files.map((f) => `<div class="recent-track"><span>${esc(f.title || f.filename || "Track")}<span class="muted"> · ${esc(f.artist || "")}</span></span>${playButton(f.id, f.title || "Track", f.artist)}<button class="quiet" data-undo-job="${esc(j.id)}" data-undo-file="${esc(f.id)}">Undo</button></div>`).join("")}</details>` : files.length ? `<div class="recent-track"><span>${esc(files[0].title || files[0].filename || "Track")}<span class="muted"> · ${esc(files[0].artist || "")}</span></span>${playButton(files[0].id, files[0].title || "Track", files[0].artist)}</div>` : ""}</article>`;
        })
        .join("")
    : '<div class="empty">Nothing added in the last week.</div>';
}
$("#show-history").addEventListener("change", () => loadJobs().catch((e) => toast(e.message, true)));
document.addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  if (button.dataset.retry)
    busy(button, async () => {
      await api(`/api/jobs/${path(button.dataset.retry)}/retry`, { stage: button.dataset.stage });
      await loadJobs();
    });
  else if (button.dataset.cancel)
    busy(button, async () => {
      await api(`/api/jobs/${path(button.dataset.cancel)}/cancel`, {});
      await Promise.all([loadJobs(), loadWishlist()]);
    });
  else if (button.dataset.undoJob)
    busy(button, async () => {
      const result = await api(`/api/jobs/${path(button.dataset.undoJob)}/undo`, button.dataset.undoFile ? { file_ids: [button.dataset.undoFile] } : {});
      toast(`Taken out of the library and back in Needs you.${result.restored ? " Your replaced version is back." : ""}`);
      state.reviewSignature = "";
      await Promise.all([loadJobs(), loadSummary()]);
    });
});
logoutHooks.push(() => {
  state.jobs = [];
  $("#jobs").innerHTML = "";
  $("#recent").innerHTML = "";
});
