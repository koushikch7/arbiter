/*
 * Dashboard announcements banner — v1.18.0
 *
 * Polls /api/announcements/active on page load, renders one styled banner
 * per active major-change notice, and lets the operator dismiss locally
 * (per-browser via localStorage). Dismissed entries reappear if the
 * operator clears their browser data or opens the dashboard elsewhere —
 * by design, so important notices are not silently lost.
 */
(function () {
  "use strict";

  const MOUNT_ID  = "announcement-banners";
  const SEEN_KEY  = "arbiter.announcements.dismissed";
  const POLL_MS   = 5 * 60 * 1000; // refresh announcements every 5 min

  function readDismissed() {
    try {
      const raw = localStorage.getItem(SEEN_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (_) { return []; }
  }

  function rememberDismissed(id) {
    const set = new Set(readDismissed());
    set.add(id);
    // Cap to last 200 entries so the list never grows unboundedly
    const arr = Array.from(set).slice(-200);
    try { localStorage.setItem(SEEN_KEY, JSON.stringify(arr)); } catch (_) {}
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function renderBanner(ann) {
    const sev = ["info", "warning", "critical"].includes(ann.severity) ? ann.severity : "warning";
    const impactedProviders = (ann.impacted_providers || []).map(escapeHtml).join(", ");
    const impactedTokens = (ann.impacted_tokens || [])
      .slice(0, 8)
      .map(function (t) {
        return escapeHtml(t.token_name || t.token_id);
      })
      .join(", ");

    const extraImpactedCount = Math.max(0, (ann.impacted_tokens || []).length - 8);

    let footer = "";
    if (ann.action_required) {
      footer += '<div class="ann-action"><strong>Action required:</strong> ' + escapeHtml(ann.action_required) + "</div>";
    }
    if (impactedProviders) {
      footer += '<div class="ann-impacted"><strong>Impacted providers:</strong> ' + impactedProviders + "</div>";
    }
    if (impactedTokens) {
      footer += '<div class="ann-impacted"><strong>Impacted gateways:</strong> ' + impactedTokens
              + (extraImpactedCount ? " <em>(+" + extraImpactedCount + " more)</em>" : "") + "</div>";
    }
    if (ann.docs_url) {
      footer += '<div class="ann-docs"><a href="' + escapeHtml(ann.docs_url) + '" target="_blank" rel="noopener">More info →</a></div>';
    }

    const expires = new Date(ann.expires_at * 1000);
    const expiresLabel = expires.toLocaleDateString() + " " + expires.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

    return ''
      + '<div class="announcement-banner severity-' + sev + '" data-ann-id="' + escapeHtml(ann.id) + '">'
      +   '<div class="ann-head">'
      +     '<span class="ann-sev">' + sev.toUpperCase() + '</span>'
      +     '<span class="ann-title">' + escapeHtml(ann.title) + '</span>'
      +     '<button class="ann-dismiss" type="button" aria-label="Dismiss">×</button>'
      +   '</div>'
      +   '<div class="ann-body">' + escapeHtml(ann.body) + '</div>'
      +   footer
      +   '<div class="ann-meta">Visible until ' + escapeHtml(expiresLabel) + '</div>'
      + '</div>';
  }

  async function fetchAndRender() {
    const mount = document.getElementById(MOUNT_ID);
    if (!mount) return;

    let data;
    try {
      const resp = await fetch("/api/announcements/active", { credentials: "same-origin" });
      if (!resp.ok) return;
      data = await resp.json();
    } catch (_) {
      return;
    }
    const dismissed = new Set(readDismissed());
    const visible = (data.announcements || []).filter(function (a) { return !dismissed.has(a.id); });

    if (!visible.length) {
      mount.innerHTML = "";
      return;
    }

    mount.innerHTML = visible.map(renderBanner).join("");

    mount.querySelectorAll(".ann-dismiss").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const wrapper = btn.closest(".announcement-banner");
        if (!wrapper) return;
        const id = wrapper.getAttribute("data-ann-id");
        if (id) rememberDismissed(id);
        wrapper.style.transition = "opacity .25s ease";
        wrapper.style.opacity = "0";
        setTimeout(function () { wrapper.remove(); }, 260);
      });
    });
  }

  function init() {
    fetchAndRender();
    setInterval(function () {
      if (!document.hidden) fetchAndRender();
    }, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
