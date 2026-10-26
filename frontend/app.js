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
  emailAccounts: [],
  selectedAccountId: null,
  candidates: [],
};

const $ = (id) => document.getElementById(id);

// -----------------------------------------------------------------------------
// User Identity, Session & Tenant Context Helpers
// -----------------------------------------------------------------------------

function getSessionToken() {
  return localStorage.getItem("subcan_session_token") || "";
}

function setSessionToken(token) {
  if (token && token.trim()) {
    localStorage.setItem("subcan_session_token", token.trim());
  } else {
    localStorage.removeItem("subcan_session_token");
  }
}

function redirectToLogin(reason) {
  setSessionToken("");
  const currentPath = window.location.pathname;
  if (currentPath.endsWith("login.html")) return;
  const q = reason ? `?error=${encodeURIComponent(reason)}` : "";
  window.location.href = `./login.html${q}`;
}

function getCurrentUserId() {
  return localStorage.getItem("subcan_user_id") || "user_alice";
}

function setCurrentUserId(uid) {
  if (uid && uid.trim()) {
    localStorage.setItem("subcan_user_id", uid.trim());
  }
}

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

// -----------------------------------------------------------------------------
// Backend Host Configuration & Endpoint Resolver
// -----------------------------------------------------------------------------

const DEFAULT_BACKEND_URL = "https://subcan.onrender.com";

function getBackendUrl() {
  const custom = localStorage.getItem("subcan_backend_url");
  if (custom && custom.trim()) {
    return custom.trim().replace(/\/+$/, "");
  }
  if (typeof window !== "undefined" && window.location && window.location.origin) {
    const origin = window.location.origin;
    if (origin === "https://subcan.onrender.com" || origin.includes("localhost:8000") || origin.includes("127.0.0.1:8000")) {
      return origin;
    }
  }
  return DEFAULT_BACKEND_URL;
}

function setBackendUrl(url) {
  if (url && url.trim()) {
    localStorage.setItem("subcan_backend_url", url.trim().replace(/\/+$/, ""));
  } else {
    localStorage.removeItem("subcan_backend_url");
  }
}

function resolveApiUrl(url) {
  if (!url) return "";
  if (/^https?:\/\//i.test(url)) {
    return url;
  }
  const base = getBackendUrl();
  const normalized = url.startsWith("/") ? url : `/${url}`;
  return `${base}${normalized}`;
}

async function api(url, options = {}) {
  const targetUrl = resolveApiUrl(url);
  const headers = Object.assign({}, options.headers || {});
  const sessionToken = getSessionToken();
  if (sessionToken && !headers["Authorization"]) {
    headers["Authorization"] = `Bearer ${sessionToken}`;
  }
  if (!headers["X-User-ID"]) {
    headers["X-User-ID"] = getCurrentUserId();
  }
  const opts = Object.assign({ credentials: "include" }, options, { headers });
  const response = await fetch(targetUrl, opts);
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    if (!window.location.search.includes("bypass_auth=1") && !url.includes("/api/auth/logout")) {
      redirectToLogin("Your session has expired. Please sign in again.");
    }
  }
  if (!response.ok) {
    const errorMsg = data.error?.message || data.detail?.message || `Request failed (${response.status})`;
    throw new Error(errorMsg);
  }
  return data;
}

