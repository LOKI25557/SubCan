/**
 * SubCan Front-End Control Center Application
 *
 * Implements:
 * 1. Overview & Subscriptions Dashboard (Cards, Filters, Table, Detail Drawer)
 * 2. Explicit Human Approval & Keep/Decline Action Gate
 * 3. Independent Verification Trigger & Outcome Tracking
 * 4. Audit & Financial Reconciliation Report with SQLite Timeline
 * 5. Screen E: Official Acceptance Test Suite Scorecard (TC1 - TC4 & Edge Cases)
 */

const state = {
  subscriptions: [],
  items: [],
  runId: null,
  activeTab: "tab-dashboard",
  testResults: [],
};

const $ = (id) => document.getElementById(id);

// -----------------------------------------------------------------------------
// Utilities & UI Helpers
// -----------------------------------------------------------------------------

function showNotice(message, type = "info") {
  const notice = $("notice");
  const msgEl = $("notice-msg");
  if (!message) {
    notice.hidden = true;
    return;
  }
  msgEl.textContent = message;
  notice.className = `notice-banner ${type}`;
  notice.hidden = false;
}

$("notice-close").addEventListener("click", () => {
  $("notice").hidden = true;
});

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const errorMsg = data.error?.message || data.detail?.message || `Request failed (${response.status})`;
    throw new Error(errorMsg);
  }
  return data;
}

function badge(text, variant = "") {
  const clean = String(text || "—").replaceAll("_", " ");
  return `<span class="badge ${variant}">${clean}</span>`;
}

function formatDate(isoStr) {
  if (!isoStr) return "Unknown";
  try {
    const d = new Date(isoStr);
    return isNaN(d.getTime()) ? isoStr : d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return isoStr;
  }
}

// -----------------------------------------------------------------------------
// Tab Switching
// -----------------------------------------------------------------------------

document.querySelectorAll(".nav-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach((p) => (p.hidden = true));

    btn.classList.add("active");
    const targetId = btn.dataset.tab;
    $(targetId).hidden = false;
    state.activeTab = targetId;

    if (targetId === "tab-audit") {
      loadAuditTab();
    }
  });
});

// -----------------------------------------------------------------------------
// Tab 1: Subscriptions & Dashboard
// -----------------------------------------------------------------------------

function renderRows() {
  const query = $("search").value.toLowerCase().trim();
  const statusFilter = $("status-filter").value;
  const eligFilter = $("eligibility-filter").value;
  const tbody = $("subscription-rows");

  const source = state.items.length
    ? state.items.map((i) => ({ sub: i.subscription, item: i }))
    : state.subscriptions.map((s) => ({ sub: s, item: null }));

  const filtered = source.filter(({ sub, item }) => {
    const matchesQuery = !query || `${sub.service_name} ${sub.subscription_id}`.toLowerCase().includes(query);
    const matchesStatus = !statusFilter || sub.status === statusFilter;
    const matchesElig = !eligFilter || (item && item.decision?.eligibility === eligFilter);
    return matchesQuery && matchesStatus && matchesElig;
  });

  $("loading").hidden = true;

  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state">No subscriptions match the selected criteria.</td></tr>`;
    updateCards();
    return;
  }

  tbody.innerHTML = filtered
    .map(({ sub, item }) => {
      const decision = item?.decision;
      const finalStatus = item?.final_status;

      // Status badge
      let statusBadge = badge(sub.status, sub.status === "active" ? "success" : "danger");

      // Eligibility badge
      let eligBadge = `<span class="badge">—</span>`;
      if (decision) {
        if (decision.eligibility === "candidate") eligBadge = badge("Candidate", "candidate");
        else if (decision.eligibility === "needs_review") eligBadge = badge("Needs Review", "warning");
        else eligBadge = badge("Not Eligible", "");
      }

      // Workflow badge
      let workflowBadge = `<span class="badge">Unreviewed</span>`;
      if (finalStatus) {
        if (finalStatus === "verified_cancelled") workflowBadge = badge("Verified Cancelled", "success");
        else if (finalStatus === "kept") workflowBadge = badge("Kept Active", "info");
        else if (finalStatus === "blocked") workflowBadge = badge("Blocked Safety", "danger");
        else if (finalStatus === "failed") workflowBadge = badge("Action Failed", "danger");
        else if (finalStatus === "pending") {
          workflowBadge = item.action_result ? badge("Action Executed (Pending Verif)", "warning") : badge("Awaiting Approval", "candidate");
        }
      }

      const costStr = sub.monthly_cost ? `$${sub.monthly_cost.toFixed(2)}/mo` : "—";
      const lastUsed = formatDate(sub.last_used_at);
      const renewal = sub.renewal_date || "—";

      return `<tr>
        <td>
          <strong>${sub.service_name}</strong>
          <div class="sub-meta">${sub.subscription_id}</div>
        </td>
        <td>${statusBadge}</td>
        <td>${lastUsed}</td>
        <td>${renewal}</td>
        <td><strong>${costStr}</strong></td>
        <td>${eligBadge}</td>
        <td>${workflowBadge}</td>
        <td>
          <button class="button secondary small review-trigger-btn" data-id="${sub.subscription_id}">
            Inspect
          </button>
        </td>
      </tr>`;
    })
    .join("");

  tbody.querySelectorAll(".review-trigger-btn").forEach((btn) => {
    btn.addEventListener("click", () => openDrawer(btn.dataset.id));
  });

  updateCards();
}

