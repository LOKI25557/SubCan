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
  gmailStatus: null,
  candidates: [],
  analytics: null,
};

const $ = (id) => document.getElementById(id);

// -----------------------------------------------------------------------------
// Utilities & UI Helpers
// -----------------------------------------------------------------------------

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function sanitizeUrl(url) {
  if (!url || typeof url !== "string") return null;
  const trimmed = url.trim();
  if (/^https?:\/\//i.test(trimmed)) {
    return escapeHtml(trimmed);
  }
  return null;
}

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
  const clean = escapeHtml(String(text || "—").replaceAll("_", " "));
  const safeVariant = escapeHtml(variant);
  return `<span class="badge ${safeVariant}">${clean}</span>`;
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
    } else if (targetId === "tab-candidates") {
      loadCandidates();
    } else if (targetId === "tab-analytics") {
      loadSpendingAnalytics();
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

      const costStr = typeof sub.monthly_cost === "number" ? `$${sub.monthly_cost.toFixed(2)}/mo` : "—";
      const lastUsed = formatDate(sub.last_used_at);
      const renewal = sub.renewal_date || "—";
      const safeServiceName = escapeHtml(sub.service_name || "Unknown Service");
      const safeSubId = escapeHtml(sub.subscription_id || "");
      const safeLastUsed = escapeHtml(lastUsed);
      const safeRenewal = escapeHtml(renewal);
      const safeCost = escapeHtml(costStr);

      return `<tr>
        <td>
          <strong>${safeServiceName}</strong>
          <div class="sub-meta">${safeSubId}</div>
        </td>
        <td>${statusBadge}</td>
        <td>${safeLastUsed}</td>
        <td>${safeRenewal}</td>
        <td><strong>${safeCost}</strong></td>
        <td>${eligBadge}</td>
        <td>${workflowBadge}</td>
        <td>
          <button class="button secondary small review-trigger-btn" data-id="${safeSubId}">
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

function closeDrawer() {
  const drawer = $("detail-drawer");
  if (!drawer) return;
  drawer.hidden = true;
  drawer.style.display = "none";
  drawer.setAttribute("aria-hidden", "true");
  drawer.classList.remove("open");
  document.body.classList.remove("drawer-open");
}

function openDrawer(target) {
  if (!target) return;
  const targetId = typeof target === "object" ? (target.subscription_id || target.id) : target;
  if (!targetId || typeof targetId !== "string") return;

  const item = state.items.find((i) => i.subscription?.subscription_id === targetId || i.subscription?.id === targetId);
  const sub = item?.subscription || state.subscriptions.find((s) => s.subscription_id === targetId || s.id === targetId);
  if (!sub) {
    console.warn(`[SubCan] openDrawer: subscription '${targetId}' not found.`);
    showNotice(`Subscription '${targetId}' could not be found.`, "error");
    closeDrawer();
    return;
  }

  // Populate title, subtitle, and eyebrow using verified object properties (safe textContent)
  $("detail-title").textContent = sub.service_name || "Service Details";
  $("detail-subtitle").textContent = sub.subscription_id || targetId;

  let eyebrow = "SUBSCRIPTION DETAILS";
  if (item?.decision?.eligibility === "candidate") {
    eyebrow = "ELIGIBLE CANCELLATION CANDIDATE";
  } else if (item?.decision?.eligibility === "needs_review") {
    eyebrow = "MANUAL REVIEW REQUIRED";
  } else if (item?.decision?.eligibility === "not_eligible") {
    eyebrow = "INELIGIBLE ACCOUNT";
  }
  $("detail-eyebrow").textContent = eyebrow;

  const decision = item?.decision;
  const plan = item?.plan;
  const evidence = item?.evidence_list || [];
  const action = item?.action_result;
  const verif = item?.verification_result;

  let html = "";

  // 1. Overview metadata
  const currencySymbol = sub.currency === "USD" ? "$" : (escapeHtml(sub.currency) || "$");
  const cost = typeof sub.monthly_cost === "number" ? `${currencySymbol}${sub.monthly_cost.toFixed(2)}` : "—";
  const billingCycle = escapeHtml(sub.billing_cycle || "monthly");
  const renewal = escapeHtml(sub.renewal_date || "No renewal date");
  const lastUsed = escapeHtml(formatDate(sub.last_used_at));
  const workflowState = item?.final_status || "unreviewed";
  const safeTermsUrl = sanitizeUrl(sub.cancellation_terms_url);

  html += `<div class="metric-card" style="background: var(--bg-elevated); margin-bottom: 0;">
    <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px;">
      <div><span class="sub-meta">MONTHLY BILLING</span><strong style="display:block; font-size: 16px;">${cost}</strong></div>
      <div><span class="sub-meta">BILLING CYCLE</span><strong style="display:block; font-size: 16px; text-transform: capitalize;">${billingCycle}</strong></div>
      <div><span class="sub-meta">CURRENT RENEWAL</span><strong style="display:block; font-size: 14px;">${renewal}</strong></div>
      <div><span class="sub-meta">LAST DETECTED ACTIVITY</span><strong style="display:block; font-size: 14px;">${lastUsed}</strong></div>
      <div><span class="sub-meta">CURRENT WORKFLOW</span><div style="margin-top: 4px;">${badge(workflowState)}</div></div>
      <div>
        <span class="sub-meta">CANCELLATION POLICY</span>
        <div style="margin-top: 4px; font-size: 13px;">
          ${safeTermsUrl ? `<a href="${safeTermsUrl}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa; text-decoration: underline;">View Terms ↗</a>` : `<span style="color: var(--text-muted);">None specified</span>`}
        </div>
      </div>
    </div>
  </div>`;

  // 2. Pre-evaluation info or Decision Rationale & Consequences
  if (decision) {
    html += `<div>
      <h3>Eligibility Decision</h3>
      <p style="margin-top: 4px; font-size: 13px; color: var(--text-secondary);">${escapeHtml(decision.reason || "Evaluated")}</p>
      <div style="margin-top: 8px; font-size: 12px; color: var(--text-muted);">
        Uncertainty Metric: <strong>${Math.round((decision.uncertainty || 0) * 100)}%</strong>
        ${(decision.uncertainty || 0) > 0.3 ? badge("High Uncertainty — Review Required", "warning") : badge("High Confidence", "success")}
      </div>
    </div>`;

    if (decision.data_deletion_risk || sub.data_deletion_warning || (plan && plan.consequences_warning)) {
      const warningText = escapeHtml(plan?.consequences_warning || "Provider policy dictates immediate and irreversible deletion of cloud libraries, playlists, and user assets upon cancellation.");
      html += `<div class="callout danger">
        <div class="callout-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          CRITICAL CONSEQUENCE: IMMEDIATE DATA DELETION
        </div>
        <p style="font-size: 12px;">${warningText}</p>
      </div>`;
    }

    if (decision.pause_alternative_available || sub.pause_option_available) {
      html += `<div class="callout info">
        <div class="callout-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><rect width="4" height="12" x="10" y="6" rx="1"/></svg>
          BILLING PAUSE ALTERNATIVE AVAILABLE
        </div>
        <p style="font-size: 12px;">This provider permits a temporary billing freeze. Remember: A paused subscription is NOT cancelled.</p>
      </div>`;
    }
  } else {
    // If opened before run evaluation
    html += `<div class="callout info">
      <div class="callout-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
        PRE-RUN INGESTION RECORD
      </div>
      <p style="font-size: 12px;">Ingested from provider connectors. Start an orchestration review run to evaluate inactivity rules and generate cancellation proposals.</p>
    </div>`;

    if (sub.data_deletion_warning) {
      html += `<div class="callout danger">
        <div class="callout-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          KNOWN CONSEQUENCE: IMMEDIATE DATA DELETION
        </div>
        <p style="font-size: 12px;">Connector metadata indicates cancellation triggers permanent data loss.</p>
      </div>`;
    }

    if (sub.pause_option_available) {
      html += `<div class="callout info">
        <div class="callout-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><rect width="4" height="12" x="10" y="6" rx="1"/></svg>
          BILLING PAUSE OPTION SUPPORTED
        </div>
        <p style="font-size: 12px;">Connector reports this service allows billing pause without account termination.</p>
      </div>`;
    }
  }

  // 3. Evidence List
  html += `<div>
    <h3>Usage & Policy Evidence (${evidence.length} Items)</h3>
    <ul class="evidence-list" style="margin-top: 10px;">
      ${evidence.length
        ? evidence.map((e) => {
            const safeType = escapeHtml(e.evidence_type || "evidence");
            const safeExcerpt = escapeHtml(e.excerpt || "No excerpt recorded");
            const safeSource = sanitizeUrl(e.source_url)
              ? `<a href="${sanitizeUrl(e.source_url)}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa;">${escapeHtml(e.source_url)}</a>`
              : escapeHtml(e.source_url || "Synthetic mock reference");
            return `<li class="evidence-card">
              <div class="evidence-meta">
                <span class="badge info">${safeType}</span>
                <span>Confidence: ${Math.round((e.confidence || 0) * 100)}% · ${escapeHtml(formatDate(e.captured_at))}</span>
              </div>
              <p class="evidence-text">"${safeExcerpt}"</p>
              <div class="evidence-source">${safeSource}</div>
            </li>`;
          }).join("")
        : `<li class="empty-state">${decision ? "No evidence records captured." : "Evidence collection executes during review runs."}</li>`}
    </ul>
  </div>`;

  // 4. Action & Verification Status (if already acted upon)
  if (action) {
    const safeActionId = escapeHtml(action.action_id || "");
    const safeObserved = escapeHtml(verif?.final_state_observed || "unknown");
    const safeDetails = escapeHtml(verif?.details || "");
    html += `<div class="card" style="background: var(--bg-elevated); padding: 16px;">
      <h3>Action Attempt & Verification Status</h3>
      <div style="margin-top: 10px; font-size: 13px;">
        <div>Action ID: <code class="sub-id-code">${safeActionId}</code></div>
        <div style="margin-top: 4px;">Action Execution: ${badge(action.status, action.status === "succeeded" ? "success" : "danger")}</div>
        <div style="margin-top: 4px;">Independent Verification: ${verif ? badge(verif.status, verif.status === "verified" ? "success" : "danger") : badge("Pending Independent Evidence", "warning")}</div>
        ${verif ? `<div style="margin-top: 6px; font-size: 12px; color: var(--text-muted);">Observed State: <strong>${safeObserved}</strong> — ${safeDetails}</div>` : ""}
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
    const safePlanService = escapeHtml(plan.service_name || sub.service_name || "");
    const safePlanVersion = escapeHtml(plan.plan_version || "");
    html += `<div class="approval-gate-box">
      <div style="font-size: 11px; font-weight: 800; color: #f87171; letter-spacing: 0.1em; margin-bottom: 6px;">
        EXPLICIT HUMAN APPROVAL GATE
      </div>
      <p style="font-size: 13px; color: #fff;">
        You are approving cancellation for <strong>${safePlanService}</strong> (Plan: <code>${safePlanVersion}</code>).
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

  // Make drawer visible, remove aria-hidden, add open class
  const drawer = $("detail-drawer");
  drawer.hidden = false;
  drawer.style.display = "flex";
  drawer.removeAttribute("aria-hidden");
  drawer.classList.add("open");
  document.body.classList.add("drawer-open");

  // Bind approval gate buttons
  $("drawer-approve-btn")?.addEventListener("click", () => handleDecision(plan, item, true));
  $("drawer-decline-btn")?.addEventListener("click", () => handleDecision(plan, item, false));

  // Bind verification button
  $("trigger-verify-btn")?.addEventListener("click", () => handleVerifyAction(action.action_id, sub));
}

// Drawer dismissal event listeners
$("close-detail")?.addEventListener("click", closeDrawer);

$("detail-drawer")?.addEventListener("click", (e) => {
  if (e.target === $("detail-drawer")) {
    closeDrawer();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("detail-drawer") && !$("detail-drawer").hidden) {
    closeDrawer();
  }
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
    closeDrawer();
  }
}

async function loadSubscriptions() {
  try {
    state.subscriptions = await api("/subscriptions");
    renderRows();
  } catch (err) {
    $("loading").textContent = `Failed to load subscriptions: ${err.message}`;
    showNotice(`Failed to load subscriptions: ${err.message}`, "error");
    closeDrawer();
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

// =============================================================================
// Stage D: Gmail OAuth & Billing Email Discovery
// =============================================================================

async function initGmail() {
  const urlParams = new URLSearchParams(window.location.search);
  const connected = urlParams.get("gmail_connected");
  const error = urlParams.get("gmail_error");

  if (connected) {
    showNotice("Gmail successfully connected! You can now synchronize billing emails.", "success");
    window.history.replaceState({}, document.title, window.location.pathname);
  } else if (error) {
    showNotice(`Gmail authorization error: ${error}`, "error");
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  await loadGmailStatus();
}

async function loadGmailStatus() {
  try {
    const data = await api("/api/email/status");
    state.gmailStatus = data;

    const statusBadge = $("gmail-status-badge");
    const dataSrcBadge = $("data-source-badge");
    const emailPill = $("gmail-user-email");
    const desc = $("gmail-status-desc");
    const hint = $("gmail-setup-hint");
    const btnConnect = $("btn-connect-gmail");
    const btnSync = $("btn-sync-gmail");
    const btnDisconnect = $("btn-disconnect-gmail");
    const navBadge = $("nav-candidates-badge");

    if (data.connected) {
      statusBadge.textContent = "Connected";
      statusBadge.className = "badge success";
      dataSrcBadge.textContent = "Live Gmail";
      dataSrcBadge.className = "badge success";

      if (data.email) {
        emailPill.textContent = data.email;
        emailPill.hidden = false;
      } else {
        emailPill.hidden = true;
      }

      desc.textContent = "Live Gmail integration active. Read-only billing discovery enabled.";
      hint.hidden = true;
      btnConnect.hidden = true;
      btnSync.hidden = false;
      btnDisconnect.hidden = false;
    } else {
      emailPill.hidden = true;
      btnSync.hidden = true;
      btnDisconnect.hidden = true;
      dataSrcBadge.textContent = "Demo Mode";
      dataSrcBadge.className = "badge";

      const isConfigured = data.credential_status?.oauth_ready;
      if (isConfigured) {
        statusBadge.textContent = "Not Connected";
        statusBadge.className = "badge";
        desc.textContent = "Connect Gmail to discover recurring billing emails and invoices automatically.";
        hint.hidden = true;
        btnConnect.hidden = false;
      } else {
        statusBadge.textContent = "OAuth Unconfigured";
        statusBadge.className = "badge warning";
        desc.textContent = "Google OAuth credentials are not configured in your environment.";
        hint.hidden = false;
        btnConnect.hidden = true;
      }
    }

    if (typeof data.candidates_pending_count === "number" && data.candidates_pending_count > 0) {
      navBadge.textContent = data.candidates_pending_count;
      navBadge.hidden = false;
    } else {
      navBadge.hidden = true;
    }
  } catch (err) {
    $("gmail-status-badge").textContent = "Status Error";
    $("gmail-status-badge").className = "badge danger";
    $("gmail-status-desc").textContent = `Could not verify Gmail connection status: ${err.message}`;
  }
}

function connectGmail() {
  window.location.href = "/api/email/oauth/start";
}

async function disconnectGmail() {
  if (!confirm("Are you sure you want to disconnect Gmail and revoke local tokens?")) return;
  try {
    const res = await api("/api/email/disconnect", { method: "POST" });
    showNotice(res.message || "Gmail disconnected.", "info");
    await loadGmailStatus();
    await loadCandidates();
    await loadSpendingAnalytics();
  } catch (err) {
    showNotice(`Failed to disconnect Gmail: ${err.message}`, "error");
  }
}

async function syncGmail() {
  const btn = $("btn-sync-gmail");
  const spinner = $("sync-spinner");
  btn.disabled = true;
  if (spinner) spinner.classList.add("spinning");

  try {
    const res = await api("/api/email/sync", { method: "POST" });
    const count = res.saved_candidates ?? res.candidates?.length ?? 0;
    $("gmail-last-sync").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    showNotice(`Gmail sync complete. ${count} billing candidates extracted and stored.`, "success");
    await loadCandidates();
    await loadSpendingAnalytics();
    await loadGmailStatus();
  } catch (err) {
    showNotice(`Gmail sync failed: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    if (spinner) spinner.classList.remove("spinning");
  }
}

