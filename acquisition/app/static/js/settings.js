async function loadAutoAdd() {
  const settings = await api("/api/settings/auto-add");
  document.querySelectorAll("[data-auto-add]").forEach((input) => (input.checked = !!settings[input.dataset.autoAdd]));
}
document.querySelectorAll("[data-auto-add]").forEach((input) =>
  input.addEventListener("change", async () => {
    const body = Object.fromEntries([...document.querySelectorAll("[data-auto-add]")].map((i) => [i.dataset.autoAdd, i.checked]));
    try {
      await api("/api/settings/auto-add", body);
      toast(input.checked ? "New downloads that need nothing from you go straight into the library." : "New downloads wait for you in Needs you.");
    } catch (error) {
      input.checked = !input.checked;
      toast(error.message, true);
    }
  }),
);
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
    toast("Selected files queued for preparation. Follow them in Requests → Other downloads.");
    state.requestGroup = "other";
    showTab("requests");
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
document.addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (button?.id === "start-analysis")
    busy(button, async () => {
      await api("/api/library/analysis", {});
      toast("Analyzing library files with missing tags.");
      await loadAnalysis();
    });
  else if (button?.id === "cancel-analysis")
    busy(button, async () => {
      await api("/api/library/analysis/cancel", {});
      await loadAnalysis();
    });
});
views.settings = {
  load: async () => {
    await Promise.all([loadAutoAdd(), state.isAdmin && loadSharing().catch(showSharingError), state.isAdmin && loadAnalysis()]);
  },
  poll: async () => {
    if (state.isAdmin && !$("#sharing-enabled").disabled) await loadSharing().catch(showSharingError);
    if (state.analysisRunning) await loadAnalysis();
  },
};
