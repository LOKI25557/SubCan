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
  emailAccount: null,
  candidates: [],
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
    } else if (targetId === "tab-email") {
      loadEmailTab();
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

      // Provenance badge
      let sourceBadge = "";
      const source = sub.discovery_source || (sub.subscription_id?.startsWith("sub_man_") ? "manual" : sub.subscription_id?.startsWith("sub_email_") ? "email" : "connector");
      if (source === "email") {
        sourceBadge = `<span class="badge" style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); font-size: 10px; margin-left: 6px;">Gmail</span>`;
      } else if (source === "manual") {
        sourceBadge = `<span class="badge" style="background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.3); font-size: 10px; margin-left: 6px;">Manual</span>`;
      } else {
        sourceBadge = `<span class="badge" style="background: rgba(148, 163, 184, 0.15); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.3); font-size: 10px; margin-left: 6px;">Demo Mock</span>`;
      }

      return `<tr>
        <td>
          <div style="display: flex; align-items: center; gap: 4px; flex-wrap: wrap;">
            <strong>${safeServiceName}</strong>
            ${sourceBadge}
          </div>
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

  // 6. Manual Cancellation Guidance (for restricted providers or services with manual instructions)
  if (sub.manual_action_url || sub.manual_action_instructions) {
    const safePortalUrl = sanitizeUrl(sub.manual_action_url);
    const instructions = escapeHtml(sub.manual_action_instructions || "Log into your account portal to manage or cancel this subscription directly without automated bypass.");
    html += `<div class="callout warning">
      <div class="callout-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
        MANUAL CANCELLATION GUIDANCE (RESTRICTED PROVIDER)
      </div>
      <p style="font-size: 12px; margin-top: 4px;">${instructions}</p>
      ${safePortalUrl ? `<div style="margin-top: 8px;"><a href="${safePortalUrl}" target="_blank" rel="noopener noreferrer" class="button secondary small" style="display: inline-flex; align-items: center; gap: 6px;">Open Provider Portal ↗</a></div>` : ""}
    </div>`;
  }

  // 7. Edit & Archive Actions
  html += `<div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border-subtle); display: flex; justify-content: flex-end; gap: 8px;">
    <button id="drawer-edit-btn" class="button secondary small">
      Edit Details
    </button>`;
  if (sub.subscription_id && (sub.subscription_id.startsWith("sub_man_") || sub.discovery_source === "manual" || sub.discovery_source === "email")) {
    html += `
    <button id="drawer-archive-btn" class="button secondary small" style="color: #f87171; border-color: rgba(239, 68, 68, 0.4);">
      Archive Subscription
    </button>`;
  }
  html += `</div>`;

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

  // Bind edit button
  $("drawer-edit-btn")?.addEventListener("click", () => openEditSubModal(sub));

  // Bind archive button
  $("drawer-archive-btn")?.addEventListener("click", async () => {
    if (!confirm(`Are you sure you want to archive '${sub.service_name}'? It will be safely removed from active monitoring.`)) return;
    try {
      await api(`/subscriptions/${sub.subscription_id}`, { method: "DELETE" });
      closeDrawer();
      showNotice(`Subscription '${sub.service_name}' archived successfully.`, "info");
      await loadSubscriptions();
    } catch (err) {
      showNotice(`Failed to archive: ${err.message}`, "error");
    }
  });
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

// -----------------------------------------------------------------------------
// Manual Subscription Entry Modal
// -----------------------------------------------------------------------------

function openAddSubModal() {
  const modal = $("add-sub-modal");
  if (!modal) return;
  modal.hidden = false;
  modal.classList.add("active");
  $("manual-service-name")?.focus();
}

function closeAddSubModal() {
  const modal = $("add-sub-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.classList.remove("active");
  $("add-sub-form")?.reset();
}

$("open-add-sub-modal")?.addEventListener("click", openAddSubModal);
$("close-add-sub-modal")?.addEventListener("click", closeAddSubModal);
$("cancel-add-sub")?.addEventListener("click", closeAddSubModal);

$("add-sub-modal")?.addEventListener("click", (e) => {
  if (e.target === $("add-sub-modal")) {
    closeAddSubModal();
  }
});

$("add-sub-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const serviceName = $("manual-service-name").value.trim();
  if (!serviceName) {
    showNotice("Service or merchant name is required.", "error");
    return;
  }

  const costVal = $("manual-cost").value.trim();
  const renewalVal = $("manual-renewal-date").value.trim();
  const lastUsedVal = $("manual-last-used").value.trim();
  const categoryVal = $("manual-category").value.trim();
  const portalUrlVal = $("manual-portal-url").value.trim();
  const notesVal = $("manual-notes").value.trim();

  const payload = {
    service_name: serviceName,
    monthly_cost: costVal ? parseFloat(costVal) : null,
    billing_cycle: $("manual-billing-cycle").value || "monthly",
    currency: "USD",
    renewal_date: renewalVal || null,
    last_used_at: lastUsedVal ? `${lastUsedVal}T00:00:00` : null,
    category: categoryVal || null,
    status: $("manual-status").value || "active",
    notes: notesVal || null,
    manual_action_url: portalUrlVal || null,
  };

  try {
    await api("/subscriptions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    closeAddSubModal();
    showNotice(`Added '${serviceName}' to inventory successfully.`, "success");
    await loadSubscriptions();
  } catch (err) {
    showNotice(`Failed to add subscription: ${err.message}`, "error");
  }
});

// -----------------------------------------------------------------------------
// Edit Subscription Modal Handlers
// -----------------------------------------------------------------------------

function openEditSubModal(sub) {
  const modal = $("edit-sub-modal");
  if (!modal) return;
  $("edit-sub-id").value = sub.subscription_id;
  $("edit-service-name").value = sub.service_name || "";
  $("edit-cost").value = typeof sub.monthly_cost === "number" ? sub.monthly_cost : "";
  $("edit-billing-cycle").value = sub.billing_cycle || "monthly";
  $("edit-renewal-date").value = sub.renewal_date || "";
  $("edit-last-used").value = sub.last_used_at ? sub.last_used_at.split("T")[0] : "";
  $("edit-category").value = sub.category || "";
  $("edit-status").value = sub.status || "active";
  $("edit-notes").value = sub.notes || "";

  modal.hidden = false;
  modal.classList.add("active");
  $("edit-service-name")?.focus();
}

function closeEditSubModal() {
  const modal = $("edit-sub-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.classList.remove("active");
  $("edit-sub-form")?.reset();
}

$("close-edit-sub-modal")?.addEventListener("click", closeEditSubModal);
$("cancel-edit-sub")?.addEventListener("click", closeEditSubModal);
$("edit-sub-modal")?.addEventListener("click", (e) => {
  if (e.target === $("edit-sub-modal")) {
    closeEditSubModal();
  }
});

$("edit-sub-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const subId = $("edit-sub-id").value;
  const serviceName = $("edit-service-name").value.trim();
  if (!serviceName) {
    showNotice("Service or merchant name cannot be blank.", "error");
    return;
  }
  const costVal = $("edit-cost").value.trim();
  const renewalVal = $("edit-renewal-date").value.trim();
  const lastUsedVal = $("edit-last-used").value.trim();
  const categoryVal = $("edit-category").value.trim();
  const notesVal = $("edit-notes").value.trim();

  const payload = {
    service_name: serviceName,
    monthly_cost: costVal !== "" ? parseFloat(costVal) : null,
    billing_cycle: $("edit-billing-cycle").value || "monthly",
    renewal_date: renewalVal || null,
    last_used_at: lastUsedVal ? `${lastUsedVal}T00:00:00` : null,
    category: categoryVal || null,
    status: $("edit-status").value || "active",
    notes: notesVal || null,
  };

  try {
    await api(`/subscriptions/${encodeURIComponent(subId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    closeEditSubModal();
    showNotice(`Updated '${serviceName}' successfully.`, "success");
    await loadSubscriptions();
    if (state.runId) await loadRunSubscriptions();
    openDrawer(subId);
  } catch (err) {
    showNotice(`Failed to update subscription: ${err.message}`, "error");
  }
});

