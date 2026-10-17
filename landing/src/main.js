/* Small vanilla JS: config injection, theme toggle, mobile nav, pricing render.
   Every page works without it; this only upgrades. Loaded after config.js and pricing.js. */
(function () {
  "use strict";

  var C = window.SITE_CONFIG || {};
  var P = window.PRICING || null;
  var doc = document;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ---------- 1. Config injection ---------- */
  function applyConfig() {
    doc.querySelectorAll("[data-cfg]").forEach(function (el) {
      var v = C[el.getAttribute("data-cfg")];
      if (v != null) el.textContent = v;
    });
    doc.querySelectorAll("[data-cfg-href]").forEach(function (el) {
      var key = el.getAttribute("data-cfg-href");
      var v = C[key];
      if (v == null) return;
      el.setAttribute("href", /Email$/.test(key) ? "mailto:" + v : v);
    });
    if (C.brand && C.brand !== "Deployra") {
      doc.title = doc.title.replace(/Deployra/g, C.brand);
      doc.querySelectorAll('meta[name="description"], meta[property^="og:"], meta[name^="twitter:"]').forEach(function (m) {
        var c = m.getAttribute("content");
        if (c) m.setAttribute("content", c.replace(/Deployra/g, C.brand));
      });
    }
  }

  /* ---------- 2. Theme toggle ---------- */
  function currentTheme() {
    var t = doc.documentElement.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  function syncThemeButtons() {
    var t = currentTheme();
    doc.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.setAttribute("aria-pressed", t === "light" ? "true" : "false");
      b.setAttribute("aria-label", t === "light" ? "Light theme on. Switch to dark theme" : "Dark theme on. Switch to light theme");
    });
  }

  function initTheme() {
    doc.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.addEventListener("click", function () {
        var next = currentTheme() === "light" ? "dark" : "light";
        doc.documentElement.setAttribute("data-theme", next);
        try { localStorage.setItem("theme", next); } catch (e) { /* ignore */ }
        syncThemeButtons();
      });
    });
    syncThemeButtons();
  }

  /* ---------- 3. Mobile nav ---------- */
  function initNav() {
    var btn = doc.querySelector("[data-nav-toggle]");
    var nav = doc.getElementById("site-nav");
    if (!btn || !nav) return;
    function set(open) {
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      nav.classList.toggle("is-open", open);
    }
    btn.addEventListener("click", function () { set(btn.getAttribute("aria-expanded") !== "true"); });
    doc.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && btn.getAttribute("aria-expanded") === "true") { set(false); btn.focus(); }
    });
    nav.addEventListener("click", function (e) { if (e.target.closest("a")) set(false); });
  }

  /* ---------- 4. Pricing render ---------- */
  var billing = "monthly";

  function money(n) { return "$" + (Math.round(n * 100) % 100 === 0 ? Math.round(n) : n.toFixed(2)); }
  function amount(monthly) {
    return billing === "yearly" ? monthly * (12 - P.yearlyMonthsFree) : monthly;
  }
  function unit(monthly) {
    if (monthly === 0) return "forever";
    return billing === "yearly" ? "per year" : "per month";
  }

  var CHECK = '<svg class="mark mark--yes" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><span class="sr-only">Included</span>';
  var DASH = '<span class="mark mark--no" aria-hidden="true">&ndash;</span><span class="sr-only">Not included</span>';

  function renderPlans(el) {
    var compact = el.hasAttribute("data-compact");
    el.innerHTML = P.plans.map(function (p) {
      var feats = compact ? p.features.slice(0, 3) : p.features;
      var note = p.monthly === 0 ? "No card required"
        : billing === "yearly" ? "Billed yearly, " + P.yearlyMonthsFree + " months free"
        : "Billed monthly";
      return '<article class="plan' + (p.featured ? " plan--featured" : "") + '">' +
        '<h3 class="plan__name">' + esc(p.name) + (p.featured ? ' <span class="badge badge--accent">Most used</span>' : "") + "</h3>" +
        '<p class="plan__price"><span class="plan__amount">' + money(amount(p.monthly)) + '</span> <span class="plan__unit">' + unit(p.monthly) + "</span></p>" +
        '<p class="plan__note">' + note + "</p>" +
        '<p class="plan__blurb">' + esc(p.blurb) + "</p>" +
        '<ul class="checklist">' + feats.map(function (f) { return "<li>" + esc(f) + "</li>"; }).join("") + "</ul>" +
        '<a class="btn ' + (p.featured ? "btn--primary" : "btn--secondary") + ' btn--block" href="' + esc(C.signupUrl || "#") + '" data-cfg-href="signupUrl">' + esc(p.cta) + "</a>" +
        "</article>";
    }).join("");
  }

  function table(headers, rows, label) {
    return '<table class="table"><caption class="sr-only">' + esc(label) + "</caption><thead><tr>" +
      headers.map(function (h, i) { return '<th scope="col"' + (i === headers.length - 1 ? ' class="num"' : "") + ">" + esc(h) + "</th>"; }).join("") +
      "</tr></thead><tbody>" + rows.join("") + "</tbody></table>";
  }

  function renderCompute(el) {
    el.innerHTML = table(["Instance", "CPU", "Memory", "Notes", "Price"], P.compute.map(function (r) {
      return '<tr><th scope="row">' + esc(r.name) + "</th><td>" + esc(r.cpu) + "</td><td>" + esc(r.ram) + "</td><td>" + esc(r.note || "") +
        '</td><td class="num">' + money(r.monthly) + ' <span class="unit">/ month</span></td></tr>';
    }), "Compute instances per service");
  }

  function renderDatabases(el) {
    el.innerHTML = table(["Database", "Storage", "Backups", "Price"], P.databases.map(function (r) {
      return '<tr><th scope="row">' + esc(r.name) + "</th><td>" + esc(r.storage) + "</td><td>" + esc(r.backups) +
        '</td><td class="num">' + money(r.monthly) + ' <span class="unit">/ month</span></td></tr>';
    }), "Managed databases (PostgreSQL, MySQL, Redis)");
  }

  function renderVps(el) {
    el.innerHTML = table(["Plan", "CPU", "Memory", "Disk", "Price"], P.vps.map(function (r) {
      var yearly = billing === "yearly";
      return '<tr><th scope="row">' + esc(r.name) + "</th><td>" + esc(r.cpu) + "</td><td>" + esc(r.ram) + "</td><td>" + esc(r.disk) +
        '</td><td class="num">' + money(amount(r.monthly)) + ' <span class="unit">/ ' + (yearly ? "year" : "month") + "</span></td></tr>";
    }), "AI-managed VPS plans");
  }

  function cell(v) {
    if (v === true) return CHECK;
    if (v === false) return DASH;
    return esc(v);
  }

  function renderMatrix(el) {
    var head = '<th scope="col">Feature</th>' + P.matrixPlans.map(function (n) { return '<th scope="col" class="center">' + esc(n) + "</th>"; }).join("");
    var cols = P.matrixPlans.length + 1;
    var body = P.matrix.map(function (g) {
      return '<tr class="matrix__group"><th scope="colgroup" colspan="' + cols + '">' + esc(g.group) + "</th></tr>" +
        g.rows.map(function (r) {
          return '<tr><th scope="row">' + esc(r.label) + "</th>" + r.values.map(function (v) { return '<td class="center">' + cell(v) + "</td>"; }).join("") + "</tr>";
        }).join("");
    }).join("");
    el.innerHTML = '<table class="table table--matrix"><caption class="sr-only">Plan feature comparison</caption><thead><tr>' + head + "</tr></thead><tbody>" + body + "</tbody></table>";
  }

  var RENDERERS = { plans: renderPlans, compute: renderCompute, databases: renderDatabases, vps: renderVps, matrix: renderMatrix };

  function renderAll() {
    if (!P) return;
    doc.querySelectorAll("[data-render]").forEach(function (el) {
      var fn = RENDERERS[el.getAttribute("data-render")];
      if (fn) fn(el);
    });
    applyConfig();
  }

  function initBilling() {
    var btns = doc.querySelectorAll("[data-billing]");
    if (!btns.length) return;
    btns.forEach(function (b) {
      b.addEventListener("click", function () {
        billing = b.getAttribute("data-billing");
        btns.forEach(function (o) { o.setAttribute("aria-pressed", o === b ? "true" : "false"); });
        renderAll();
        doc.querySelectorAll("[data-billing-note]").forEach(function (n) {
          n.textContent = billing === "yearly"
            ? "Yearly billing: " + P.yearlyMonthsFree + " months free on workspace plans and VPS."
            : "Monthly billing. Switch to yearly and get " + P.yearlyMonthsFree + " months free.";
        });
      });
    });
  }

  /* ---------- boot ---------- */
  function boot() {
    initTheme();
    initNav();
    initBilling();
    renderAll();
    applyConfig();
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