// =============================================================================
// Stage D: Discovered Candidates Management
// =============================================================================

async function loadCandidates() {
  const loader = $("candidates-loading");
  if (loader) loader.hidden = false;

  try {
    const data = await api("/api/email/candidates");
    state.candidates = data.candidates || [];
    renderCandidateRows();
  } catch (err) {
    showNotice(`Failed to load candidates: ${err.message}`, "error");
  } finally {
    if (loader) loader.hidden = true;
  }
}

function renderCandidateRows() {
  const query = ($("candidate-search")?.value || "").toLowerCase().trim();
  const statusFilter = $("candidate-status-filter")?.value || "";
  const tbody = $("candidate-rows");
  if (!tbody) return;

  const total = state.candidates.length;
  const pending = state.candidates.filter((c) => !c.confirmed).length;
  const confirmed = total - pending;

  $("cand-stat-total").textContent = total;
  $("cand-stat-pending").textContent = pending;
  $("cand-stat-confirmed").textContent = confirmed;

  const navBadge = $("nav-candidates-badge");
  if (navBadge) {
    navBadge.textContent = pending;
    navBadge.hidden = pending === 0;
  }

  const filtered = state.candidates.filter((c) => {
    const matchesQuery =
      !query ||
      `${c.provider_name || ""} ${c.sender || ""} ${c.subject || ""}`.toLowerCase().includes(query);
    const matchesStatus =
      !statusFilter ||
      (statusFilter === "pending" && !c.confirmed) ||
      (statusFilter === "confirmed" && c.confirmed);
    return matchesQuery && matchesStatus;
  });

  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">${
      state.candidates.length
        ? "No candidates match your search or filter."
        : "No Gmail billing candidates discovered yet. Connect Gmail and run 'Sync Billing Emails'."
    }</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered
    .map((c) => {
      const isConfirmed = Boolean(c.confirmed);
      const safeProvider = escapeHtml(c.provider_name || "Unidentified Service");
      const safeSender = escapeHtml(c.sender || "Unknown Sender");
      const safeSubject = escapeHtml(c.subject || "No subject");
      const safeMsgId = escapeHtml(c.message_id ? c.message_id.slice(0, 16) + "…" : "—");
      const currencySymbol = c.currency === "USD" ? "$" : (escapeHtml(c.currency) || "$");
      const amountStr = typeof c.amount === "number" ? `${currencySymbol}${c.amount.toFixed(2)}` : `<span style="color: var(--text-muted);">Unknown</span>`;
      const billingDateStr = escapeHtml(c.billing_date || c.date_header || "Unknown");
      const discoveryTimeStr = escapeHtml(formatDate(c.extracted_at || c.created_at));

      const statusBadgeHtml = isConfirmed
        ? `<span class="badge success">Confirmed</span>`
        : `<span class="badge warning">Awaiting Review</span>`;

      const actionBtnHtml = isConfirmed
        ? `<button class="button secondary small" disabled>Confirmed</button>`
        : `<button class="button primary small" data-confirm-cand="${escapeHtml(c.message_id)}">Confirm Subscription</button>`;

      return `<tr>
        <td>
          <strong>${safeProvider}</strong><br>
          <small class="sub-meta">${safeSender}</small>
        </td>
        <td><strong>${amountStr}</strong></td>
        <td>${billingDateStr}</td>
        <td>
          <span title="${safeSubject}">${safeSubject.length > 35 ? safeSubject.slice(0, 35) + "…" : safeSubject}</span><br>
          <code class="sub-id-code">${safeMsgId}</code>
        </td>
        <td><small class="sub-meta">${discoveryTimeStr}</small></td>
        <td>${statusBadgeHtml}</td>
        <td>
          <div class="row-actions">
            <button class="button secondary small" data-view-cand="${escapeHtml(c.message_id)}">Details</button>
            ${actionBtnHtml}
          </div>
        </td>
      </tr>`;
    })
    .join("");

  tbody.querySelectorAll("[data-confirm-cand]").forEach((btn) => {
    btn.addEventListener("click", () => confirmCandidate(btn.dataset.confirmCand, btn));
  });

  tbody.querySelectorAll("[data-view-cand]").forEach((btn) => {
    btn.addEventListener("click", () => openCandidateDrawer(btn.dataset.viewCand));
  });
}