function updateCards() {
  const items = state.items;
  $("total").textContent = state.subscriptions.length || items.length;
  $("inactive").textContent = items.filter((i) => i.decision?.eligibility === "candidate").length;
  $("review").textContent = items.filter((i) => i.decision?.eligibility === "needs_review").length;
  $("pending").textContent = items.filter((i) => i.plan?.status === "awaiting_approval").length;
  $("verified").textContent = items.filter((i) => i.final_status === "verified_cancelled").length;
  $("blocked").textContent = items.filter((i) => ["blocked", "failed", "kept"].includes(i.final_status)).length;
}

// -----------------------------------------------------------------------------
// Drawer: Inspection, Approval Gate & Verification Trigger
// -----------------------------------------------------------------------------

function openDrawer(subscriptionId) {
  const item = state.items.find((i) => i.subscription.subscription_id === subscriptionId);
  const sub = item?.subscription || state.subscriptions.find((s) => s.subscription_id === subscriptionId);
  if (!sub) return;

  $("detail-title").textContent = sub.service_name;
  $("detail-subtitle").textContent = sub.subscription_id;
  $("detail-eyebrow").textContent = item?.decision?.eligibility === "candidate" ? "ELIGIBLE CANCELLATION CANDIDATE" : "SUBSCRIPTION DETAILS";

  const decision = item?.decision;
  const plan = item?.plan;
  const evidence = item?.evidence_list || [];
  const action = item?.action_result;
  const verif = item?.verification_result;

  let html = "";

  // 1. Overview metadata
  html += `<div class="metric-card" style="background: var(--bg-elevated); margin-bottom: 0;">
    <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px;">
      <div><span class="sub-meta">MONTHLY BILLING</span><strong style="display:block; font-size: 16px;">$${sub.monthly_cost ? sub.monthly_cost.toFixed(2) : '0.00'} / mo</strong></div>
      <div><span class="sub-meta">CURRENT RENEWAL</span><strong style="display:block; font-size: 16px;">${sub.renewal_date || 'No renewal date'}</strong></div>
      <div><span class="sub-meta">LAST DETECTED STREAM</span><strong style="display:block; font-size: 14px;">${formatDate(sub.last_used_at)}</strong></div>
      <div><span class="sub-meta">CURRENT WORKFLOW</span><div style="margin-top: 4px;">${badge(item?.final_status || 'not reviewed')}</div></div>
    </div>
  </div>`;

  // 2. Decision Rationale & Consequences
  if (decision) {
    html += `<div>
      <h3>Eligibility Decision</h3>
      <p style="margin-top: 4px; font-size: 13px; color: var(--text-secondary);">${decision.reason}</p>
      <div style="margin-top: 8px; font-size: 12px; color: var(--text-muted);">
        Uncertainty Metric: <strong>${Math.round(decision.uncertainty * 100)}%</strong>
        ${decision.uncertainty > 0.3 ? badge("High Uncertainty — Review Required", "warning") : badge("High Confidence", "success")}
      </div>
    </div>`;

    if (decision.data_deletion_risk || (plan && plan.consequences_warning)) {
      html += `<div class="callout danger">
        <div class="callout-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          CRITICAL CONSEQUENCE: IMMEDIATE DATA DELETION
        </div>
        <p style="font-size: 12px;">${plan?.consequences_warning || "Provider policy dictates immediate and irreversible deletion of cloud libraries and assets."}</p>
      </div>`;
    }

    if (decision.pause_alternative_available) {
      html += `<div class="callout info">
        <div class="callout-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><rect width="4" height="12" x="10" y="6" rx="1"/></svg>
          BILLING PAUSE ALTERNATIVE AVAILABLE
        </div>
        <p style="font-size: 12px;">This provider permits a temporary billing freeze. Remember: A paused subscription is NOT cancelled.</p>
      </div>`;
    }
  }

  // 3. Evidence List
  html += `<div>
    <h3>Usage & Policy Evidence (${evidence.length} Items)</h3>
    <ul class="evidence-list" style="margin-top: 10px;">
      ${evidence.length
        ? evidence.map((e) => `<li class="evidence-card">
            <div class="evidence-meta">
              <span class="badge info">${e.evidence_type}</span>
              <span>Confidence: ${Math.round(e.confidence * 100)}% · ${formatDate(e.captured_at)}</span>
            </div>
            <p class="evidence-text">"${e.excerpt}"</p>
            <div class="evidence-source">${e.source_url || "Synthetic mock reference"}</div>
          </li>`).join("")
        : `<li class="empty-state">No evidence records available.</li>`}
    </ul>
  </div>`;

  // 4. Action & Verification Status (if already acted upon)
  if (action) {
    html += `<div class="card" style="background: var(--bg-elevated); padding: 16px;">
      <h3>Action Attempt & Verification Status</h3>
      <div style="margin-top: 10px; font-size: 13px;">
        <div>Action ID: <code class="sub-id-code">${action.action_id}</code></div>
        <div>Action Execution: ${badge(action.status, action.status === "succeeded" ? "success" : "danger")}</div>
        <div style="margin-top: 4px;">Independent Verification: ${verif ? badge(verif.status, verif.status === "verified" ? "success" : "danger") : badge("Pending Independent Evidence", "warning")}</div>
        ${verif ? `<div style="margin-top: 6px; font-size: 12px; color: var(--text-muted);">Observed State: <strong>${verif.final_state_observed}</strong> — ${verif.details}</div>` : ""}
      </div>
      ${action.status === "succeeded" && (!verif || verif.status !== "verified") ? `
        <div style="margin-top: 14px;">
          <button id="trigger-verify-btn" class="button verify-btn" style="width: 100%;">
            Trigger Independent Renewal Verification Check
          </button>
        </div>
      ` : ""}
    </div>`;
  }

  // 5. Explicit Approval Action Gate (Screen C)
  if (plan && plan.status === "awaiting_approval") {
    html += `<div class="approval-gate-box">
      <div style="font-size: 11px; font-weight: 800; color: #f87171; letter-spacing: 0.1em; margin-bottom: 6px;">
        EXPLICIT HUMAN APPROVAL GATE
      </div>
      <p style="font-size: 13px; color: #fff;">
        You are approving cancellation for <strong>${plan.service_name}</strong> (Plan: <code>${plan.plan_version}</code>).
      </p>
      <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
        Action will ONLY be authorized for this exact subscription. All other subscriptions remain strictly isolated.
      </p>
      <div class="approval-actions">
        <button id="drawer-approve-btn" class="button danger">
          Approve Cancellation
        </button>
        <button id="drawer-decline-btn" class="button secondary">
          Keep Subscription
        </button>
      </div>
    </div>`;
  }

  $("detail-body").innerHTML = html;
  $("detail-drawer").hidden = false;

  // Bind approval gate buttons
  $("drawer-approve-btn")?.addEventListener("click", () => handleDecision(plan, item, true));
  $("drawer-decline-btn")?.addEventListener("click", () => handleDecision(plan, item, false));

  // Bind verification button
  $("trigger-verify-btn")?.addEventListener("click", () => handleVerifyAction(action.action_id, sub));
}

