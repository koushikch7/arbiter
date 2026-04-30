/* ─────────────────────────────────────────────────────────────────────────
 *  Arbiter — UI helpers
 *
 *    1. Sortable tables  — add `data-sortable` to any <table>; the helper
 *       reads `<th data-sort-key="<dataAttr>" data-sort-type="num|date|str">`
 *       and rewires header clicks to sort the rows in place.  Add a
 *       `data-sort` attribute on each <td> with the comparable value when
 *       the visible text is formatted (e.g. a date string).
 *
 *    2. Tooltips         — `data-tip="..."` on any element shows a polished
 *       tooltip on hover/focus (in addition to the native title="").  Use
 *       `data-tip-pos="top|bottom|left|right"` to control placement.
 *
 *  No external deps; ~3 KB minified.  Loaded once via /static/components/ui.js.
 * ──────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  // ─── 1. Sortable tables ─────────────────────────────────────────────
  function comparableValue(td, type) {
    if (!td) return type === "num" ? 0 : "";
    let v = td.dataset.sort;
    if (v === undefined) v = (td.textContent || "").trim();
    if (type === "num") {
      const n = parseFloat(String(v).replace(/[^0-9.\-]+/g, ""));
      return Number.isFinite(n) ? n : -Infinity;
    }
    if (type === "date") {
      const t = Date.parse(v);
      return Number.isFinite(t) ? t : 0;
    }
    return String(v).toLowerCase();
  }

  function sortTable(table, colIdx, type, asc) {
    const tbody = table.tBodies[0];
    if (!tbody) return;
    const rows = Array.from(tbody.rows);
    rows.sort((a, b) => {
      const av = comparableValue(a.cells[colIdx], type);
      const bv = comparableValue(b.cells[colIdx], type);
      if (av < bv) return asc ? -1 : 1;
      if (av > bv) return asc ? 1 : -1;
      return 0;
    });
    const frag = document.createDocumentFragment();
    for (const r of rows) frag.appendChild(r);
    tbody.appendChild(frag);
  }

  function wireTable(table) {
    if (table.dataset.sortableWired === "1") return;
    table.dataset.sortableWired = "1";
    const headers = table.tHead ? Array.from(table.tHead.rows[0].cells) : [];
    headers.forEach((th, idx) => {
      const type = th.dataset.sortType || "str";
      if (th.dataset.sortable === "false") return;
      th.classList.add("th-sortable");
      th.setAttribute("role", "button");
      th.setAttribute("tabindex", "0");
      th.title = th.title || "Click to sort";
      const arrow = document.createElement("span");
      arrow.className = "th-sort-arrow";
      arrow.textContent = " ⇅";
      th.appendChild(arrow);
      const handler = () => {
        const cur = th.dataset.sortDir || "";
        const nxt = cur === "asc" ? "desc" : "asc";
        // Clear other headers
        headers.forEach((other) => {
          if (other !== th) {
            other.dataset.sortDir = "";
            const a = other.querySelector(".th-sort-arrow");
            if (a) a.textContent = " ⇅";
          }
        });
        th.dataset.sortDir = nxt;
        const a = th.querySelector(".th-sort-arrow");
        if (a) a.textContent = nxt === "asc" ? " ↑" : " ↓";
        sortTable(table, idx, type, nxt === "asc");
      };
      th.addEventListener("click", handler);
      th.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handler();
        }
      });
    });
  }

  function rescanTables() {
    document.querySelectorAll("table[data-sortable]").forEach(wireTable);
  }

  // Re-wire when tbodies are replaced by AJAX renders.
  const observer = new MutationObserver((muts) => {
    let need = false;
    for (const m of muts) {
      if (m.target && m.target.closest && m.target.closest("table[data-sortable]")) {
        need = true;
        break;
      }
    }
    if (need) rescanTables();
  });

  // ─── 2. Tooltips ────────────────────────────────────────────────────
  let tipEl = null;
  function ensureTipEl() {
    if (tipEl) return tipEl;
    tipEl = document.createElement("div");
    tipEl.className = "ui-tooltip";
    tipEl.setAttribute("role", "tooltip");
    document.body.appendChild(tipEl);
    return tipEl;
  }

  function showTip(target) {
    const text = target.dataset.tip;
    if (!text) return;
    const tip = ensureTipEl();
    tip.textContent = text;
    tip.style.opacity = "0";
    tip.style.display = "block";
    const r = target.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    const pos = target.dataset.tipPos || "top";
    let top, left;
    switch (pos) {
      case "bottom":
        top = r.bottom + 8;
        left = r.left + r.width / 2 - tr.width / 2;
        break;
      case "left":
        top = r.top + r.height / 2 - tr.height / 2;
        left = r.left - tr.width - 8;
        break;
      case "right":
        top = r.top + r.height / 2 - tr.height / 2;
        left = r.right + 8;
        break;
      default:
        top = r.top - tr.height - 8;
        left = r.left + r.width / 2 - tr.width / 2;
    }
    // Clamp to viewport
    left = Math.max(8, Math.min(left, window.innerWidth - tr.width - 8));
    top = Math.max(8, top);
    tip.style.top = top + window.scrollY + "px";
    tip.style.left = left + window.scrollX + "px";
    tip.dataset.pos = pos;
    requestAnimationFrame(() => { tip.style.opacity = "1"; });
  }

  function hideTip() {
    if (!tipEl) return;
    tipEl.style.opacity = "0";
    setTimeout(() => {
      if (tipEl && tipEl.style.opacity === "0") tipEl.style.display = "none";
    }, 150);
  }

  function bindTooltips() {
    document.addEventListener("mouseover", (e) => {
      const t = e.target.closest && e.target.closest("[data-tip]");
      if (t) showTip(t);
    });
    document.addEventListener("mouseout", (e) => {
      const t = e.target.closest && e.target.closest("[data-tip]");
      if (t) hideTip();
    });
    document.addEventListener("focusin", (e) => {
      const t = e.target.closest && e.target.closest("[data-tip]");
      if (t) showTip(t);
    });
    document.addEventListener("focusout", () => hideTip());
  }

  function init() {
    rescanTables();
    bindTooltips();
    observer.observe(document.body, { subtree: true, childList: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Expose for pages that build tables dynamically and want to re-scan.
  window.ArbiterUI = { rescanTables, sortTable };
})();