// -----------------------------------------------------------------------------
// User Data Export (GDPR / Privacy)
// -----------------------------------------------------------------------------

async function handleExportUserData() {
  try {
    const data = await api("/api/user/export");
    const jsonBlob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const blobUrl = URL.createObjectURL(jsonBlob);
    const link = document.createElement("a");
    link.href = blobUrl;
    link.download = `subcan_user_data_export_${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(blobUrl);
    showNotice("User data export downloaded successfully.", "success");
  } catch (err) {
    showNotice(`Export failed: ${err.message}`, "error");
  }
}

$("export-user-data-btn")?.addEventListener("click", handleExportUserData);

// -----------------------------------------------------------------------------
// Tab 4: Email Discovery & Review
// -----------------------------------------------------------------------------

async function loadEmailTab() {
  try {
    const status = await api("/api/email/status");
    state.emailAccount = status;

    const pill = $("email-status-pill");
    const label = $("email-status-label");
    const modeBadge = $("oauth-mode-badge");
    const addressDisplay = $("email-address-display");
    const lastSyncDisplay = $("email-last-sync");
    const connectBtn = $("email-connect-btn");
    const disconnectBtn = $("email-disconnect-btn");
    const syncBtn = $("email-sync-btn");

    if (modeBadge) {
      if (status.is_live_configured) {
        modeBadge.textContent = "Google OAuth 2.0 (Live Configured)";
        modeBadge.className = "badge success";
      } else {
        modeBadge.textContent = "Safe Demo Fallback (Mock Mode)";
        modeBadge.className = "badge warning";
      }
    }

    if (status.is_connected || status.connected) {
      const isDemo = status.mode === "demo" || (status.email_address && status.email_address.includes("@subcan-demo.com"));
      if (pill) pill.className = isDemo ? "status-indicator connected warning" : "status-indicator connected";
      if (label) label.textContent = isDemo ? "Connected (Demo Mode)" : "Connected (Live Gmail)";
      if (addressDisplay) {
        const badgeHtml = isDemo
          ? `<span class="badge warning" style="font-size: 11px; margin-left: 8px;">Demo Mock Mailbox</span>`
          : `<span class="badge success" style="font-size: 11px; margin-left: 8px;">Authorized Gmail Account</span>`;
        addressDisplay.innerHTML = `${escapeHtml(status.email_address || "Connected Mailbox")} ${badgeHtml}`;
      }
      if (lastSyncDisplay) lastSyncDisplay.textContent = status.last_synced_at ? formatDate(status.last_synced_at) : "Never";
      if (connectBtn) connectBtn.style.display = "none";
      if (disconnectBtn) disconnectBtn.style.display = "inline-flex";
      if (syncBtn) syncBtn.disabled = false;
    } else {
      if (pill) pill.className = "status-indicator disconnected";
      if (label) label.textContent = "Disconnected";
      if (addressDisplay) addressDisplay.textContent = "No connected mailbox";
      if (lastSyncDisplay) lastSyncDisplay.textContent = "Never";
      if (connectBtn) {
        connectBtn.style.display = "inline-flex";
        connectBtn.innerHTML = status.is_live_configured
          ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg> Connect Gmail Account`
          : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg> Connect Demo Mailbox (Mock)`;
      }
      if (disconnectBtn) disconnectBtn.style.display = "none";
      if (syncBtn) syncBtn.disabled = true;
    }

    await loadCandidates();
  } catch (err) {
    showNotice(`Failed to load email account status: ${err.message}`, "error");
  }
}

async function loadCandidates() {
  const loader = $("candidates-loading");
  const tbody = $("candidate-rows");
  const countBadge = $("candidate-count-badge");
  if (loader) loader.hidden = false;

  try {
    const candidates = await api("/api/email/candidates");
    state.candidates = candidates;

    const pending = candidates.filter((c) => (c.review_status || c.status) === "pending");
    if (countBadge) countBadge.textContent = `${pending.length} candidate${pending.length === 1 ? "" : "s"} pending`;

    if (!candidates.length) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No candidate subscriptions discovered yet. Click 'Sync Billing Emails' to scan.</td></tr>`;
      return;
    }

    if (tbody) {
      tbody.innerHTML = candidates.map((c) => {
        const safeId = escapeHtml(c.candidate_id);
        const safeMerchant = escapeHtml(c.merchant_name || "Unknown Merchant");
        const planName = c.product_name || c.plan_name;
        const safePlan = planName ? `<div style="font-size: 11px; color: var(--text-muted);">${escapeHtml(planName)}</div>` : "";
        const cost = c.estimated_cost ?? c.extracted_amount;
        const costStr = typeof cost === "number" ? `$${cost.toFixed(2)} ${escapeHtml(c.currency || "USD")}` : `<span style="color: var(--text-muted);">Unknown</span>`;
        const cycleStr = escapeHtml(c.billing_interval || c.extracted_interval || "Unknown");
        const renewalStr = escapeHtml(c.detected_renewal_date || c.extracted_renewal_date || "Unknown");
        const confPct = Math.round((c.confidence || 0) * 100);
        const confBadge = confPct >= 70 ? badge(`${confPct}%`, "success") : badge(`${confPct}%`, "warning");
        const cancelSignal = c.cancellation_signal || c.cancellation_signal_detected;
        const cancelSignalBadge = cancelSignal ? badge("Cancellation Signal", "danger") : "";
        const snippetText = c.email_snippet || c.raw_snippet || "Receipt metadata extracted from billing notification.";
        const snippet = escapeHtml(snippetText);

        const revStatus = c.review_status || c.status;
        let actionHtml = "";
        if (revStatus === "pending") {
          actionHtml = `<div style="display: flex; gap: 6px;">
            <button class="button small primary" onclick="confirmCandidate('${safeId}')">Confirm</button>
            <button class="button small secondary" onclick="dismissCandidate('${safeId}')">Dismiss</button>
          </div>`;
        } else if (revStatus === "confirmed") {
          actionHtml = badge("Confirmed", "success");
        } else {
          actionHtml = badge("Dismissed", "");
        }

        return `<tr>
          <td>
            <div style="font-weight: 700; color: #fff;">${safeMerchant}</div>
            ${safePlan}
          </td>
          <td><strong>${costStr}</strong></td>
          <td><span style="text-transform: capitalize;">${cycleStr}</span></td>
          <td>${renewalStr}</td>
          <td>
            <div style="display: flex; flex-direction: column; gap: 4px;">
              ${confBadge}
              ${cancelSignalBadge}
            </div>
          </td>
          <td style="font-size: 11px; color: var(--text-secondary); max-width: 280px; overflow: hidden; text-overflow: ellipsis;">
            "${snippet}"
          </td>
          <td>${actionHtml}</td>
        </tr>`;
      }).join("");
    }
  } catch (err) {
    showNotice(`Failed to load candidates: ${err.message}`, "error");
  } finally {
    if (loader) loader.hidden = true;
  }
}