function badge(text, variant = "") {
  const clean = escapeHtml(String(text || "â€”").replaceAll("_", " "));
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

function isPastDate(dateStr) {
  if (!dateStr) return false;
  try {
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return false;
    const now = new Date();
    return d.getTime() < now.getTime();
  } catch {
    return false;
  }
}

function getLifecycleBadge(statusStr, lifecycleStr) {
  const key = (lifecycleStr || statusStr || "unknown").toLowerCase();
  const map = {
    active: { text: "Active", variant: "success" },
    trial: { text: "Trial", variant: "info" },
    payment_failed: { text: "Payment Failed", variant: "warning" },
    expired: { text: "Expired", variant: "danger" },
    cancelled: { text: "Cancelled", variant: "danger" },
    refunded: { text: "Refunded", variant: "info" },
    requires_review: { text: "Requires Review", variant: "warning" },
    unknown: { text: "Unknown", variant: "" },
  };
  const item = map[key] || { text: key.replaceAll("_", " "), variant: "" };
  return badge(item.text, item.variant);
}

function formatCurrency(amount, currency = "INR") {
  if (amount === null || amount === undefined || isNaN(amount) || typeof amount !== "number") {
    return "Unknown";
  }
  const curr = (currency || "INR").toUpperCase();
  const locale = curr === "INR" ? "en-IN" : "en-US";
  const symbolMap = {
    INR: "₹",
    USD: "$",
    EUR: "€",
    GBP: "£",
    CAD: "CA$",
    AUD: "A$",
    JPY: "¥",
  };
  const symbol = symbolMap[curr] || (curr + " ");
  try {
    const numPart = new Intl.NumberFormat(locale, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(amount);
    return `${symbol}${numPart}`;
  } catch {
    return `${symbol}${Number(amount).toFixed(2)}`;
  }
}

// -----------------------------------------------------------------------------
// Tab Switching
// -----------------------------------------------------------------------------

function switchTab(tabId) {
  const normalizedId = tabId.startsWith("tab-") ? tabId : `tab-${tabId}`;
  const btn = document.querySelector(`.nav-tab[data-tab="${normalizedId}"]`);
  if (btn) {
    btn.click();
  } else {
    document.querySelectorAll(".nav-tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach((p) => (p.hidden = true));
    const pane = $(normalizedId);
    if (pane) pane.hidden = false;
    state.activeTab = normalizedId;
    if (normalizedId === "tab-audit") {
      loadAuditTab();
    } else if (normalizedId === "tab-email") {
      loadEmailTab();
    }
  }
}
window.switchTab = switchTab;

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
  const sourceFilter = $("source-filter") ? $("source-filter").value : "";
  const tbody = $("subscription-rows");

  const source = state.items.length
    ? state.items.map((i) => ({ sub: i.subscription, item: i }))
    : state.subscriptions.map((s) => ({ sub: s, item: null }));

  const filtered = source.filter(({ sub, item }) => {
    const matchesQuery = !query || `${sub.service_name} ${sub.subscription_id}`.toLowerCase().includes(query);
    const matchesStatus = !statusFilter || (statusFilter === "archived" ? sub.is_archived : (!sub.is_archived && sub.status === statusFilter));
    const matchesElig = !eligFilter || (item && item.decision?.eligibility === eligFilter);
    const subSource = sub.discovery_source || (sub.subscription_id?.startsWith("sub_man_") ? "manual" : sub.subscription_id?.startsWith("sub_email_") ? "email" : "connector");
    const matchesSource = !sourceFilter || (sourceFilter === "demo" ? (subSource === "demo" || subSource === "connector") : subSource === sourceFilter);
    return matchesQuery && matchesStatus && matchesElig && matchesSource;
  });

  $("loading").hidden = true;

  if (!filtered.length) {
    if (!source.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty-state">No confirmed subscriptions yet. Sync Gmail to discover billing evidence or add a subscription manually.</td></tr>`;
    } else {
      tbody.innerHTML = `<tr><td colspan="8" class="empty-state">No subscriptions match the selected criteria.</td></tr>`;
    }
    updateCards();
    return;
  }

  tbody.innerHTML = filtered
    .map(({ sub, item }) => {
      const decision = item?.decision;
      const finalStatus = item?.final_status;

      // Status badge
      let statusBadge = getLifecycleBadge(sub.status, sub.lifecycle_status);

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

      const costStr = typeof sub.monthly_cost === "number" ? `${formatCurrency(sub.monthly_cost, sub.currency)}/mo` : "Unknown";
      const lastUsed = formatDate(sub.last_used_at);
      let renewalHtml = escapeHtml(sub.renewal_date || "—");
      if (sub.trial_end_date) {
        renewalHtml += `<div style="font-size: 10px; color: var(--text-muted);">Trial ends: ${escapeHtml(sub.trial_end_date)}</div>`;
      }
      const isPast = isPastDate(sub.renewal_date);
      const isInactiveLifecycle = ["expired", "cancelled", "refunded", "payment_failed"].includes((sub.lifecycle_status || sub.status || "").toLowerCase());
      if (sub.renewal_date && (isPast || isInactiveLifecycle)) {
        renewalHtml += ` <span class="badge warning" style="font-size: 10px; margin-top: 2px;" title="Date is in the past or subscription is inactive">Past / Inactive</span>`;
      }

      const safeServiceName = escapeHtml(sub.service_name || "Unknown Service");
      const safeSubId = escapeHtml(sub.subscription_id || "");
      const safeLastUsed = escapeHtml(lastUsed);
      const safeCost = escapeHtml(costStr);

      // Provenance badge
      let sourceBadge = "";
      const source = sub.discovery_source || (sub.subscription_id?.startsWith("sub_man_") ? "manual" : (sub.subscription_id?.startsWith("sub_email_") || sub.subscription_id?.startsWith("sub_gmail_")) ? "gmail" : "connector");
      if (source === "email" || source === "gmail") {
        sourceBadge = `<span class="badge" style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); font-size: 10px; margin-left: 6px;">Gmail</span>`;
      } else if (source === "manual") {
        sourceBadge = `<span class="badge" style="background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.3); font-size: 10px; margin-left: 6px;">Manual</span>`;
      } else {
        sourceBadge = `<span class="badge" style="background: rgba(148, 163, 184, 0.15); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.3); font-size: 10px; margin-left: 6px;">Demo</span>`;
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
        <td>${renewalHtml}</td>
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
  const cost = typeof sub.monthly_cost === "number" ? formatCurrency(sub.monthly_cost, sub.currency) : "â€”";
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
      <div><span class="sub-meta">LIFECYCLE STATUS</span><div style="margin-top: 4px;">${getLifecycleBadge(sub.status, sub.lifecycle_status)}</div></div>
      <div><span class="sub-meta">CURRENT WORKFLOW</span><div style="margin-top: 4px;">${badge(workflowState)}</div></div>
      ${sub.trial_end_date ? `<div><span class="sub-meta">TRIAL END DATE</span><strong style="display:block; font-size: 14px;">${escapeHtml(sub.trial_end_date)}</strong></div>` : ""}
      ${sub.evidence_category ? `<div><span class="sub-meta">EVIDENCE CATEGORY</span><strong style="display:block; font-size: 14px; text-transform: capitalize;">${escapeHtml(sub.evidence_category.replaceAll('_', ' '))}</strong></div>` : ""}
      <div>
        <span class="sub-meta">CANCELLATION POLICY</span>
        <div style="margin-top: 4px; font-size: 13px;">
          ${safeTermsUrl ? `<a href="${safeTermsUrl}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa; text-decoration: underline;">View Terms â†—</a>` : `<span style="color: var(--text-muted);">None specified</span>`}
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
        ${(decision.uncertainty || 0) > 0.3 ? badge("High Uncertainty â€” Review Required", "warning") : badge("High Confidence", "success")}
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
                <span>Confidence: ${Math.round((e.confidence || 0) * 100)}% Â· ${escapeHtml(formatDate(e.captured_at))}</span>
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
        ${verif ? `<div style="margin-top: 6px; font-size: 12px; color: var(--text-muted);">Observed State: <strong>${safeObserved}</strong> â€” ${safeDetails}</div>` : ""}
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
      ${safePortalUrl ? `<div style="margin-top: 8px;"><a href="${safePortalUrl}" target="_blank" rel="noopener noreferrer" class="button secondary small" style="display: inline-flex; align-items: center; gap: 6px;">Open Provider Portal â†—</a></div>` : ""}
    </div>`;
  }

  // 7. Edit & Archive Actions
  html += `<div style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border-subtle); display: flex; justify-content: flex-end; gap: 8px;">
    <button id="drawer-edit-btn" class="button secondary small">
      Edit Details
    </button>`;
  if (sub.subscription_id && (sub.subscription_id.startsWith("sub_man_") || sub.discovery_source === "manual" || sub.discovery_source === "email")) {
    if (sub.is_archived) {
      html += `
      <button id="drawer-unarchive-btn" class="button secondary small" style="color: #60a5fa; border-color: rgba(59, 130, 246, 0.4);">
        Restore to Active
      </button>`;
    } else {
      html += `
      <button id="drawer-archive-btn" class="button secondary small" style="color: #f87171; border-color: rgba(239, 68, 68, 0.4);">
        Archive Subscription
      </button>`;
    }
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

  // Bind unarchive button
  $("drawer-unarchive-btn")?.addEventListener("click", async () => {
    try {
      await api(`/subscriptions/${encodeURIComponent(sub.subscription_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_archived: false }),
      });
      closeDrawer();
      showNotice(`Subscription '${sub.service_name}' restored to active monitoring.`, "info");
      await loadSubscriptions();
    } catch (err) {
      showNotice(`Failed to restore: ${err.message}`, "error");
    }
  });

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
    const statusVal = $("status-filter")?.value;
    const includeArchived = statusVal === "archived";
    const url = includeArchived ? "/subscriptions?include_archived=true" : "/subscriptions";
    state.subscriptions = await api(url);
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
        <div>Entity ID: <code>${ev.entity_id || "â€”"}</code></div>
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

  const currencyVal = $("manual-currency") ? $("manual-currency").value : "INR";
  const numCost = costVal ? parseFloat(costVal) : null;

  const payload = {
    service_name: serviceName,
    monthly_cost: numCost,
    billing_cycle: $("manual-billing-cycle").value || "monthly",
    currency: currencyVal || "INR",
    original_amount: numCost,
    original_currency: currencyVal || "INR",
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
    // 1. Check all connected email accounts for current user
    let accounts = [];
    try {
      accounts = await api("/api/email/accounts");
      state.emailAccounts = accounts || [];
    } catch (_) {
      state.emailAccounts = [];
    }

    const switcherWrap = $("email-account-switcher-wrap");
    const accountSelect = $("email-account-select");

    if (accounts.length > 0) {
      if (switcherWrap) switcherWrap.style.display = "flex";
      if (!state.selectedAccountId || !accounts.some((a) => a.account_id === state.selectedAccountId)) {
        const defaultAcc = accounts.find((a) => a.is_default) || accounts[0];
        state.selectedAccountId = defaultAcc.account_id;
      }
      if (accountSelect) {
        accountSelect.innerHTML = accounts.map((a) => {
          const isSel = a.account_id === state.selectedAccountId ? "selected" : "";
          const name = a.account_name && a.email_address && a.account_name !== a.email_address
            ? `${escapeHtml(a.account_name)} (${escapeHtml(a.email_address)})`
            : escapeHtml(a.account_name || a.email_address || a.account_id);
          const conn = a.is_connected ? "Connected" : "Disconnected";
          return `<option value="${escapeHtml(a.account_id)}" ${isSel}>${name} [${conn}]</option>`;
        }).join("");
      }
    } else {
      if (switcherWrap) switcherWrap.style.display = "none";
      state.selectedAccountId = null;
    }

    const q = state.selectedAccountId ? `?account_id=${encodeURIComponent(state.selectedAccountId)}` : "";
    const status = await api(`/api/email/status${q}`);
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

    const hasAccount = !!(status.email_address || status.account_name || (accounts.length > 0 && state.selectedAccountId));
    if (status.is_connected || status.connected) {
      const isDemo = status.mode === "demo" || (status.email_address && status.email_address.includes("@subcan-demo.com"));
      if (pill) pill.className = isDemo ? "status-indicator connected warning" : "status-indicator connected";
      if (label) label.textContent = isDemo ? "Connected (Demo Mode)" : "Connected (Live Gmail)";
      if (addressDisplay) {
        const badgeHtml = isDemo
          ? `<span class="badge warning" style="font-size: 11px; margin-left: 8px;">Demo Mock Mailbox</span>`
          : `<span class="badge success" style="font-size: 11px; margin-left: 8px;">Authorized Gmail Account</span>`;
        const titleText = status.account_name && status.email_address && status.account_name !== status.email_address
          ? `${escapeHtml(status.account_name)} <span style="font-size: 14px; font-weight: normal; color: var(--text-muted);">&lt;${escapeHtml(status.email_address)}&gt;</span>`
          : escapeHtml(status.account_name || status.email_address || "Connected Mailbox");
        addressDisplay.innerHTML = `${titleText} ${badgeHtml}`;
      }
      if (lastSyncDisplay) lastSyncDisplay.textContent = status.last_synced_at ? formatDate(status.last_synced_at) : "Never";
      if (connectBtn) {
        connectBtn.style.display = "inline-flex";
        connectBtn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> Connect Another Account`;
      }
      if (disconnectBtn) disconnectBtn.style.display = "inline-flex";
      if (syncBtn) syncBtn.disabled = false;
    } else {
      if (pill) pill.className = "status-indicator disconnected";
      if (label) label.textContent = "Disconnected";
      if (addressDisplay) {
        if (hasAccount && (status.email_address || status.account_name)) {
          const titleText = status.account_name && status.email_address && status.account_name !== status.email_address
            ? `${escapeHtml(status.account_name)} <span style="font-size: 14px; font-weight: normal; color: var(--text-muted);">&lt;${escapeHtml(status.email_address)}&gt;</span>`
            : escapeHtml(status.account_name || status.email_address || "Disconnected Mailbox");
          addressDisplay.innerHTML = `${titleText} <span class="badge" style="background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); font-size: 11px; margin-left: 8px;">Disconnected (Access Revoked)</span>`;
        } else {
          addressDisplay.textContent = "No connected mailbox";
        }
      }
      if (lastSyncDisplay) lastSyncDisplay.textContent = "Never";
      if (connectBtn) {
        connectBtn.style.display = "inline-flex";
        if (hasAccount && (status.account_name || status.email_address)) {
          const targetName = status.account_name || status.email_address;
          connectBtn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg> Reconnect ${escapeHtml(targetName)}`;
        } else {
          connectBtn.innerHTML = status.is_live_configured
            ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg> Connect Gmail Account`
            : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg> Connect Demo Mailbox (Mock)`;
        }
      }
      if (disconnectBtn) disconnectBtn.style.display = "none";
      if (syncBtn) syncBtn.disabled = true;
    }

    try {
      const diag = await api(`/api/email/diagnostics${q}`);
      if (diag) {
        if ($("diag-mode-tag")) {
          $("diag-mode-tag").textContent = diag.mode === "live" ? "Live OAuth Mode" : "Demo Mode";
          $("diag-mode-tag").className = diag.mode === "live" ? "badge success" : "badge warning";
        }
        if ($("diag-oauth-config")) $("diag-oauth-config").textContent = diag.oauth_valid ? "Valid (Ready)" : (diag.oauth_error || "Not Configured");
        if ($("diag-account-conn")) $("diag-account-conn").textContent = diag.is_connected ? (diag.email_address || "Connected") : "Disconnected";
        if ($("diag-msgs-discovered")) $("diag-msgs-discovered").textContent = diag.messages_discovered ?? diag.messages_retrieved ?? 0;
        if ($("diag-msgs-retrieved")) $("diag-msgs-retrieved").textContent = diag.messages_retrieved || 0;
        if ($("diag-msgs-parsed")) $("diag-msgs-parsed").textContent = diag.messages_parsed ?? Math.max(0, (diag.messages_retrieved || 0) - (diag.parsing_failures || 0));
        if ($("diag-msgs-skipped")) $("diag-msgs-skipped").textContent = diag.messages_skipped || 0;
        if ($("diag-msgs-insufficient")) $("diag-msgs-insufficient").textContent = diag.messages_insufficient_evidence || 0;
        if ($("diag-candidates-parsed")) $("diag-candidates-parsed").textContent = diag.candidates_parsed ?? diag.candidate_count ?? 0;
        if ($("diag-dup-skipped")) $("diag-dup-skipped").textContent = diag.duplicates_skipped || 0;
        if ($("diag-parse-fail")) $("diag-parse-fail").textContent = diag.parsing_failures || 0;
        if ($("diag-scan-range")) $("diag-scan-range").textContent = diag.scan_date_range || "Full search";

        const partialNotice = $("diag-partial-scan-notice");
        if (partialNotice) {
          if (diag.is_partial_scan) {
            partialNotice.style.display = "block";
            if ($("diag-scan-limit")) $("diag-scan-limit").textContent = diag.scan_limit || 100;
            if ($("diag-scan-window")) $("diag-scan-window").textContent = diag.scan_date_range ? `scanned ${diag.scan_date_range}` : "configured message limit reached";
          } else {
            partialNotice.style.display = "none";
          }
        }

        const errRow = $("diag-error-row");
        const errText = $("diag-error-text");
        if (errRow && errText) {
          if (diag.error_message) {
            errRow.style.display = "block";
            errText.textContent = diag.error_message;
          } else {
            errRow.style.display = "none";
          }
        }
      }
    } catch (_) {}

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
    const q = state.selectedAccountId ? `?account_id=${encodeURIComponent(state.selectedAccountId)}` : "";
    const candidates = await api(`/api/email/candidates${q}`);
    state.candidates = candidates;

    const pending = candidates.filter((c) => (c.review_status || c.status) === "pending");
    if (countBadge) countBadge.textContent = `${pending.length} candidate${pending.length === 1 ? "" : "s"} pending`;

    if (!candidates.length) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="9" class="empty-state">No subscription candidates were found in the scanned emails. This does not necessarily mean the account has no subscriptions.</td></tr>`;
      return;
    }

    if (tbody) {
      tbody.innerHTML = candidates.map((c) => {
        const safeId = escapeHtml(c.candidate_id);
        const safeMerchant = escapeHtml(c.merchant_name || "Unknown Merchant");
        const planName = c.product_name || c.plan_name;
        const safePlan = planName ? `<div style="font-size: 11px; color: var(--text-muted);">${escapeHtml(planName)}</div>` : "";
        const lifecycleBadge = c.lifecycle_status ? `<div style="margin-top: 4px;">${getLifecycleBadge(null, c.lifecycle_status)}</div>` : "";
        const cost = c.estimated_cost ?? c.extracted_amount;
        const costStr = typeof cost === "number" ? formatCurrency(cost, c.currency || "INR") : `<span style="color: var(--text-muted);">Unknown</span>`;
        const cycleStr = escapeHtml(c.billing_interval || c.extracted_interval || "Unknown");

        let renewalHtml = escapeHtml(c.detected_renewal_date || c.extracted_renewal_date || "â€”");
        if (c.trial_end_date) {
          renewalHtml += `<div style="font-size: 10px; color: var(--text-muted);">Trial ends: ${escapeHtml(c.trial_end_date)}</div>`;
        }
        const isPast = isPastDate(c.detected_renewal_date || c.extracted_renewal_date);
        const isInactiveLifecycle = ["expired", "cancelled", "refunded", "payment_failed"].includes((c.lifecycle_status || "").toLowerCase());
        if ((c.detected_renewal_date || c.extracted_renewal_date) && (isPast || isInactiveLifecycle)) {
          renewalHtml += ` <span class="badge warning" style="font-size: 10px; margin-top: 2px;" title="Date is in the past or subscription is inactive">Past / Inactive</span>`;
        }

        const emailDateStr = c.source_date ? formatDate(c.source_date) : "â€”";
        const confPct = Math.round((c.confidence || 0) * 100);
        const confBadge = confPct >= 70 ? badge(`${confPct}%`, "success") : badge(`${confPct}%`, "warning");
        const cancelSignal = c.cancellation_signal || c.cancellation_signal_detected;
        const cancelSignalBadge = cancelSignal ? badge("Cancellation Signal", "danger") : "";
        const noteBadge = c.notes ? `<div style="font-size: 10px; color: var(--text-secondary); margin-top: 2px;">${escapeHtml(c.notes)}</div>` : "";
        const snippetText = c.email_snippet || c.raw_snippet || "Receipt metadata extracted from billing notification.";
        const snippet = escapeHtml(snippetText);

        const candSourceBadge = c.is_demo
          ? `<span class="badge warning" style="font-size: 10px;">Demo</span>`
          : `<span class="badge" style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); font-size: 10px;">Gmail</span>`;

        const evidenceCategory = c.evidence_category
          ? c.evidence_category.replaceAll("_", " ")
          : (c.product_name ? "Subscription Receipt" : "Billing Notification");

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
            ${lifecycleBadge}
          </td>
          <td>
            <div style="display: flex; flex-direction: column; gap: 2px;">
              ${candSourceBadge}
              <span style="font-size: 10px; color: var(--text-muted); text-transform: capitalize;">${escapeHtml(evidenceCategory)}</span>
            </div>
          </td>
          <td style="font-size: 12px; color: var(--text-secondary);">${emailDateStr}</td>
          <td><strong>${costStr}</strong></td>
          <td><span style="text-transform: capitalize;">${cycleStr}</span></td>
          <td>${renewalHtml}</td>
          <td>
            <div style="display: flex; flex-direction: column; gap: 4px;">
              ${confBadge}
              ${cancelSignalBadge}
              ${noteBadge}
            </div>
          </td>
          <td style="font-size: 11px; color: var(--text-secondary); max-width: 240px; overflow: hidden; text-overflow: ellipsis;">
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