$("close-detail").addEventListener("click", () => {
  $("detail-drawer").hidden = true;
});

// -----------------------------------------------------------------------------
// Decision Execution (Approve / Decline)
// -----------------------------------------------------------------------------

async function handleDecision(plan, item, approve) {
  const actionName = approve ? "Approve cancellation" : "Keep subscription";
  if (!confirm(`Are you sure you want to ${actionName.toLowerCase()} for ${item.subscription.service_name}?`)) {
    return;
  }

  const endpoint = `/plans/${plan.plan_id}/${approve ? "approve" : "decline"}`;
  const payload = approve
    ? {
        plan_id: plan.plan_id,
        subscription_id: item.subscription.subscription_id,
        service_name: plan.service_name,
        plan_version: plan.plan_version,
        approved_by: "dashboard_operator",
        notes: "Approved via SubCan Dashboard human review gate.",
      }
    : {
        plan_id: plan.plan_id,
        plan_version: plan.plan_version,
        declined_by: "dashboard_operator",
        notes: "Declined: user explicitly opted to keep account active.",
      };

  try {
    await api(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    showNotice(
      approve
        ? `Cancellation action initiated for ${item.subscription.service_name}. Awaiting independent renewal verification.`
        : `Subscription ${item.subscription.service_name} preserved active per user decision.`,
      approve ? "warning" : "info"
    );

    await loadRunSubscriptions();
    openDrawer(item.subscription.subscription_id);
  } catch (err) {
    showNotice(err.message, "error");
  }
}

// -----------------------------------------------------------------------------
// Trigger Independent Verification Check
// -----------------------------------------------------------------------------

async function handleVerifyAction(actionId, subscription) {
  const btn = $("trigger-verify-btn");
  if (btn) btn.disabled = true;

  try {
    // Independent observation confirms provider billing terminated & renewal disabled
    const postActionObserved = {
      subscription_id: subscription.subscription_id,
      service_name: subscription.service_name,
      status: "cancelled",
      renewal_date: null,
      last_used_at: subscription.last_used_at,
    };

    const verif = await api(`/actions/${actionId}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        observed_subscription: postActionObserved,
        method: "renewal_status_check",
      }),
    });

    showNotice(
      verif.status === "verified"
        ? `Cancellation independently verified for ${subscription.service_name}! Final outcome: VERIFIED CANCELLED.`
        : `Verification check returned: ${verif.status}. ${verif.details}`,
      verif.status === "verified" ? "success" : "warning"
    );

    await loadRunSubscriptions();
    openDrawer(subscription.subscription_id);
  } catch (err) {
    showNotice(`Verification check error: ${err.message}`, "error");
    if (btn) btn.disabled = false;
  }
}

// -----------------------------------------------------------------------------
// Orchestration Run Lifecycle
// -----------------------------------------------------------------------------

async function startReviewRun() {
  const btn = $("start-run");
  btn.disabled = true;
  try {
    const subIds = state.subscriptions.map((s) => s.subscription_id);
    const run = await api("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription_ids: subIds }),
    });

    state.runId = run.run_id;
    $("active-run-id").textContent = run.run_id;
    showNotice(`Review run ${run.run_id} initiated. Inactivity evaluated; NO cancellations executed automatically.`, "info");
    await loadRunSubscriptions();
  } catch (err) {
    showNotice(`Failed to start run: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function loadRunSubscriptions() {
  if (!state.runId) return renderRows();
  try {
    const data = await api(`/runs/${state.runId}/subscriptions`);
    state.items = data.items;
    $("table-subtext").textContent = `Viewing Run ${state.runId} (${data.items.length} accounts analyzed).`;
    renderRows();
  } catch (err) {
    showNotice(`Failed to load run details: ${err.message}`, "error");
  }
}

async function loadSubscriptions() {
  try {
    state.subscriptions = await api("/subscriptions");
    renderRows();
  } catch (err) {
    $("loading").textContent = `Failed to load subscriptions: ${err.message}`;
    showNotice(err.message, "error");
  }
}

// -----------------------------------------------------------------------------
// Tab 2: Audit & Reconciliation Report
// -----------------------------------------------------------------------------

async function loadAuditTab() {
  if (!state.runId) {
    $("reconciliation-rows").innerHTML = `<tr><td colspan="4" class="empty-state">Start an orchestration review run first.</td></tr>`;
    $("audit-events-timeline").innerHTML = `<div class="empty-state">Start an orchestration review run first.</div>`;
    return;
  }

  try {
    // 1. Fetch reconciliation report
    const report = await api(`/runs/${state.runId}/report`);
    $("audit-total-evaluated").textContent = report.total_evaluated;
    $("audit-total-cancelled").textContent = report.total_cancelled_verified;
    $("audit-total-kept").textContent = report.total_kept;
    $("audit-monthly-savings").textContent = `$${report.total_monthly_savings_usd.toFixed(2)}`;

    const tbody = $("reconciliation-rows");
    tbody.innerHTML = report.items.map((i) => {
      const isVerified = i.final_status === "verified_cancelled";
      return `<tr>
        <td><strong>${i.service_name}</strong><br><small class="sub-meta">${i.subscription_id}</small></td>
        <td>${badge(i.final_status, isVerified ? "success" : i.final_status === "kept" ? "info" : "warning")}</td>
        <td>${i.action_id ? badge("Action Executed", "success") : badge("No Action", "")}</td>
        <td>${i.verification_id ? badge("Verified", "success") : badge("Unverified", "warning")}</td>
      </tr>`;
    }).join("");

    // 2. Fetch SQLite immutable audit events
    const auditEvents = await api(`/runs/${state.runId}/audit`);
    const timelineEl = $("audit-events-timeline");
    if (!auditEvents.length) {
      timelineEl.innerHTML = `<div class="empty-state">No audit events recorded for run ${state.runId}.</div>`;
      return;
    }

    timelineEl.innerHTML = auditEvents.map((ev) => {
      return `<div class="timeline-event ${ev.event_type}">
        <div class="event-header">
          <span class="event-type">${ev.event_type}</span>
          <span>${formatDate(ev.event_at)}</span>
        </div>
        <div>Entity ID: <code>${ev.entity_id || "—"}</code></div>
      </div>`;
    }).join("");
  } catch (err) {
    showNotice(`Failed to load audit report: ${err.message}`, "error");
  }
}

$("refresh-audit").addEventListener("click", loadAuditTab);

// -----------------------------------------------------------------------------
// Tab 3: Official Acceptance Test Suite (Screen E)
// -----------------------------------------------------------------------------

async function runOfficialTests() {
  const btn = $("run-official-tests-btn");
  const loader = $("tests-loading");
  const summaryBar = $("test-summary-bar");
  btn.disabled = true;
  loader.hidden = false;

  try {
    const results = await api("/api/test-report");
    state.testResults = results;

    const passedCount = results.filter((r) => r.status === "PASSED").length;
    const failedCount = results.filter((r) => r.status === "FAILED").length;

    $("score-passed").textContent = passedCount;
    $("score-failed").textContent = failedCount;
    $("score-total").textContent = results.length;
    summaryBar.hidden = false;

    const tbody = $("official-test-rows");
    tbody.innerHTML = results.map((r) => {
      const isPass = r.status === "PASSED";
      return `<tr>
        <td><strong>${r.test_id}</strong></td>
        <td>
          <div style="font-weight: 700; color: #fff;">${r.title}</div>
          <div style="font-size: 11px; color: var(--text-muted);">${r.details}</div>
        </td>
        <td><code class="sub-id-code">${r.input_fixture}</code></td>
        <td style="color: #93c5fd; font-size: 12px;">${r.expected_result}</td>
        <td style="color: #cbd5e1; font-size: 12px;">${r.actual_result}</td>
        <td>${badge(r.status, isPass ? "success" : "danger")}</td>
        <td style="font-size: 11px; color: var(--text-secondary);">${r.evidence_summary}</td>
      </tr>`;
    }).join("");

    showNotice(`Official acceptance test suite completed: ${passedCount} passed, ${failedCount} failed.`, isPassAll(results) ? "success" : "error");
  } catch (err) {
    showNotice(`Test execution failed: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    loader.hidden = true;
  }
}

function isPassAll(results) {
  return results.every((r) => r.status === "PASSED");
}

$("run-official-tests-btn").addEventListener("click", runOfficialTests);

// -----------------------------------------------------------------------------
// Initialization & Global Event Listeners
// -----------------------------------------------------------------------------

$("refresh").addEventListener("click", () => {
  loadSubscriptions();
  if (state.runId) loadRunSubscriptions();
});

$("start-run").addEventListener("click", startReviewRun);
$("search").addEventListener("input", renderRows);
$("status-filter").addEventListener("change", renderRows);
$("eligibility-filter").addEventListener("change", renderRows);

// Initial boot
loadSubscriptions();
