// Not requested yet: tracks saved in your lists that are not in the library. Nothing is searched until chosen here.
const unreq = { items: [], loaded: false, groups: [], selected: new Set(), shown: 40 };
const UNREQ_GROUPS = {
  artist: (i) => i.artist || "Unknown artist",
  album: (i) => `${i.artist || "Unknown artist"} — ${i.album || "No album"}`,
  list: (i) => i.list,
};
async function loadUnrequested(force = false) {
  if (!unreq.loaded || force) {
    unreq.items = (await api("/api/requests/suggested")).items;
    unreq.loaded = true;
    const ids = new Set(unreq.items.map((i) => i.id));
    unreq.selected = new Set([...unreq.selected].filter((id) => ids.has(id)));
  }
  const lists = [...new Set(unreq.items.map((i) => i.list))];
  const select = $("#unreq-list");
  select.innerHTML = `<option value="">All lists</option>${lists.map((l) => `<option ${l === state.listFilter ? "selected" : ""}>${esc(l)}</option>`).join("")}`;
  select.value = lists.includes(state.listFilter) ? state.listFilter : "";
  renderUnrequested();
}
function unreqMatching() {
  const words = $("#unreq-filter").value.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const list = $("#unreq-list").value;
  return unreq.items.filter((i) => (!list || i.list === list) &&
    words.every((w) => `${i.artist} ${i.title} ${i.album || ""}`.toLocaleLowerCase().includes(w)));
}
function renderUnrequested() {
  const by = $("#unreq-group").value;
  const matching = unreqMatching();
  const groups = new Map();
  for (const i of matching) {
    const key = UNREQ_GROUPS[by](i);
    groups.has(key) ? groups.get(key).push(i) : groups.set(key, [i]);
  }
  unreq.groups = [...groups].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  const shown = unreq.groups.slice(0, unreq.shown);
  const trackLabel = (t) => by === "artist" ? esc(t.title) + (t.album ? ` <span class="muted">· ${esc(t.album)}</span>` : "")
    : by === "album" ? esc(t.title) : `${esc(t.artist)} — ${esc(t.title)}`;
  $("#unreq-items").innerHTML = shown.length
    ? shown.map(([name, tracks], n) => {
        const all = tracks.every((t) => unreq.selected.has(t.id));
        const lists = by === "list" ? "" : [...new Set(tracks.map((t) => t.list))].join(", ");
        return `<div class="suggest-artist"><label class="checkbox"><input type="checkbox" data-unreq-group="${n}" ${all ? "checked" : ""}> <strong>${esc(name)}</strong> <span class="muted">${plural(tracks.length, "track")}${lists ? ` · ${esc(lists)}` : ""}</span></label><details class="suggest-tracks-wrap" ${tracks.length <= 6 ? "open" : ""}><summary class="muted">Tracks</summary><div class="suggest-tracks">${tracks
          .map((t) => `<label class="checkbox"><input type="checkbox" data-unreq="${esc(t.id)}" ${unreq.selected.has(t.id) ? "checked" : ""}> <span>${trackLabel(t)}</span></label>`)
          .join("")}</div></details></div>`;
      }).join("") + (unreq.groups.length > shown.length ? `<button class="quiet" id="unreq-more">Show more (${unreq.groups.length - shown.length} groups left)</button>` : "")
    : `<div class="empty">${unreq.items.length ? "Nothing matches the filter." : "Nothing left: every saved track is requested or in your library."}</div>`;
  const count = unreq.selected.size;
  $("#unreq-summary").textContent = `${plural(matching.length, "track")}${matching.length !== unreq.items.length ? ` of ${unreq.items.length.toLocaleString()}` : ""} · ${plural(unreq.groups.length, by === "list" ? "list" : by)}`;
  $("#unreq-selected").textContent = count ? `${count.toLocaleString()} selected` : "";
  $("#unreq-select-all").textContent = `Select all ${matching.length.toLocaleString()}`;
  for (const id of ["#unreq-get", "#unreq-albums", "#unreq-drop", "#unreq-clear"]) $(id).disabled = !count;
}
const rerenderUnrequested = () => {
  unreq.shown = 40;
  renderUnrequested();
};
$("#unreq-filter").addEventListener("input", rerenderUnrequested);
$("#unreq-group").addEventListener("change", rerenderUnrequested);
$("#unreq-list").addEventListener("change", (e) => {
  state.listFilter = e.target.value;
  rerenderUnrequested();
});
$("#unreq-items").addEventListener("change", (e) => {
  const box = e.target;
  if (box.dataset.unreq) box.checked ? unreq.selected.add(box.dataset.unreq) : unreq.selected.delete(box.dataset.unreq);
  else if (box.dataset.unreqGroup)
    for (const t of unreq.groups[Number(box.dataset.unreqGroup)][1])
      box.checked ? unreq.selected.add(t.id) : unreq.selected.delete(t.id);
  renderUnrequested();
});
$("#unreq-items").addEventListener("click", (e) => {
  if (e.target.id !== "unreq-more") return;
  unreq.shown += 40;
  renderUnrequested();
});
$("#unreq-select-all").addEventListener("click", () => {
  for (const i of unreqMatching()) unreq.selected.add(i.id);
  renderUnrequested();
});
$("#unreq-clear").addEventListener("click", () => {
  unreq.selected.clear();
  renderUnrequested();
});
for (const [button, get, album] of [["#unreq-get", true, false], ["#unreq-albums", true, true], ["#unreq-drop", false, false]])
  $(button).addEventListener("click", (e) => {
    const ids = [...unreq.selected];
    if (!ids.length) return;
    if (!get && ids.length > 20 && !confirm(`Remove ${ids.length.toLocaleString()} tracks from this list for good? They will not be offered again.`)) return;
    busy(e.target, async () => {
      let count = 0;
      for (let n = 0; n < ids.length; n += 2000) count += (await api("/api/requests/choose", { ids: ids.slice(n, n + 2000), get, album })).count;
      toast(get ? `${plural(count, "request")} added; they are searched a few at a time. Follow them in Requests.` : `${plural(count, "track")} removed.`);
      unreq.selected.clear();
      await Promise.all([loadUnrequested(true), loadSummary()]);
    });
  });
views.unrequested = { load: () => loadUnrequested(true) };
logoutHooks.push(() => {
  Object.assign(unreq, { items: [], loaded: false, groups: [], shown: 40 });
  unreq.selected.clear();
  $("#unreq-items").innerHTML = "";
});