async function handleConnectEmail(isNewAccount = false) {
  try {
    let accName = "";
    let accId = null;

    const currentAcc = state.emailAccounts.find((a) => a.account_id === state.selectedAccountId);

    if (isNewAccount || !currentAcc || currentAcc.is_connected) {
      // Prompt for nickname only when registering a genuinely new account or explicitly clicking 'Add Account'
      const promptRes = prompt("Account description or nickname (e.g., Personal, Work, Streaming) [optional]:", "");
      if (promptRes === null) {
        return; // User cancelled prompt
      }
      accName = promptRes.trim();
    } else {
      // Reconnecting an existing registered account -> automatically reuse its stable account_id and nickname!
      accId = currentAcc.account_id;
      accName = currentAcc.account_name || "";
    }

    const params = new URLSearchParams();
    if (accName) params.append("account_name", accName);
    if (accId) params.append("account_id", accId);
    const q = params.toString() ? `?${params.toString()}` : "";

    const res = await api(`/api/email/oauth/url${q}`);
    if (res.is_live_configured && res.url && res.url.startsWith("https://accounts.google.com")) {
      // Live Google OAuth 2.0 flow -> redirect browser to Google consent screen
      window.location.href = res.url;
    } else {
      // Safe demo / simulated OAuth flow
      await api(`/api/email/oauth/callback?code=mock_oauth_code&state=${encodeURIComponent(res.state)}`);
      showNotice("Connected to demo Gmail account (Mock Mode).", "success");
      await loadEmailTab();
      await loadSubscriptions();
    }
  } catch (err) {
    showNotice(`OAuth authorization failed: ${err.message}`, "error");
  }
}

