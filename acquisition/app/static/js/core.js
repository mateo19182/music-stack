"use strict";
const $ = (s) => document.querySelector(s);
const state = {
  tab: "overview",
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
  searchError: null,
  searchSource: "all",
  link: null,
  libraryGeneration: 0,
  concertsGeneration: 0,
  libraryFiles: {},
  analysisRunning: false,
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
      : source === "torrent"
        ? "Torrent"
        : String(source || "");
const duration = (n) =>
  n
    ? n >= 3600
      ? `${Math.floor(n / 3600)} h ${Math.floor((n % 3600) / 60)} min`
      : `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, "0")}`
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
// Each view registers what it clears when the session ends.
const logoutHooks = [];
function loginView() {
  state.authenticated = false;
  state.isAdmin = false;
  for (const hook of logoutHooks) hook();
  $("#app").hidden = true;
  $("#login-view").hidden = false;
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
    if (typeof data.detail?.message === "string")
      throw Object.assign(new Error(data.detail.message), {
        fileId: data.detail.file_id,
        field: data.detail.field,
      });
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
// Views by name; each module registers { load, poll } here. The URL hash names the view.
const views = {};
const VIEW_ALIASES = { review: "needs", activity: "requests", inbox: "settings", saved: "unrequested" };
function showTab(tab, { push = true } = {}) {
  tab = VIEW_ALIASES[tab] || tab;
  if (!views[tab]) tab = "overview";
  state.tab = tab;
  for (const name of Object.keys(views)) {
    $(`#${name}-view`).hidden = name !== tab;
    const navButton = $(`nav [data-tab="${name}"]`);
    navButton?.classList.toggle("selected", name === tab);
    navButton?.setAttribute("aria-current", name === tab ? "page" : "false");
  }
  if (push && location.hash.slice(1) !== tab) history.pushState(null, "", `#${tab}`);
  Promise.resolve(views[tab].load?.()).catch((e) => toast(e.message, true));
}
window.addEventListener("popstate", () => state.authenticated && showTab(location.hash.slice(1), { push: false }));
const plural = (n, one, many = `${one}s`) => `${Number(n).toLocaleString()} ${n === 1 ? one : many}`;
function meta(items) {
  return `<div class="meta">${items
    .filter((v) => v !== "" && v != null)
    .map((v) => `<span>${esc(v)}</span>`)
    .join("")}</div>`;
}
function tagValue(value) {
  return value == null
    ? ""
    : Array.isArray(value)
      ? value.join(", ")
      : String(value);
}
function relativeTime(seconds) {
  const diff = Math.round(seconds - Date.now() / 1000);
  const hours = Math.round(Math.abs(diff) / 3600);
  const text = Math.abs(diff) < 3600 ? `${Math.max(1, Math.round(Math.abs(diff) / 60))} min` : `${hours} h`;
  return diff > 0 ? `in ${text}` : `${text} ago`;
}
