// Users (admins only): who may use Acquire and whose downloads go in on their own. Accounts stay in Navidrome.
async function loadUsers() {
  if (!state.isAdmin) return;
  const data = await api("/api/users");
  $("#navidrome-users").href = safeLink(`${data.navidrome_url.replace(/\/$/, "")}/app/#/user`);
  const when = (value) => (value ? new Date(value).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "never");
  const toggle = (u, field, checked, label, disabled = false) =>
    `<label class="checkbox"><input type="checkbox" data-user="${esc(u.username)}" data-field="${field}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""} aria-label="${esc(label)} for ${esc(u.username)}" /></label>`;
  $("#users").innerHTML = `<table class="lists users"><thead><tr><th>User</th><th>Role</th><th>Last login</th><th>Can use Acquire</th><th>Auto-add requests</th><th>Auto-add imports</th><th>Requests</th><th>In review</th></tr></thead><tbody>${data.users
    .map((u) => `<tr${u.access ? "" : ' class="no-access"'}><th scope="row">${esc(u.name || u.username)}${u.name && u.name !== u.username ? ` <span class="muted">${esc(u.username)}</span>` : ""}</th><td>${u.is_admin ? "Admin" : "Listener"}</td><td>${esc(when(u.last_login))}</td><td>${u.is_admin ? '<span class="muted">Always</span>' : toggle(u, "access", u.access, "Can use Acquire")}</td><td>${toggle(u, "search", u.auto_add.search, "Auto-add requests", !u.access)}</td><td>${toggle(u, "import", u.auto_add.import, "Auto-add imports", !u.access)}</td><td>${u.requests.toLocaleString()}</td><td>${u.reviews ? `<button class="link" data-tab="needs">${u.reviews}</button>` : '<span class="muted">0</span>'}</td></tr>`)
    .join("")}</tbody></table>`;
}
$("#users").addEventListener("change", async (e) => {
  const input = e.target.closest("[data-user]");
  if (!input) return;
  input.disabled = true;
  try {
    await api(`/api/users/${path(input.dataset.user)}`, { [input.dataset.field]: input.checked });
    toast(input.dataset.field === "access"
      ? `${input.dataset.user} ${input.checked ? "can now use Acquire" : "can no longer use Acquire; listening in Navidrome is unchanged"}.`
      : "Saved.");
    await Promise.all([loadUsers(), loadAutoAdd()]);
  } catch (error) {
    input.checked = !input.checked;
    input.disabled = false;
    toast(error.message, true);
  }
});
logoutHooks.push(() => ($("#users").innerHTML = ""));
