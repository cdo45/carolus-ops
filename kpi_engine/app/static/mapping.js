"use strict";

// Reusable account-mapping module. The standalone /mapping page and the
// embedded wizard step both drive it through Mapping.load / Mapping.submit.

var Mapping = (function () {
  var S = { slug: null, root: null, categories: [], onMessage: null };

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function enc(s) { return encodeURIComponent(s); }
  function msg(kind, text) { if (S.onMessage) S.onMessage(kind, text); }

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, message: "The server returned an unreadable reply." };
      });
    });
  }

  function options(selected, placeholder) {
    var html = placeholder ? '<option value="">— choose a category —</option>' : "";
    S.categories.forEach(function (c) {
      var sel = c.code === selected ? " selected" : "";
      html += '<option value="' + c.code + '" title="' + esc(c.explanation) +
        '"' + sel + ">" + esc(c.label) + " (" + c.code + ")</option>";
    });
    return html;
  }

  function queueRow(r) {
    var sub = esc(r.full_path || r.qbo_name);
    if (r.qbo_type) sub += " · " + esc(r.qbo_type);
    return '<div class="qrow" data-id="' + r.id +
      '" data-unsure="0" data-ignore="0">' +
      '<div class="name">' + esc(r.qbo_name) +
      '<span class="sub">' + sub + "</span></div>" +
      '<select class="cat">' + options(r.category, true) + "</select>" +
      '<button class="linkish unsure-btn" type="button">I’m not sure</button>' +
      '<button class="linkish ignore-btn" type="button">Ignore</button>' +
      '<label class="inline"><input type="checkbox" class="remember"> ' +
      "remember</label></div>";
  }

  function ignoredGroup(rows) {
    var items = rows.map(function (r) {
      return "<li>" + esc(r.qbo_name) + "</li>";
    }).join("");
    return "<p class='note'>Inactive or deleted in QBO — left out of the " +
      "dashboard and proposal. No action needed.</p>" +
      "<details class='auto-cat'><summary>Show accounts</summary><ul>" +
      items + "</ul></details>";
  }

  function confirmRow(r) {
    return '<div class="crow" data-id="' + r.id + '" data-orig="' +
      esc(r.category) + '">' +
      '<span class="name">' + esc(r.qbo_name) + "</span>" +
      '<span class="cat">' + esc(r.label || r.category) + "</span>" +
      '<button class="linkish change-btn" type="button">change</button>' +
      '<select class="cat-select" hidden>' + options(r.category, false) +
      "</select>" +
      '<label class="inline"><input type="checkbox" class="remember"> ' +
      "remember</label></div>";
  }

  function autoGroup(rows) {
    var byCat = {};
    rows.forEach(function (r) {
      var k = r.label || r.category || "Unclassified";
      (byCat[k] = byCat[k] || []).push(r);
    });
    return Object.keys(byCat).sort().map(function (k) {
      // Each account is editable — a wrong high-confidence guess can be
      // moved to any category via its [change] control.
      var items = byCat[k].map(confirmRow).join("");
      return '<details class="auto-cat"><summary>' + esc(k) +
        ' <span class="count">(' + byCat[k].length + ")</span></summary>" +
        items + "</details>";
    }).join("");
  }

  function render(groups, counts) {
    var q = groups.queue, c = groups.confirm, a = groups.auto;
    var html = "";
    html += '<section class="group"><h2>Needs your input ' +
      '<span class="count">(' + counts.queue + ")</span></h2>" +
      (q.length ? q.map(queueRow).join("")
        : '<div class="empty">Nothing here — every account is recognized.</div>') +
      "</section>";
    html += '<section class="group"><h2>Best guesses — review ' +
      '<span class="count">(' + counts.confirm + ")</span></h2>" +
      (c.length ? c.map(confirmRow).join("")
        : '<div class="empty">No middling guesses.</div>') + "</section>";
    html += '<section class="group"><h2>Auto-classified ' +
      '<span class="count">(' + counts.auto + ")</span></h2>" +
      (a.length ? autoGroup(a)
        : '<div class="empty">Nothing auto-classified yet.</div>') + "</section>";
    var ignoredRows = groups.ignored || [];
    if (ignoredRows.length) {
      html += '<section class="group"><h2>Ignored ' +
        '<span class="count">(' + ignoredRows.length + ")</span></h2>" +
        ignoredGroup(ignoredRows) + "</section>";
    }
    S.root.innerHTML = html;
    wire();
  }

  function wire() {
    S.root.querySelectorAll(".unsure-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var row = btn.closest(".qrow");
        var on = row.dataset.unsure === "1";
        row.dataset.unsure = on ? "0" : "1";
        row.classList.toggle("unsure", !on);
        btn.textContent = on ? "I’m not sure" : "marked unsure";
      });
    });
    S.root.querySelectorAll(".change-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var sel = btn.closest(".crow").querySelector(".cat-select");
        sel.hidden = !sel.hidden;
      });
    });
    S.root.querySelectorAll(".ignore-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var row = btn.closest(".qrow");
        var on = row.dataset.ignore === "1";
        row.dataset.ignore = on ? "0" : "1";
        row.classList.toggle("ignored-row", !on);
        btn.textContent = on ? "Ignore" : "will ignore";
      });
    });
  }

  function collect() {
    var changes = [], unsure = [], remember = [], ignore = [];
    S.root.querySelectorAll(".qrow").forEach(function (row) {
      var id = parseInt(row.dataset.id, 10);
      if (row.dataset.ignore === "1") { ignore.push(id); return; }
      if (row.dataset.unsure === "1") { unsure.push(id); return; }
      var sel = row.querySelector(".cat");
      if (sel.value) {
        changes.push({ id: id, category: sel.value });
        if (row.querySelector(".remember").checked) {
          remember.push({ id: id, category: sel.value });
        }
      }
    });
    S.root.querySelectorAll(".crow").forEach(function (row) {
      var sel = row.querySelector(".cat-select");
      if (sel.value && sel.value !== row.dataset.orig) {
        var id = parseInt(row.dataset.id, 10);
        changes.push({ id: id, category: sel.value });
        if (row.querySelector(".remember").checked) {
          remember.push({ id: id, category: sel.value });
        }
      }
    });
    return { changes: changes, confirm_all: true, unsure: unsure,
             remember: remember, ignore: ignore };
  }

  function load(slug, root, opts) {
    S.slug = slug; S.root = root; opts = opts || {};
    S.onMessage = opts.onMessage || null;
    return api("/api/mapping?client=" + enc(slug)).then(function (d) {
      if (!d.ok) { msg("err", d.message); return null; }
      S.categories = d.categories || [];
      render(d.groups, d.counts);
      return d.counts;
    });
  }

  function submit(onDone) {
    var payload = collect();
    return api("/api/mapping?client=" + enc(S.slug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (!r.ok) { msg("err", r.message); return; }
      if (onDone) onDone(r);
    });
  }

  return { load: load, submit: submit, collect: collect };
})();

// Standalone page wiring (only on /mapping, which has #confirm-continue).
document.addEventListener("DOMContentLoaded", function () {
  var footer = document.getElementById("confirm-continue");
  var root = document.getElementById("mapping-root");
  if (!footer || !root) return;
  var slug = new URLSearchParams(location.search).get("client");
  function message(kind, text) {
    var box = document.getElementById("messages");
    var div = document.createElement("div");
    div.className = "msg " + kind;
    div.textContent = text;
    box.insertBefore(div, box.firstChild);
  }
  if (!slug) { message("err", "No client selected."); return; }

  function refreshCounts(counts) {
    if (!counts) { document.getElementById("footer-counts").textContent = ""; return; }
    var text = counts.queue + " need input · " + counts.confirm +
      " to review · " + counts.auto + " auto";
    if (counts.ignored) text += " · " + counts.ignored + " ignored";
    document.getElementById("footer-counts").textContent = text;
  }
  Mapping.load(slug, root, { onMessage: message }).then(refreshCounts);
  footer.addEventListener("click", function () {
    footer.disabled = true;
    Mapping.submit(function () { location.href = "/"; }).then(function () {
      footer.disabled = false;
    });
  });
});
