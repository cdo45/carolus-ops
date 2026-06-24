"use strict";

// First-run wizard. Reuses the Mapping module (inlined alongside) for step 3.
// Steps: 1 name (skipped if ?client= present), 2 upload, 3 mapping,
// 4 cash floor, 5 generate.

(function () {
  var W = { slug: null, step: 1 };
  var STEPS = ["Name", "Upload", "Categories", "Cash floor", "Generate"];

  function $(id) { return document.getElementById(id); }

  function message(kind, text) {
    var box = $("messages");
    var div = document.createElement("div");
    div.className = "msg " + kind;
    div.textContent = text;
    box.insertBefore(div, box.firstChild);
    if (kind === "ok" || kind === "info") {
      setTimeout(function () { div.remove(); }, 7000);
    }
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, message: "The server returned an unreadable reply." };
      });
    });
  }

  function renderStepper() {
    var ol = $("stepper");
    ol.innerHTML = "";
    STEPS.forEach(function (label, i) {
      var n = i + 1;
      var li = document.createElement("li");
      li.textContent = n + ". " + label;
      if (n === W.step) li.className = "active";
      else if (n < W.step) li.className = "done";
      ol.appendChild(li);
    });
  }

  function show(step) {
    W.step = step;
    document.querySelectorAll(".step").forEach(function (sec) {
      sec.hidden = parseInt(sec.dataset.step, 10) !== step;
    });
    renderStepper();
    if (step === 3) {
      Mapping.load(W.slug, $("wizard-mapping-root"), { onMessage: message });
    }
  }

  // -- step 1: name --
  function createClient() {
    var name = $("wiz-name").value.trim();
    if (!name) { message("err", "Enter a client name."); return; }
    api("/api/clients", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name })
    }).then(function (r) {
      if (!r.ok) { message("err", r.message); return; }
      W.slug = r.client.slug;
      show(2);
    });
  }

  // -- step 2: uploads --
  function uploadFiles(files) {
    var list = Array.prototype.slice.call(files);
    var receipts = $("wiz-receipts");
    list.forEach(function (file) {
      var li = document.createElement("li");
      li.textContent = "Uploading " + file.name + "…";
      receipts.appendChild(li);
      var fd = new FormData();
      fd.append("file", file, file.name);
      api("/api/upload?client=" + encodeURIComponent(W.slug),
          { method: "POST", body: fd }).then(function (r) {
        if (!r.ok) {
          li.className = "err";
          li.textContent = file.name + " — " + r.message;
          return;
        }
        var when = r.as_of ? ("as of " + r.as_of) :
          (r.period ? (r.period.start + " – " + r.period.end) : "");
        li.innerHTML = "<strong>" + file.name + "</strong> → " + r.friendly +
          " (" + r.row_count + " rows" + (when ? ", " + when : "") + ")";
        var notes = [];
        if (r.misroute && r.misroute_message) notes.push(r.misroute_message);
        (r.warnings || []).forEach(function (w) { notes.push(w); });
        if (notes.length) {
          li.innerHTML += '<div class="note">' + notes.join("<br>") + "</div>";
        }
      });
    });
  }

  function attachDrag(el, onFiles) {
    el.addEventListener("dragover", function (e) {
      e.preventDefault(); el.classList.add("drag");
    });
    el.addEventListener("dragleave", function () { el.classList.remove("drag"); });
    el.addEventListener("drop", function (e) {
      e.preventDefault(); el.classList.remove("drag");
      if (e.dataTransfer.files.length) onFiles(e.dataTransfer.files);
    });
  }

  // -- step 4: cash floor --
  function saveFloor() {
    var value = $("wiz-floor").value || "0";
    api("/api/config?client=" + encodeURIComponent(W.slug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cash_floor: value })
    }).then(function (r) {
      if (!r.ok) { message("err", r.message); return; }
      show(5);
    });
  }

  // -- step 5: generate --
  function money(v) {
    return (v === null || v === undefined) ? "—" :
      "$" + Math.round(v).toLocaleString();
  }
  function generate() {
    var btn = $("wiz-generate");
    btn.disabled = true;
    message("info", "Generating the first dashboard…");
    api("/api/run?client=" + encodeURIComponent(W.slug),
        { method: "POST" }).then(function (r) {
      btn.disabled = false;
      if (!r.ok) { message("err", r.message); return; }
      var h = r.headline || {};
      var weeks = (h.weeks_of_cash == null) ? "—" :
        h.weeks_of_cash.toFixed(1) + " wk";
      var breach = (h.breach_weeks && h.breach_weeks.length) ?
        "weeks " + h.breach_weeks.join(", ") : "none";
      var panel = $("wiz-result");
      panel.hidden = false;
      panel.innerHTML =
        '<div class="headline">' +
        '<div><div class="num">' + money(h.cash) + '</div>' +
        '<div class="lbl">Cash on hand</div></div>' +
        '<div><div class="num">' + weeks + '</div>' +
        '<div class="lbl">Weeks of cash</div></div>' +
        '<div><div class="num">' + breach + '</div>' +
        '<div class="lbl">Floor breaches</div></div></div>' +
        '<p><a href="/dashboard?client=' + encodeURIComponent(W.slug) +
        '" target="_blank"><strong>Open dashboard</strong></a></p>' +
        '<div class="pathline">Saved to <code>' +
        r.dashboard_path + "</code></div>" +
        '<p><a href="/">← Back to home</a></p>';
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    W.slug = new URLSearchParams(location.search).get("client");
    $("wiz-create").addEventListener("click", createClient);
    $("wiz-name").addEventListener("keydown", function (e) {
      if (e.key === "Enter") createClient();
    });
    $("wiz-pick").addEventListener("click", function () {
      $("wiz-files").click();
    });
    $("wiz-files").addEventListener("change", function (e) {
      if (e.target.files.length) uploadFiles(e.target.files);
    });
    attachDrag($("wiz-drop"), uploadFiles);
    $("wiz-to-3").addEventListener("click", function () { show(3); });
    $("wiz-to-4").addEventListener("click", function () {
      Mapping.submit(function () { show(4); });
    });
    $("wiz-to-5").addEventListener("click", saveFloor);
    $("wiz-generate").addEventListener("click", generate);

    show(W.slug ? 2 : 1);
  });
})();
