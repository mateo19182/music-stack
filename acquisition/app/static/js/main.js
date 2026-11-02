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
document.addEventListener("click", (e) => {
  document.querySelectorAll(".more-actions[open]").forEach((menu) => {
    if (!menu.contains(e.target)) menu.open = false;
  });
  const target = e.target.closest("[data-tab]");
  if (!target) return;
  e.preventDefault();
  state.listFilter = target.dataset.list ?? (target.closest("nav") ? "" : state.listFilter);
  if (target.dataset.group) state.requestGroup = target.dataset.group;
  showTab(target.dataset.tab);
});
async function start() {
  const user = await api("/api/me");
  state.authenticated = true;
  state.isAdmin = Boolean(user.is_admin ?? user.isAdmin);
  for (const el of document.querySelectorAll("[data-admin]")) el.hidden = !state.isAdmin;
  if (user.navidromeUrl) $("#listen").href = safeLink(user.navidromeUrl);
  $("#username").textContent = user.username;
  $("#login-view").hidden = true;
  $("#app").hidden = false;
  showTab(location.hash.slice(1) || "overview", { push: false });
  const results = await Promise.allSettled([loadSummary(), loadSoulseekStatus()]);
  for (const result of results)
    if (result.status === "rejected") toast(result.reason.message, true);
}
start().catch(() => loginView());
// The counts in the menu and the open view refresh on their own; hidden tabs are not polled.
setInterval(() => {
  if (!state.authenticated || document.hidden) return;
  loadSoulseekStatus().catch(() => {});
  loadSummary().catch(() => {});
  Promise.resolve(views[state.tab]?.poll?.()).catch(() => {});
}, 5000);