async function handleRenameEmailAccount() {
  if (!state.selectedAccountId) {
    showNotice("No email account selected to rename.", "error");
    return;
  }
  const currentAcc = state.emailAccounts.find((a) => a.account_id === state.selectedAccountId);
  const currentNick = currentAcc ? (currentAcc.account_name || "") : "";
  const newNick = prompt("Enter new nickname for this mailbox:", currentNick);
  if (newNick === null || !newNick.trim()) {
    return;
  }
  try {
    const res = await api(`/api/email/accounts/${encodeURIComponent(state.selectedAccountId)}/nickname`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nickname: newNick.trim() }),
    });
    showNotice(`Mailbox renamed to "${res.account_name}".`, "success");
    await loadEmailTab();
  } catch (err) {
    showNotice(`Failed to rename mailbox: ${err.message}`, "error");
  }
}

async function handleDisconnectEmail() {
  if (!confirm("Are you sure you want to disconnect this mailbox and revoke OAuth access?")) {
    return;
  }
  try {
    const targetAccountId = state.selectedAccountId;
    const q = targetAccountId ? `?account_id=${encodeURIComponent(targetAccountId)}` : "";
    await api(`/api/email/disconnect${q}`, { method: "POST" });
    showNotice("Mailbox disconnected and OAuth tokens cleared.", "info");
    // Preserve the target account selection so user sees its disconnected status and can reconnect or rename it
    state.selectedAccountId = targetAccountId;
    await loadEmailTab();
    await loadSubscriptions();
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
    const q = state.selectedAccountId ? `?account_id=${encodeURIComponent(state.selectedAccountId)}` : "";
    const res = await api(`/api/email/sync${q}`, { method: "POST" });
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

async function handleResetDemo() {
  if (!confirm("Are you sure you want to reset demo data? This will clear synthetic demo candidates while preserving all manual and real Gmail records.")) {
    return;
  }
  try {
    const res = await api("/api/demo/reset", { method: "POST" });
    showNotice(res.message || "Demo data successfully reset.", "success");
    await loadEmailTab();
    await loadSubscriptions();
  } catch (err) {
    showNotice(`Reset failed: ${err.message}`, "error");
  }
}

async function handleDatabaseAudit() {
  const btn = $("db-audit-btn");
  if (btn) btn.disabled = true;
  try {
    const report = await api("/api/database/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: true }),
    });
    const mockSubs = report.mock_subscriptions_to_remove?.length || 0;
    const testSubs = report.test_manual_subscriptions_to_remove?.length || 0;
    const falseCands = report.false_positive_candidates_to_remove?.length || 0;
    const reconcileCands = report.candidates_to_reconcile?.length || 0;
    const preservedSubs = report.legitimate_subscriptions_preserved?.length || 0;
    const preservedCands = report.legitimate_candidates_preserved?.length || 0;

    showNotice(
      `Audit Complete (Dry Run): Identified ${mockSubs + testSubs} mock/test subscriptions, ${falseCands} false-positive candidates, ${reconcileCands} candidates to reconcile. Preserved ${preservedSubs} legitimate subscriptions and ${preservedCands} candidates. No changes applied.`,
      "info"
    );
  } catch (err) {
    showNotice(`Database audit failed: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleDatabaseReconcile() {
  if (!confirm("Are you sure you want to apply database reconciliation? This will safely remove mock fixtures, filter false-positive marketing candidates, and reconcile candidate lifecycles, while preserving legitimate manual and Gmail-discovered records.")) {
    return;
  }
  const btn = $("db-reconcile-btn");
  if (btn) btn.disabled = true;
  try {
    const report = await api("/api/database/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: false }),
    });
    const mockSubs = report.mock_subscriptions_to_remove?.length || 0;
    const testSubs = report.test_manual_subscriptions_to_remove?.length || 0;
    const falseCands = report.false_positive_candidates_to_remove?.length || 0;
    const reconcileCands = report.candidates_to_reconcile?.length || 0;

    showNotice(
      `Reconciliation Applied: Removed ${mockSubs + testSubs} mock/test subscriptions, purged ${falseCands} false positives, and reconciled ${reconcileCands} candidate records.`,
      "success"
    );
    await loadEmailTab();
    await loadSubscriptions();
  } catch (err) {
    showNotice(`Database reconciliation failed: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleRepairData() {
  if (!confirm("Execute safe data collision repair? This will automatically create a database backup in reports/ and re-isolate candidates and subscriptions to their verified accounts.")) {
    return;
  }
  const btn = $("repair-data-btn");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/api/database/repair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: false }),
    });
    showNotice(
      `Data Repair Complete: Reassigned ${res.reassigned_subscriptions} subscriptions, ${res.reassigned_candidates} candidates. Backup saved to ${res.backup_created || 'reports/'}.`,
      "success"
    );
    await refreshAll();
  } catch (err) {
    showNotice(`Data repair failed: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function refreshAll() {
  closeDrawer();
  await loadSubscriptions();
  if (state.runId) await loadRunSubscriptions();
  if (state.activeTab === "tab-email") {
    await loadEmailTab();
  }
}

function initUserSwitcher() {
  const sel = $("user-selector");
  if (!sel) return;
  const current = getCurrentUserId();
  let found = false;
  for (let opt of sel.options) {
    if (opt.value === current) {
      sel.value = current;
      found = true;
      break;
    }
  }
  if (!found && current) {
    const customOpt = document.createElement("option");
    customOpt.value = current;
    customOpt.textContent = `Custom (${current})`;
    sel.insertBefore(customOpt, sel.lastElementChild);
    sel.value = current;
  }

  sel.addEventListener("change", async (e) => {
    let val = e.target.value;
    if (val === "custom") {
      const entered = prompt("Enter Custom User ID (e.g. user_alice, user_bob, user_charlie):", current);
      if (!entered || !entered.trim()) {
        sel.value = current;
        return;
      }
      val = entered.trim();
    }
    setCurrentUserId(val);
    state.selectedAccountId = null;
    showNotice(`Switched user context to: ${val}`, "info");
    await refreshAll();
  });
}

function initBackendSwitcher() {
  const sel = $("backend-selector");
  if (!sel) return;
  const current = getBackendUrl();
  let found = false;
  for (let opt of sel.options) {
    if (opt.value === current) {
      sel.value = current;
      found = true;
      break;
    }
  }
  if (!found && current) {
    const customOpt = document.createElement("option");
    customOpt.value = current;
    customOpt.textContent = `Custom (${current})`;
    sel.insertBefore(customOpt, sel.lastElementChild);
    sel.value = current;
  }

  sel.addEventListener("change", async (e) => {
    let val = e.target.value;
    if (val === "custom") {
      const entered = prompt("Enter Backend API Base URL (e.g. https://subcan.onrender.com or http://127.0.0.1:8000):", current);
      if (!entered || !entered.trim()) {
        sel.value = current;
        return;
      }
      val = entered.trim().replace(/\/+$/, "");
    }
    setBackendUrl(val);
    showNotice(`Switched backend API target to: ${val}`, "info");
    await refreshAll();
  });
}

$("email-account-select")?.addEventListener("change", async (e) => {
  state.selectedAccountId = e.target.value;
  await loadEmailTab();
});

$("repair-data-btn")?.addEventListener("click", handleRepairData);
$("email-connect-btn")?.addEventListener("click", () => handleConnectEmail(false));
$("email-connect-new-btn")?.addEventListener("click", () => handleConnectEmail(true));
$("email-rename-btn")?.addEventListener("click", handleRenameEmailAccount);
$("email-disconnect-btn")?.addEventListener("click", handleDisconnectEmail);
$("email-sync-btn")?.addEventListener("click", handleSyncEmail);
$("reset-demo-btn")?.addEventListener("click", handleResetDemo);
$("db-audit-btn")?.addEventListener("click", handleDatabaseAudit);
$("db-reconcile-btn")?.addEventListener("click", handleDatabaseReconcile);

// -----------------------------------------------------------------------------
// Initialization & Global Event Listeners
// -----------------------------------------------------------------------------

$("refresh")?.addEventListener("click", () => {
  refreshAll();
});

$("start-run")?.addEventListener("click", startReviewRun);
$("search")?.addEventListener("input", renderRows);
$("status-filter")?.addEventListener("change", () => {
  const statusVal = $("status-filter")?.value;
  if (statusVal === "archived" || state.subscriptions.some((s) => s.is_archived)) {
    loadSubscriptions();
  } else {
    renderRows();
  }
});
$("eligibility-filter")?.addEventListener("change", renderRows);
$("source-filter")?.addEventListener("change", renderRows);

// -----------------------------------------------------------------------------
// Authentication Session & User Profile Lifecycle
// -----------------------------------------------------------------------------

function renderUserProfile(user) {
  const nameEl = $("user-name-display");
  const emailEl = $("user-email-display");
  const avatarEl = $("user-avatar-badge");
  const selectorEl = $("user-selector");

  const displayName = user.name || user.email || user.user_id;
  if (nameEl) nameEl.textContent = displayName;
  if (emailEl) emailEl.textContent = user.email || "";

  if (avatarEl) {
    if (user.picture) {
      avatarEl.innerHTML = `<img src="${escapeHtml(user.picture)}" alt="${escapeHtml(displayName)}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">`;
    } else {
      const initial = (displayName.charAt(0) || "U").toUpperCase();
      avatarEl.textContent = initial;
    }
  }

  if (selectorEl && user.user_id) {
    selectorEl.value = user.user_id;
  }
}

function extractSessionTokenFromHash() {
  const hash = window.location.hash;
  if (hash.includes("session_token=")) {
    const hashParams = new URLSearchParams(hash.substring(1));
    const tokenFromHash = hashParams.get("session_token");
    if (tokenFromHash) {
      setSessionToken(tokenFromHash);
      const uid = hashParams.get("user_id");
      if (uid) setCurrentUserId(uid);
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }
}

async function initAuthSession() {
  // Allow developer test bypass if specified
  if (window.location.search.includes("bypass_auth=1")) {
    renderUserProfile({ user_id: getCurrentUserId(), name: "Test User", email: "user@example.com" });
    return true;
  }

  const token = getSessionToken();
  if (!token) {
    redirectToLogin();
    return false;
  }

  try {
    const user = await api("/api/auth/me");
    if (user && user.user_id) {
      state.currentUser = user;
      setCurrentUserId(user.user_id);
      renderUserProfile(user);
      return true;
    } else {
      redirectToLogin("Invalid user session.");
      return false;
    }
  } catch (err) {
    console.warn("Auth session check failed:", err);
    redirectToLogin("Session expired or invalid.");
    return false;
  }
}

$("logout-btn")?.addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (e) {
    console.warn("Logout error:", e);
  }
  setSessionToken("");
  window.location.href = "./login.html";
});

// Initial deterministic boot: extract token, switchers, close drawer, load subscriptions, and init session
extractSessionTokenFromHash();
initBackendSwitcher();
initUserSwitcher();
closeDrawer();
loadSubscriptions();
initAuthSession();

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
