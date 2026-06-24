"use strict";

// Plain vanilla controller. Status is fetched only after an action
// (client change, upload, run) — never on a continuous poll.

var state = { slug: null };

function $(id) { return document.getElementById(id); }

function message(kind, text) {
  var box = $("messages");
  var div = document.createElement("div");
  div.className = "msg " + kind;
  div.textContent = text;
  box.insertBefore(div, box.firstChild);
  if (kind === "ok" || kind === "info") {
    setTimeout(function () { div.remove(); }, 8000);
  }
}

function api(path, opts) {
  return fetch(path, opts).then(function (r) {
    return r.json().catch(function () {
      return { ok: false, message: "The server returned an unreadable reply." };
    });
  });
}

// ── clients ────────────────────────────────────────────────────────────────

function loadClients(selectSlug) {
  return api("/api/clients").then(function (data) {
    var sel = $("client-select");
    sel.innerHTML = "";
    (data.clients || []).forEach(function (c) {
      var opt = document.createElement("option");
      opt.value = c.slug;
      opt.textContent = c.name;
      sel.appendChild(opt);
    });
    if (selectSlug) sel.value = selectSlug;
    var settings = $("settings-link");
    if (sel.options.length) {
      state.slug = sel.value;
      $("run-btn").disabled = false;
      settings.hidden = false;
      settings.href = "/settings?client=" + encodeURIComponent(state.slug);
      loadStatus();
    } else {
      state.slug = null;
      $("run-btn").disabled = true;
      settings.hidden = true;
      $("buckets").innerHTML = "";
    }
  });
}

function createClient() {
  var name = $("new-client-name").value.trim();
  if (!name) { message("err", "Enter a client name."); return; }
  api("/api/clients", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name })
  }).then(function (data) {
    if (!data.ok) { message("err", data.message); return; }
    $("add-client-form").hidden = true;
    $("new-client-name").value = "";
    message("ok", "Created client " + data.client.name + ".");
    loadClients(data.client.slug);
  });
}

// ── status + buckets ─────────────────────────────────────────────────────────

function loadStatus() {
  if (!state.slug) return;
  api("/api/status?client=" + encodeURIComponent(state.slug)).then(function (s) {
    if (!s.ok) { message("err", s.message); return; }
    renderBuckets(s.buckets || []);
    renderQueue(s.queue_count || 0);
  });
}

function renderBuckets(buckets) {
  var grid = $("buckets");
  grid.innerHTML = "";
  buckets.forEach(function (b) {
    var card = document.createElement("div");
    card.className = "bucket" + (b.greyed ? " greyed" : "");
    var warn = b.warn ? ' <span class="warn">⚠ stale</span>' : "";
    card.innerHTML =
      "<h3>" + b.label + "</h3>" +
      '<div class="state">' + (b.loaded_label || "none") + warn + "</div>" +
      '<div class="drop">Drop a file or click to upload</div>';
    var input = document.createElement("input");
    input.type = "file";
    input.hidden = true;
    card.appendChild(input);
    var drop = card.querySelector(".drop");
    drop.addEventListener("click", function () { input.click(); });
    input.addEventListener("change", function () {
      if (input.files[0]) upload(input.files[0], b.key);
    });
    attachDrag(card, function (file) { upload(file, b.key); });
    grid.appendChild(card);
  });
}

function renderQueue(count) {
  var banner = $("queue-banner");
  if (count > 0) {
    banner.hidden = false;
    banner.innerHTML = count +
      " account(s) need a category → <a href='/mapping'>Review</a>";
  } else {
    banner.hidden = true;
  }
}

// ── uploads ──────────────────────────────────────────────────────────────────