async function handleConnectEmail() {
  try {
    const res = await api("/api/email/oauth/url");
    if (res.is_live_configured && res.url && res.url.startsWith("https://accounts.google.com")) {
      // Live Google OAuth 2.0 flow -> redirect browser to Google consent screen
      window.location.href = res.url;
    } else {
      // Safe demo / simulated OAuth flow
      await api(`/api/email/oauth/callback?code=mock_oauth_code&state=${encodeURIComponent(res.state)}`);
      showNotice("Connected to demo Gmail account (Mock Mode).", "success");
      await loadEmailTab();
    }
  } catch (err) {
    showNotice(`OAuth authorization failed: ${err.message}`, "error");
  }
}

async function handleDisconnectEmail() {
  if (!confirm("Are you sure you want to disconnect this mailbox and revoke OAuth access?")) {
    return;
  }
  try {
    await api("/api/email/disconnect", { method: "POST" });
    showNotice("Mailbox disconnected and OAuth tokens cleared.", "info");
    await loadEmailTab();
  } catch (err) {
    showNotice(`Disconnect failed: ${err.message}`, "error");
  }
}

async function handleSyncEmail() {
  const btn = $("email-sync-btn");
  const loader = $("candidates-loading");
  if (btn) btn.disabled = true;
  if (loader) loader.hidden = false;
  try {
    const res = await api("/api/email/sync", { method: "POST" });
    showNotice(`Mailbox scan complete: processed ${res.synced_count} billing email(s).`, "success");
    await loadEmailTab();
    await loadSubscriptions();
  } catch (err) {
    showNotice(`Email sync failed: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
    if (loader) loader.hidden = true;
  }
}

async function confirmCandidate(candId) {
  try {
    const res = await api(`/api/email/candidates/${candId}/confirm`, { method: "POST" });
    showNotice(`Candidate confirmed and added as '${res.service_name}' to inventory!`, "success");
    await loadCandidates();
    await loadSubscriptions();
  } catch (err) {
    showNotice(`Failed to confirm candidate: ${err.message}`, "error");
  }
}

async function dismissCandidate(candId) {
  try {
    await api(`/api/email/candidates/${candId}/dismiss`, { method: "POST" });
    showNotice("Candidate dismissed.", "info");
    await loadCandidates();
  } catch (err) {
    showNotice(`Failed to dismiss candidate: ${err.message}`, "error");
  }
}

// Make candidate actions accessible to inline handlers
window.confirmCandidate = confirmCandidate;
window.dismissCandidate = dismissCandidate;

$("email-connect-btn")?.addEventListener("click", handleConnectEmail);
$("email-disconnect-btn")?.addEventListener("click", handleDisconnectEmail);
$("email-sync-btn")?.addEventListener("click", handleSyncEmail);

// -----------------------------------------------------------------------------
// Initialization & Global Event Listeners
// -----------------------------------------------------------------------------

$("refresh")?.addEventListener("click", () => {
  loadSubscriptions();
  if (state.runId) loadRunSubscriptions();
});

$("start-run")?.addEventListener("click", startReviewRun);
$("search")?.addEventListener("input", renderRows);
$("status-filter")?.addEventListener("change", renderRows);
$("eligibility-filter")?.addEventListener("change", renderRows);

// Initial deterministic boot: close drawer and load subscriptions
closeDrawer();
loadSubscriptions();

// Check for OAuth callback redirects
if (window.location.hash.startsWith("#email-connected")) {
  switchTab("email");
  showNotice("Gmail account successfully connected via Google OAuth!", "success");
  history.replaceState(null, "", window.location.pathname);
} else if (window.location.hash.startsWith("#email-error")) {
  switchTab("email");
  const params = new URLSearchParams(window.location.hash.split("?")[1] || "");
  const errorMsg = params.get("error") || "Authorization failed.";
  showNotice(`Google OAuth error: ${errorMsg}`, "error");
  history.replaceState(null, "", window.location.pathname);
}