async function confirmCandidate(messageId, buttonEl) {
  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = "Confirming…";
  }

  try {
    await api(`/api/email/candidates/${messageId}/confirm`, {
      method: "POST",
    });

    showNotice("Candidate successfully confirmed and added to subscription inventory!", "success");
    await loadCandidates();
    await loadSubscriptions();
    await loadSpendingAnalytics();
    closeDrawer();
  } catch (err) {
    showNotice(`Failed to confirm candidate: ${err.message}`, "error");
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.textContent = "Confirm Subscription";
    }
  }
}

function openCandidateDrawer(messageId) {
  const candidate = state.candidates.find((c) => c.message_id === messageId);
  if (!candidate) return;

  const safeProvider = escapeHtml(candidate.provider_name || "Unidentified Service");
  const isConfirmed = Boolean(candidate.confirmed);

  $("detail-title").textContent = safeProvider;
  $("detail-subtitle").textContent = candidate.message_id || "Candidate Record";
  $("detail-eyebrow").textContent = "GMAIL BILLING CANDIDATE";

  const currencySymbol = candidate.currency === "USD" ? "$" : (escapeHtml(candidate.currency) || "$");
  const cost = typeof candidate.amount === "number" ? `${currencySymbol}${candidate.amount.toFixed(2)}` : "Unknown";
  const billingDate = escapeHtml(candidate.billing_date || candidate.date_header || "Unknown");
  const discoveryDate = escapeHtml(formatDate(candidate.extracted_at || candidate.created_at));
  const safeSender = escapeHtml(candidate.sender || "Unknown");
  const safeSubject = escapeHtml(candidate.subject || "No subject");
  const safePreview = escapeHtml(candidate.body_preview || "No body preview available.");

  let html = `
    <div class="safety-alert-card" style="margin-bottom: 16px;">
      <div class="alert-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>
      </div>
      <div>
        <strong>Verification Required:</strong>
        <p>This candidate was parsed from an email receipt. A billing email is NOT proof of active usage and does NOT authorize cancellation. Confirming will add it to your subscriptions inventory for review.</p>
      </div>
    </div>

    <div class="metric-card" style="background: var(--bg-elevated); margin-bottom: 16px;">
      <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px;">
        <div><span class="sub-meta">EXTRACTED AMOUNT</span><strong style="display:block; font-size: 16px;">${cost}</strong></div>
        <div><span class="sub-meta">CURRENCY</span><strong style="display:block; font-size: 16px;">${escapeHtml(candidate.currency || "USD")}</strong></div>
        <div><span class="sub-meta">BILLING DATE</span><strong style="display:block; font-size: 14px;">${billingDate}</strong></div>
        <div><span class="sub-meta">DISCOVERED AT</span><strong style="display:block; font-size: 14px;">${discoveryDate}</strong></div>
        <div><span class="sub-meta">CANDIDATE STATUS</span><div style="margin-top: 4px;">${isConfirmed ? badge("Confirmed", "success") : badge("Awaiting Review", "warning")}</div></div>
        <div><span class="sub-meta">PROVENANCE</span><div style="margin-top: 4px;"><span class="badge info">Gmail Read-Only</span></div></div>
      </div>
    </div>

    <div class="card" style="background: var(--bg-elevated); padding: 16px; margin-bottom: 16px;">
      <h3>Email Provenance Metadata</h3>
      <div style="margin-top: 10px; font-size: 13px;">
        <div><strong>Sender:</strong> <code>${safeSender}</code></div>
        <div style="margin-top: 6px;"><strong>Subject:</strong> ${safeSubject}</div>
        <div style="margin-top: 6px;"><strong>Message ID:</strong> <code class="sub-id-code">${escapeHtml(candidate.message_id)}</code></div>
      </div>
    </div>

    <div class="card" style="background: var(--bg-elevated); padding: 16px; margin-bottom: 20px;">
      <h3>Message Preview (Safe Snippet)</h3>
      <p style="margin-top: 8px; font-size: 12px; color: var(--text-secondary); background: rgba(0,0,0,0.3); padding: 10px; border-radius: var(--radius-sm); font-family: var(--font-mono); white-space: pre-wrap;">${safePreview}</p>
    </div>

    <div class="drawer-actions">
      ${isConfirmed
        ? `<button class="button secondary" style="width: 100%;" disabled>Already Confirmed into Inventory</button>`
        : `<button id="drawer-confirm-cand-btn" class="button primary glow" style="width: 100%;">Confirm as Subscription Record</button>`
      }
    </div>
  `;

  $("detail-body").innerHTML = html;

  const drawer = $("detail-drawer");
  drawer.hidden = false;
  drawer.style.display = "flex";
  drawer.removeAttribute("aria-hidden");
  drawer.classList.add("open");
  document.body.classList.add("drawer-open");

  $("drawer-confirm-cand-btn")?.addEventListener("click", (e) => {
    confirmCandidate(candidate.message_id, e.target);
  });
}

