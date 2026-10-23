const state = { subscriptions: [], items: [], runId: null };
const $ = (id) => document.getElementById(id);

function showNotice(message, error = false) {
  const notice = $("notice");
  notice.textContent = message;
  notice.className = `notice${error ? " error" : ""}`;
  notice.hidden = !message;
}

async function api(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || `Request failed (${response.status})`);
  return body;
}

function badge(value, tone = "") {
  return `<span class="badge ${tone}">${String(value || "—").replaceAll("_", " ")}</span>`;
}

function renderRows() {
  const query = $("search").value.toLowerCase();
  const status = $("status-filter").value;
  const rows = $("subscription-rows");
  const source = state.items.length ? state.items.map((item) => item.subscription) : state.subscriptions;
  const filtered = source.filter((sub) =>
    (!status || sub.status === status) &&
    (!query || `${sub.service_name} ${sub.subscription_id}`.toLowerCase().includes(query)));
  $("loading").hidden = true;
  rows.innerHTML = filtered.length ? filtered.map((sub) => {
    const item = state.items.find((candidate) => candidate.subscription.subscription_id === sub.subscription_id);
    const decision = item?.decision;
    const workflow = item?.final_status || (decision?.eligibility === "candidate" ? "pending" : "not reviewed");
    const tone = workflow.includes("verified") ? "good" : (workflow.includes("failed") || workflow.includes("blocked") ? "bad" : "warn");
    return `<tr><td><strong>${sub.service_name}</strong><br><small>${sub.subscription_id}</small></td>
      <td>${badge(sub.status, sub.status === "active" ? "good" : "")}</td>
      <td>${sub.last_used_at ? new Date(sub.last_used_at).toLocaleString() : "Unknown"}</td>
      <td>${sub.renewal_date || "—"}</td><td>${badge(decision?.eligibility || "not reviewed")}</td>
      <td>${badge(workflow, tone)}</td><td><button class="button secondary" data-detail="${sub.subscription_id}">Review</button></td></tr>`;
  }).join("") : `<tr><td colspan="7" class="empty">No subscriptions match the current filters.</td></tr>`;
  rows.querySelectorAll("[data-detail]").forEach((button) => button.addEventListener("click", () => showDetail(button.dataset.detail)));
  updateCards();
}

function updateCards() {
  const items = state.items;
  $("total").textContent = state.subscriptions.length || items.length;
  $("inactive").textContent = items.filter((i) => i.decision?.eligibility === "candidate").length;
  $("review").textContent = items.filter((i) => i.decision?.eligibility === "needs_review").length;
  $("pending").textContent = items.filter((i) => i.plan?.status === "awaiting_approval").length;
  $("verified").textContent = items.filter((i) => i.final_status === "verified_cancelled").length;
  $("blocked").textContent = items.filter((i) => ["blocked", "failed"].includes(i.final_status)).length;
}

function showDetail(id) {
  const item = state.items.find((candidate) => candidate.subscription.subscription_id === id);
  const sub = item?.subscription || state.subscriptions.find((candidate) => candidate.subscription_id === id);
  if (!sub) return;
  $("detail").hidden = false; $("detail-title").textContent = sub.service_name; $("detail-subtitle").textContent = sub.subscription_id;
  const evidence = item?.evidence_list || [];
  const plan = item?.plan;
  $("detail-content").innerHTML = `<dl>
    <div><dt>Current status</dt><dd>${badge(sub.status)}</dd></div>
    <div><dt>Workflow status</dt><dd>${badge(item?.final_status || "not reviewed")}</dd></div>
    <div><dt>Last used</dt><dd>${sub.last_used_at ? new Date(sub.last_used_at).toLocaleString() : "Unknown — review required"}</dd></div>
    <div><dt>Renewal</dt><dd>${sub.renewal_date || "Not available"}</dd></div>
    <div><dt>Eligibility reasoning</dt><dd>${item?.decision?.reason || "Run a review to evaluate this subscription."}</dd></div>
    <div><dt>Consequences</dt><dd>${plan?.consequences_warning || "No warning recorded."}</dd></div>
  </dl><h3>Evidence</h3><ul class="evidence">${evidence.length ? evidence.map((e) => `<li><strong>${e.evidence_type}</strong>: ${e.excerpt}<br><small>${e.source_url || "No source URL"} · ${new Date(e.captured_at).toLocaleString()}</small></li>`).join("") : "<li>No evidence available.</li>"}</ul>
  ${plan ? `<div class="toolbar"><button id="approve" class="button primary">Approve exact plan</button><button id="keep" class="button secondary">Keep subscription</button></div>` : ""}`;
  $("approve")?.addEventListener("click", () => decide(plan, item, true));
  $("keep")?.addEventListener("click", () => decide(plan, item, false));
}

async function decide(plan, item, approve) {
  if (!plan || !confirm(approve ? `Approve cancellation for ${item.subscription.service_name}?` : `Keep ${item.subscription.service_name}?`)) return;
  const button = $(approve ? "approve" : "keep"); button.disabled = true;
  try {
    await api(`/plans/${plan.plan_id}/${approve ? "approve" : "decline"}`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(approve ? { plan_id: plan.plan_id, subscription_id: item.subscription.subscription_id, service_name: plan.service_name, plan_version: plan.plan_version, approved_by: "dashboard_user" } : { plan_id: plan.plan_id, plan_version: plan.plan_version, declined_by: "dashboard_user" }) });
    showNotice(approve ? "Approval recorded. The result remains unverified until independent evidence confirms cancellation." : "Subscription kept by user.");
    await loadRun();
  } catch (error) { showNotice(error.message, true); button.disabled = false; }
}

async function loadRun() {
  if (!state.runId) return renderRows();
  const response = await api(`/runs/${state.runId}/subscriptions`);
  state.items = response.items; $("run-label").textContent = `Run ${state.runId}`;
  renderRows();
}

async function startRun() {
  const button = $("start-run"); button.disabled = true;
  try {
    const run = await api("/runs", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ subscription_ids: state.subscriptions.map((s) => s.subscription_id) }) });
    state.runId = run.run_id; await loadRun(); showNotice("Review run created. No cancellation was executed.");
  } catch (error) { showNotice(error.message, true); } finally { button.disabled = false; }
}

async function loadSubscriptions() {
  try { state.subscriptions = await api("/subscriptions"); renderRows(); }
  catch (error) { $("loading").textContent = error.message; showNotice(error.message, true); }
}

$("refresh").addEventListener("click", loadSubscriptions);
$("start-run").addEventListener("click", startRun);
$("search").addEventListener("input", renderRows);
$("status-filter").addEventListener("change", renderRows);
$("close-detail").addEventListener("click", () => $("detail").hidden = true);
loadSubscriptions();
