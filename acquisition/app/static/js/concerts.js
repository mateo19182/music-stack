const CONCERT_STATUS = { offsale: "Not on sale", cancelled: "Cancelled", postponed: "Postponed", rescheduled: "Rescheduled" };
const concertDay = (value) => {
  const [y, m, d] = String(value).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short", year: "numeric" });
};
const madridTime = (iso) =>
  iso ? new Date(iso).toLocaleString("en-GB", { timeZone: "Europe/Madrid", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
function concertRow(e) {
  const status = ["onsale", "listed"].includes(e.status) ? "" : CONCERT_STATUS[e.status] || e.status;
  const links = [
    e.url ? `<a href="${safeLink(e.url)}" target="_blank" rel="noopener noreferrer">Event / tickets ↗</a>` : "",
    ...e.alternates.map((a) => `<a href="${safeLink(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.source)} ↗</a>`),
  ].filter(Boolean).join("");
  return `<li class="concert-date"><div class="concert-when"><strong>${esc(e.date_unknown ? "Date TBA" : concertDay(e.date))}</strong>${e.time ? `<span>${esc(e.time)}</span>` : ""}</div><div class="concert-where"><div>${e.highlight ? `<span class="concert-star" title="${esc(e.highlight)}">★</span> ` : ""}<strong>${esc(e.city || "Unknown city")}</strong>${e.venue ? ` · ${esc(e.venue)}` : ""}${status ? ` <span class="badge failed">${esc(status)}</span>` : ""}</div>${e.show_title ? `<div class="muted">${esc(e.name)}</div>` : ""}</div><div class="concert-links">${links}<span class="muted">${esc(e.source)}</span></div></li>`;
}
function concertCard(g) {
  const count = g.dates.length;
  const hidden = g.hidden.length;
  const info = [g.multi ? "Shared bill" : "", count ? `${count} ${count === 1 ? "date" : "dates"}` : "", hidden ? `${hidden} outside preferred areas` : ""];
  const more = hidden
    ? `<details class="concert-hidden"${count ? "" : " open"}><summary>${count ? `Show ${hidden} other ${hidden === 1 ? "date" : "dates"}` : `${hidden} ${hidden === 1 ? "date" : "dates"}, all outside preferred areas`}</summary><ul class="concert-dates">${g.hidden.map(concertRow).join("")}</ul></details>`
    : "";
  return `<article class="card concert-card${count ? "" : " concert-collapsed"}"><h3>${esc(g.artists)}${g.title ? ` <span class="muted">— ${esc(g.title)}</span>` : ""}</h3>${meta(info)}${count ? `<ul class="concert-dates">${g.dates.map(concertRow).join("")}</ul>` : ""}${more}</article>`;
}
async function loadConcerts() {
  const generation = ++state.concertsGeneration;
  const form = formData($("#concerts-form"));
  const query = new URLSearchParams({ area: form.area, q: form.q.trim(), days: form.days });
  const data = await api(`/api/concerts?${query}`);
  if (generation !== state.concertsGeneration) return;
  const sources = data.sources.map((s) =>
    s.status === "ok" ? `${s.name}: ok${s.cached ? " (cached)" : ""}${s.pending ? ` (${s.pending} pages still to read)` : ""}` : `${s.name}: unavailable${s.error ? ` (${s.error})` : ""}`);
  $("#concerts-check").textContent = [
    data.checked_at ? `Last checked ${madridTime(data.checked_at)}` : "Not checked yet",
    `next check ${madridTime(data.next_check)} (${data.schedule})`,
    ...sources,
  ].join(" · ");
  $("#concerts-notice").hidden = !data.notices.length;
  $("#concerts-notice").innerHTML = data.notices.map((n) => `<p>${esc(n)}</p>`).join("");
  const c = data.counts;
  const limit = data.settings.max_nationwide_dates;
  $("#concerts-summary").textContent = data.status === "ok" || data.status === "stale"
    ? [`${c.shown} ${c.shown === 1 ? "date" : "dates"} shown`, c.hidden ? `${c.hidden} hidden` : "",
       limit ? `artists with more than ${limit} upcoming dates show only Galicia, Madrid and Barcelona; expand a card for the rest` : ""].filter(Boolean).join(" · ")
    : "";
  $("#concerts").innerHTML = data.groups.length
    ? data.groups.map(concertCard).join("") + (data.coverage ? `<p class="muted">${esc(data.coverage)}</p>` : "")
    : `<p class="empty">${data.status === "ok" || data.status === "stale" ? "No upcoming concerts match these filters." : "No concerts to show."}</p>`;
}
let concertsTimer;
$("#concerts-form").addEventListener("submit", (e) => e.preventDefault());
$("#concerts-form").addEventListener("change", () => loadConcerts().catch((e) => toast(e.message, true)));
$("#concerts-form [name=q]").addEventListener("input", () => {
  clearTimeout(concertsTimer);
  concertsTimer = setTimeout(() => loadConcerts().catch((e) => toast(e.message, true)), 250);
});
$("#refresh-concerts").addEventListener("click", (e) => busy(e.target, loadConcerts));
views.concerts = { load: loadConcerts };
logoutHooks.push(() => {
  state.concertsGeneration++;
  $("#concerts").innerHTML = "";
});