// =============================================================================
// Stage E: Real Spending Analytics
// =============================================================================

async function loadSpendingAnalytics() {
  try {
    const data = await api("/api/spending/analytics");
    state.analytics = data;

    $("analytics-active-count").textContent = data.active_subscriptions_count ?? 0;
    $("analytics-monthly-total").textContent = data.monthly_recurring_display ?? "$0.00";
    $("analytics-annual-total").textContent = data.annual_recurring_display ?? "$0.00";
    $("analytics-upcoming-count").textContent = data.upcoming_renewals?.length ?? 0;
    $("analytics-candidates-pending").textContent = data.candidates_awaiting_review_count ?? 0;
    $("analytics-unknown-count").textContent = (data.unknown_cost_count || 0) + (data.unknown_status_count || 0);

    const tbody = $("analytics-breakdown-rows");
    if (tbody) {
      const activeSubs = data.active_subscriptions || [];
      if (!activeSubs.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No active subscriptions found.</td></tr>`;
      } else {
        tbody.innerHTML = activeSubs.map((sub) => {
          const currSym = sub.currency === "USD" ? "$" : (escapeHtml(sub.currency) || "$");
          const monthlyStr = typeof sub.monthly_equivalent === "number" ? `${currSym}${sub.monthly_equivalent.toFixed(2)}` : (typeof sub.monthly_cost === "number" ? `${currSym}${sub.monthly_cost.toFixed(2)}` : "—");
          const annualStr = typeof sub.annual_equivalent === "number" ? `${currSym}${sub.annual_equivalent.toFixed(2)}` : "—";
          const cycleStr = escapeHtml(sub.billing_cycle || "monthly");
          const renewalStr = escapeHtml(sub.renewal_date || "—");

          return `<tr>
            <td><strong>${escapeHtml(sub.service_name)}</strong><br><small class="sub-meta">${escapeHtml(sub.subscription_id)}</small></td>
            <td><span style="text-transform: capitalize;">${cycleStr}</span></td>
            <td><strong>${monthlyStr}</strong></td>
            <td>${annualStr}</td>
            <td>${renewalStr}</td>
          </tr>`;
        }).join("");
      }
    }

    const renewalsEl = $("analytics-renewals-list");
    if (renewalsEl) {
      const renewals = data.upcoming_renewals || [];
      if (!renewals.length) {
        renewalsEl.innerHTML = `<div class="empty-state">No upcoming renewal dates scheduled.</div>`;
      } else {
        renewalsEl.innerHTML = renewals.map((r) => {
          const currSym = r.currency === "USD" ? "$" : (escapeHtml(r.currency) || "$");
          const amt = typeof r.amount === "number" ? `${currSym}${r.amount.toFixed(2)}` : "";
          return `<div class="renewal-item">
            <span class="renewal-service">${escapeHtml(r.service_name)}</span>
            <span class="renewal-meta">${escapeHtml(r.renewal_date)} ${amt ? `· ${amt}` : ""}</span>
          </div>`;
        }).join("");
      }
    }

    const uncertEl = $("analytics-uncertainties-list");
    if (uncertEl) {
      const uncerts = data.uncertainties || [];
      if (!uncerts.length) {
        uncertEl.innerHTML = `<p class="sub-meta">No data ambiguities detected. All amounts and renewals validated.</p>`;
      } else {
        uncertEl.innerHTML = uncerts.map((u) => {
          return `<div class="uncertainty-item">
            <span><strong>${escapeHtml(u.service_name)}:</strong> ${escapeHtml(u.issue)}</span>
            <span class="badge warning">Needs Review</span>
          </div>`;
        }).join("");
      }
    }
  } catch (err) {
    showNotice(`Failed to load spending analytics: ${err.message}`, "error");
  }
}

// -----------------------------------------------------------------------------
// Initialization & Global Event Listeners
// -----------------------------------------------------------------------------

$("refresh")?.addEventListener("click", () => {
  loadSubscriptions();
  loadGmailStatus();
  loadSpendingAnalytics();
  if (state.runId) loadRunSubscriptions();
});

$("start-run")?.addEventListener("click", startReviewRun);
$("search")?.addEventListener("input", renderRows);
$("status-filter")?.addEventListener("change", renderRows);
$("eligibility-filter")?.addEventListener("change", renderRows);

// Gmail & Candidate Listeners
$("btn-connect-gmail")?.addEventListener("click", connectGmail);
$("btn-disconnect-gmail")?.addEventListener("click", disconnectGmail);
$("btn-sync-gmail")?.addEventListener("click", syncGmail);
$("btn-refresh-candidates")?.addEventListener("click", loadCandidates);
$("candidate-search")?.addEventListener("input", renderCandidateRows);
$("candidate-status-filter")?.addEventListener("change", renderCandidateRows);
$("btn-refresh-analytics")?.addEventListener("click", loadSpendingAnalytics);

// Initial deterministic boot: close drawer and load subscriptions, Gmail, and analytics
closeDrawer();
loadSubscriptions();
initGmail();
loadSpendingAnalytics();