function upload(file, bucketKey) {
  if (!state.slug) { message("err", "Pick a client first."); return; }
  var fd = new FormData();
  if (bucketKey) fd.append("bucket", bucketKey);
  fd.append("file", file, file.name);
  message("info", "Uploading " + file.name + "…");
  api("/api/upload?client=" + encodeURIComponent(state.slug),
      { method: "POST", body: fd }).then(function (r) {
    if (!r.ok) { message("err", r.message); return; }
    var when = r.as_of ? ("as of " + r.as_of) :
      (r.period ? (r.period.start + " – " + r.period.end) : "");
    message("ok", r.friendly + " imported (" + r.row_count + " rows" +
      (when ? ", " + when : "") + ").");
    if (r.misroute && r.misroute_message) message("info", r.misroute_message);
    (r.warnings || []).forEach(function (w) { message("info", w); });
    loadStatus();
  });
}

function attachDrag(el, onFile) {
  el.addEventListener("dragover", function (e) {
    e.preventDefault(); el.classList.add("drag");
  });
  el.addEventListener("dragleave", function () { el.classList.remove("drag"); });
  el.addEventListener("drop", function (e) {
    e.preventDefault(); el.classList.remove("drag");
    var f = e.dataTransfer.files[0];
    if (f) onFile(f);
  });
}

// ── run ──────────────────────────────────────────────────────────────────────

function run() {
  if (!state.slug) return;
  $("run-btn").disabled = true;
  message("info", "Updating dashboard…");
  api("/api/run?client=" + encodeURIComponent(state.slug),
      { method: "POST" }).then(function (r) {
    $("run-btn").disabled = false;
    if (!r.ok) { message("err", r.message); return; }
    renderResult(r);
    loadStatus();
  });
}

function money(v) {
  return (v === null || v === undefined) ? "—" :
    "$" + Math.round(v).toLocaleString();
}

function renderResult(r) {
  var panel = $("result");
  panel.hidden = false;
  var h = r.headline || {};
  var weeks = (h.weeks_of_cash === null || h.weeks_of_cash === undefined) ?
    "—" : h.weeks_of_cash.toFixed(1) + " wk";
  var breach = (h.breach_weeks && h.breach_weeks.length) ?
    "<span class='breach'>weeks " + h.breach_weeks.join(", ") + "</span>" :
    "none";
  var changes = r.changes ? "<p>" + r.changes + "</p>" : "";
  panel.innerHTML =
    "<h2>Dashboard updated</h2>" +
    '<div class="headline">' +
    '<div><div class="num">' + money(h.cash) + '</div>' +
    '<div class="lbl">Cash on hand</div></div>' +
    '<div><div class="num">' + weeks + '</div>' +
    '<div class="lbl">Weeks of cash</div></div>' +
    '<div><div class="num">' + breach + '</div>' +
    '<div class="lbl">Floor breaches</div></div>' +
    "</div>" + changes +
    '<div class="pathline"><a id="dash-link">Open dashboard</a>' +
    "<code id='dash-path'></code>" +
    '<button id="copy-path" type="button">Copy path</button></div>';
  var path = r.dashboard_path;
  $("dash-path").textContent = path;
  // Open over the app's own http server — browsers block file:// from a page.
  $("dash-link").setAttribute(
    "href", "/dashboard?client=" + encodeURIComponent(state.slug));
  $("dash-link").setAttribute("target", "_blank");
  $("copy-path").addEventListener("click", function () {
    navigator.clipboard.writeText(path).then(function () {
      message("ok", "Path copied.");
    }, function () { message("info", "Copy failed; select the path manually."); });
  });
}

// ── wiring ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", function () {
  $("client-select").addEventListener("change", function (e) {
    state.slug = e.target.value;
    var settings = $("settings-link");
    settings.hidden = false;
    settings.href = "/settings?client=" + encodeURIComponent(state.slug);
    loadStatus();
  });
  $("add-client").addEventListener("click", function () {
    // New clients go through the onboarding wizard.
    location.href = "/wizard";
  });
  $("cancel-client").addEventListener("click", function () {
    $("add-client-form").hidden = true;
  });
  $("create-client").addEventListener("click", createClient);
  $("run-btn").addEventListener("click", run);
  attachDrag($("dropzone"), function (file) { upload(file, null); });
  loadClients();
});
