/* ─────────────────────────────────────────────────────────────────────────
 *  Arbiter — shared sidebar component
 *
 *  Renders the same sidebar markup on every page so navigation is uniform
 *  and maintained in ONE place. Pages should now contain only:
 *
 *      <aside class="sidebar" id="sidebar"></aside>
 *      <script src="/static/components/sidebar.js"></script>
 *
 *  The script auto-injects the brand, nav items, and footer, and marks the
 *  current page as `active` based on `window.location.pathname`.
 * ──────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  // Single source of truth for the version label shown under the brand.
  const VERSION = "v1.13";

  // Each entry: { href, label, icon, target?, section, tip }
  // `tip` becomes the title="" attribute (native tooltip).
  const NAV = [
    { section: "Main" },
    {
      href: "/dashboard",
      label: "Dashboard",
      tip: "Live KPIs, provider status and recent activity.",
      icon: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>',
    },
    {
      href: "/analytics",
      label: "Analytics",
      tip: "Filterable per-token, per-provider and per-model usage.",
      icon: '<path d="M3 3v18h18M7 16l4-4 4 4 5-7"/>',
    },
    {
      href: "/api-docs",
      label: "API Documentation",
      tip: "OpenAI-compatible endpoints, examples and SDK guides.",
      icon: '<path d="M9 12h6M9 16h6M9 8h6M5 3h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2z"/>',
    },
    {
      href: "/settings",
      label: "Settings",
      tip: "Manage providers, keys, gateway tokens and routing.",
      icon: '<path d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37a1.724 1.724 0 002.572-1.065z"/><circle cx="12" cy="12" r="3"/>',
    },
    {
      href: "/images",
      label: "Image Generation",
      tip: "Generate and browse images via OpenAI-compatible API.",
      icon: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
    },
    {
      href: "/playground",
      label: "Playground",
      tip: "Interactive chat / completions sandbox with full controls.",
      icon: '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>',
    },
    {
      href: "/logs",
      label: "Logs",
      tip: "Live request log with filters and export.",
      icon: '<path d="M4 6h16M4 10h16M4 14h10M4 18h6"/>',
    },
    { divider: true },
    { section: "Developer" },
    {
      href: "/docs",
      label: "Swagger UI",
      target: "_blank",
      tip: "Interactive OpenAPI explorer.",
      icon: '<path d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"/>',
    },
    {
      href: "/redoc",
      label: "ReDoc",
      target: "_blank",
      tip: "Read-only OpenAPI reference.",
      icon: '<path d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"/>',
    },
  ];

  const EXT_ICON = '<svg class="nav-item-ext" width="11" height="11" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6M15 3h6v6M10 14L21 3"/></svg>';

  function svg(icon) {
    return (
      '<svg width="16" height="16" fill="none" viewBox="0 0 24 24" ' +
      'stroke="currentColor" stroke-width="2">' + icon + "</svg>"
    );
  }

  function navItem(item, currentPath) {
    const isActive =
      !item.target &&
      (item.href === currentPath ||
        (item.href !== "/" && currentPath.startsWith(item.href + "/")));
    const cls = "nav-item" + (isActive ? " active" : "");
    const tgt = item.target ? ` target="${item.target}" rel="noopener"` : "";
    const tip = item.tip ? ` title="${item.tip.replace(/"/g, "&quot;")}"` : "";
    const ext = item.target ? EXT_ICON : "";
    return (
      `<a href="${item.href}" class="${cls}"${tgt}${tip}>` +
      svg(item.icon) +
      `<span>${item.label}</span>` +
      ext +
      "</a>"
    );
  }

  function render() {
    const root = document.getElementById("sidebar");
    if (!root) return;
    const path = window.location.pathname || "/";

    let html =
      '<div class="sidebar-brand">' +
      '<div class="sidebar-logo">A</div>' +
      '<span class="sidebar-brand-name">Arbiter</span>' +
      `<span class="sidebar-brand-ver">${VERSION}</span>` +
      "</div>" +
      '<nav class="sidebar-nav" aria-label="Primary">';

    for (const item of NAV) {
      if (item.divider) {
        html += '<div class="sidebar-divider"></div>';
      } else if (item.section) {
        html += `<span class="sidebar-section">${item.section}</span>`;
      } else {
        html += navItem(item, path);
      }
    }
    html +=
      "</nav>" +
      '<div class="sidebar-footer">' +
      '<button class="theme-btn" id="theme-btn" type="button" ' +
      'title="Toggle light / dark theme">' +
      '<span class="theme-icon">🌙</span>' +
      '<span class="theme-label">Dark mode</span>' +
      "</button>" +
      "</div>";

    root.innerHTML = html;
    root.classList.add("sidebar");

    // Wire up theme toggle once. Pages that still define their own
    // toggleTheme() global will be reused; otherwise fall back.
    const btn = document.getElementById("theme-btn");
    if (btn) {
      btn.addEventListener("click", () => {
        if (typeof window.toggleTheme === "function") {
          window.toggleTheme();
        } else {
          const cur = document.documentElement.dataset.theme || "dark";
          const nxt = cur === "dark" ? "light" : "dark";
          document.documentElement.dataset.theme = nxt;
          try { localStorage.setItem("arbiter-theme", nxt); } catch (e) {}
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
