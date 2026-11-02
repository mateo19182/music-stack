// Soulseek outage: downloads wait instead of failing; say so until the connection is back.
async function loadSoulseekStatus() {
  const s = await api("/api/soulseek/status");
  const banner = $("#soulseek-status");
  banner.hidden = s.connected;
  if (s.connected) return;
  const since = s.down_since ? ` since ${new Date(s.down_since * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
  const until = s.paused_until ? ` until ${new Date(s.paused_until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
  const why = s.logged_in ? `Several Soulseek connection errors in a row; new downloads paused${until}` : `Soulseek is disconnected${since}`;
  banner.textContent = `${why}. ${s.waiting} downloads are waiting and resume on their own when it recovers.`;
}

// Uploaders whose anti-leech plugin asks for a human check. The user types the answer.
async function loadAgentRequests() {
  if (!state.isAdmin) return;
  const data = await api("/api/agents/requests");
  const signature = JSON.stringify(data.requests || []);
  if (signature === state.agentRequestsSignature) return;
  state.agentRequestsSignature = signature;
  $("#agent-requests").innerHTML = (data.requests || [])
    .map(
      (r) =>
        `<article class="card check"><div class="card-head"><div><span class="badge failed">An agent asks for access</span><h3>${esc(r.requested_by || "Agent")} wants a token “${esc(r.name)}”</h3>${meta([`Code ${r.code}`, r.expires ? `until ${new Date(r.expires * 1000).toLocaleTimeString()}` : ""])}</div></div><p class="muted">${r.can_approve ? "It could add downloads to your library and reject them, without asking you each time." : "Same rights as the agent already has."} Allow only if you asked for this and the code matches what the agent told you.</p><div class="actions"><button data-agent-request="${esc(r.id)}" data-decision="allow">Allow</button><button class="quiet" data-agent-request="${esc(r.id)}" data-decision="deny">Deny</button></div></article>`,
    )
    .join("");
}
document.addEventListener("click", (e) => {
  const button = e.target.closest("[data-agent-request]");
  if (!button) return;
  busy(button, async () => {
    await api(`/api/agents/requests/${path(button.dataset.agentRequest)}/${button.dataset.decision}`, {});
    toast(button.dataset.decision === "allow" ? "Allowed. The agent picks up its token." : "Denied.");
    state.agentRequestsSignature = null;
    await loadAgentRequests();
  });
});
async function loadChecks() {
  if (!state.isAdmin) return;
  const data = await api("/api/soulseek/checks");
  const signature = JSON.stringify(data.checks || []);
  if (document.activeElement?.closest("#checks") || signature === state.checksSignature) return;
  state.checksSignature = signature;
  $("#checks").innerHTML = (data.checks || [])
    .map((c) => {
      const jobs = c.jobs || [];
      const since = c.since ? new Date(c.since).toLocaleString() : "";
      const recent = (c.recent || [])
        .map((m) => `<p class="${m.direction === "Out" ? "muted" : ""}"><strong>${m.direction === "Out" ? "You" : esc(c.username)}:</strong> ${esc(m.message)}</p>`)
        .join("");
      return `<article class="card check"><div class="card-head"><div><span class="badge ${c.status === "open" ? "failed" : "queued"}">${c.status === "open" ? "Uploader asks you to verify" : "Answered"}</span><h3>${esc(c.username)}</h3>${meta([since, jobs.length ? `${jobs.length} download${jobs.length === 1 ? "" : "s"} waiting on this` : ""])}</div></div>${c.status === "open" ? `<blockquote>${esc(c.message)}</blockquote><p class="muted">Soulseek uploaders use this to keep bots out. Read it and type the answer yourself; a wrong answer can lock you out for a day.</p><form class="check-reply" data-check-reply="${esc(c.username)}"><input name="message" maxlength="200" autocomplete="off" aria-label="Your answer to ${esc(c.username)}" required /><button>Send answer</button></form>` : `<details open><summary>Conversation</summary>${recent}</details><p class="muted">Check their reply, then retry.</p>`}${jobs.length ? `<details><summary>Blocked downloads</summary>${jobs.map((j) => `<p class="muted">${esc(j.label || j.id)}</p>`).join("")}</details><div class="actions"><button ${c.status === "open" ? 'class="quiet"' : ""} data-retry-check="${esc(jobs.map((j) => j.id).join(","))}">Retry ${jobs.length === 1 ? "download" : `${jobs.length} downloads`}</button></div>` : ""}</article>`;
    })
    .join("");
}
document.addEventListener("submit", (e) => {
  const form = e.target.closest("[data-check-reply]");
  if (!form) return;
  e.preventDefault();
  busy(form.querySelector("button"), async () => {
    await api(`/api/soulseek/checks/${path(form.dataset.checkReply)}/reply`, formData(form));
    toast("Answer sent.");
    form.reset();
    document.activeElement?.blur();
    state.checksSignature = null;
    await loadChecks();
  });
});
document.addEventListener("click", (e) => {
  const button = e.target.closest("button[data-retry-check]");
  if (!button) return;
  busy(button, async () => {
    for (const id of button.dataset.retryCheck.split(","))
      await api(`/api/jobs/${path(id)}/retry`, { stage: "download" });
    toast("Downloads queued again.");
    state.checksSignature = null;
    await loadChecks();
  });
});
// Requests holding at the copies they found, for whoever chooses first: you or your agent.
async function loadChoices() {
  const data = await api("/api/requests?status=choice");
  const signature = JSON.stringify(data.items);
  if (signature === state.choicesSignature) return;
  state.choicesSignature = signature;
  $("#choices").innerHTML = data.items.map(wishRow).join("");
  renderNeedsEmpty();
}
function renderNeedsEmpty() {
  const empty = ["#agent-requests", "#checks", "#choices", "#reviews"].every((id) => !$(id).innerHTML.trim());
  $("#needs-empty").hidden = !empty;
  $("#review-help").hidden = !$("#reviews").innerHTML.trim();
}
const loadNeeds = () => Promise.all([loadAgentRequests(), loadChecks(), loadChoices(), loadReview()]).then(renderNeedsEmpty);
views.needs = { load: loadNeeds, poll: loadNeeds };
logoutHooks.push(() => {
  for (const key of ["agentRequestsSignature", "checksSignature", "choicesSignature"]) state[key] = null;
  for (const id of ["#agent-requests", "#checks", "#choices"]) $(id).innerHTML = "";
});
