function downloads(file, editable = false) {
  return `<button class="quiet" data-preview="${esc(file.id)}" data-title="${esc(file.title || file.filename || "Track")}" data-artist="${esc(file.artist || "")}">Play</button><a href="/api/files/${path(file.id)}/download">Download</a><details class="more-actions"><summary aria-label="More actions for ${esc(file.title || "track")}">More</summary><div>${file.published && file.shared !== undefined ? (state.isAdmin ? `<label class="checkbox"><input type="checkbox" data-share-file="${esc(file.id)}" ${file.shared ? "checked" : ""}>Share on Soulseek</label><span class="error sharing-file-error" role="alert"></span>` : `<span class="muted">${file.shared ? "Selected for sharing" : "Private on Soulseek"}</span>`) : ""}${editable && state.isAdmin ? `<button class="quiet" data-edit-tags="${esc(file.id)}">Edit tags</button>` : ""}${file.album_id ? `<a href="/api/albums/${path(file.album_id)}/download">Album ZIP</a>` : ""}${file.navidrome_url ? `<a href="${safeLink(file.navidrome_url)}" target="_blank" rel="noopener">Open in Navidrome ↗</a>` : ""}<span class="muted">${esc([file.format, file.bitrate ? `${file.bitrate} kbps` : "", duration(file.duration), bytes(file.size)].filter(Boolean).join(" · "))}</span></div></details>`;
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
async function loadDuplicates() {
  const data = await api("/api/library/duplicates");
  if (!state.authenticated) return;
  state.libraryGeneration++;
  state.libraryFiles = Object.fromEntries(data.groups.flat().map((f) => [f.id, f]));
  $("#library-count").textContent = `${data.extra} extra ${data.extra === 1 ? "copy" : "copies"} of ${data.groups.length} songs`;
  $("#pagination").innerHTML = "";
  const back = '<button type="button" class="quiet" data-close-duplicates>Back to library</button>';
  $("#library").innerHTML = data.groups.length
    ? `<div class="panel"><p class="muted">Each song here is in your library more than once: same artist and title, lengths within 3 seconds. The best copy (lossless, then bitrate, then the fuller album) is kept; the ticked copies go to the app's trash, where they can be restored by hand. Untick a copy that is really another version.</p><div class="actions"><button type="button" data-remove-duplicates>Move ticked copies to trash</button>${back}</div></div>` +
      data.groups
        .map(
          (group) =>
            `<section class="duplicate-group">${group
              .map(
                (f, i) =>
                  `<article class="library-row"><div class="track-info"><h3>${esc(f.title || f.filename || "Untitled")}</h3><span class="muted">${esc([f.artist, f.filename].filter(Boolean).join(" · "))}</span></div><span class="album-name muted">${esc(f.album || "")}</span><label class="checkbox"><input type="checkbox" data-duplicate="${esc(f.id)}" ${i ? "checked" : ""}>${i ? "Extra copy" : "Keep (best)"}</label><div class="actions">${downloads(f)}</div></article>`,
              )
              .join("")}</section>`,
        )
        .join("")
    : `<div class="empty">No song is in your library twice. ${back}</div>`;
}
async function removeDuplicates() {
  const ids = [...document.querySelectorAll("[data-duplicate]:checked")].map((input) => input.dataset.duplicate);
  if (!ids.length) return toast("Tick the copies to remove.", true);
  if (!confirm(`Move ${ids.length} ${ids.length === 1 ? "copy" : "copies"} to the trash?`)) return;
  for (let i = 0; i < ids.length; i += 50)
    await api("/api/library/remove", { file_ids: ids.slice(i, i + 50) });
  toast(`Moved ${ids.length} to the trash.`);
  await loadDuplicates();
}
$("#library-duplicates").addEventListener("click", (e) => busy(e.currentTarget, loadDuplicates));
$("#library-form").addEventListener("submit", (e) => {
  e.preventDefault();
  state.page = 1;
  busy(e.submitter, loadLibrary);
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
$("#reset-filters").addEventListener("click", () => {
  $("#library-form").reset();
  state.page = 1;
  loadLibrary().catch((e) => toast(e.message, true));
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
document.addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  if (button.dataset.editTags) {
    const row = button.closest(".library-row");
    button.closest(".more-actions").open = false;
    if (row.nextElementSibling?.dataset.tagForm) {
      row.nextElementSibling.querySelector("input").focus();
      return;
    }
    row.insertAdjacentHTML("afterend", tagEditor(state.libraryFiles[button.dataset.editTags]));
    row.nextElementSibling.querySelector("input").focus();
  } else if (button.dataset.closeTags !== undefined) button.closest("form").remove();
  else if (button.dataset.removeDuplicates !== undefined) busy(button, removeDuplicates);
  else if (button.dataset.closeDuplicates !== undefined) busy(button, loadLibrary);
  else if (button.dataset.page) {
    state.page = Number(button.dataset.page);
    busy(button, async () => {
      await loadLibrary();
      $("#library-view").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
});
views.library = { load: loadLibrary };
logoutHooks.push(() => {
  state.libraryGeneration++;
  $("#library").innerHTML = "";
});
