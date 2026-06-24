"use strict";

(function () {
  var slug = new URLSearchParams(location.search).get("client");
  var SCENARIOS = ["BASE", "STRETCH", "CRUNCH"];
  var SCN_FIELDS = [
    ["lag_shift_weeks", "Lag shift (wk)"],
    ["haircut_61_90", "61–90 haircut"],
    ["haircut_91", "91+ haircut"],
    ["discretionary", "Discretionary"],
  ];

  function $(id) { return document.getElementById(id); }

  function message(kind, text) {
    var box = $("messages");
    var div = document.createElement("div");
    div.className = "msg " + kind;
    div.textContent = text;
    box.insertBefore(div, box.firstChild);
    if (kind === "ok") setTimeout(function () { div.remove(); }, 6000);
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, message: "The server returned an unreadable reply." };
      });
    });
  }
  function postJSON(path, obj) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(obj)
    });
  }

  function buildScenarioGrid(scenarioCfg) {
    var grid = $("scenario-grid");
    grid.innerHTML = "";
    SCENARIOS.forEach(function (scn) {
      var box = document.createElement("div");
      box.className = "scn";
      var fields = SCN_FIELDS.map(function (f) {
        var key = f[0];
        var val = (scenarioCfg[scn] && scenarioCfg[scn][key] != null) ?
          scenarioCfg[scn][key] : "";
        return '<label>' + f[1] +
          '<input data-scn="' + scn + '" data-key="' + key +
          '" type="number" step="0.05" value="' + val +
          '" placeholder="default"></label>';
      }).join("");
      box.innerHTML = "<h4>" + scn + "</h4>" + fields;
      grid.appendChild(box);
    });
  }

  function collectScenario() {
    var out = {};
    document.querySelectorAll("#scenario-grid input").forEach(function (inp) {
      if (inp.value === "") return;
      var scn = inp.dataset.scn;
      out[scn] = out[scn] || {};
      out[scn][inp.dataset.key] = inp.value;
    });
    return out;
  }

  function loadConfig() {
    return api("/api/config?client=" + encodeURIComponent(slug))
      .then(function (r) {
        if (!r.ok) { message("err", r.message); return; }
        var c = r.config;
        $("client-name").textContent = c.display_name || slug;
        $("display-name").value = c.display_name || "";
        $("cash-floor").value = c.cash_floor != null ? c.cash_floor : "";
        $("archetype").value = c.archetype || "auto";
        $("manual-billing").value = c.manual_billing_schedule || "";
        buildScenarioGrid(c.scenario || {});
      });
  }

  function loadHistory() {
    api("/api/history?client=" + encodeURIComponent(slug)).then(function (r) {
      if (!r.ok) return;
      var ul = $("history");
      ul.innerHTML = r.uploads.length ? "" :
        '<li class="meta">No uploads yet.</li>';
      r.uploads.forEach(function (u) {
        var li = document.createElement("li");
        var sup = u.superseded_prior ? " · replaced earlier data" : "";
        li.innerHTML = "<strong>" + u.report_type + "</strong> " +
          (u.coverage || "") + " (" + u.row_count + " rows)" +
          '<div class="meta">' + (u.filename || "") + " · " +
          (u.uploaded_at || "") + sup + "</div>";
        ul.appendChild(li);
      });
    });
  }

  function loadAudit() {
    api("/api/audit?client=" + encodeURIComponent(slug) + "&limit=40")
      .then(function (r) {
        if (!r.ok) return;
        var ul = $("audit");
        ul.innerHTML = r.items.length ? "" :
          '<li class="meta">No changes recorded yet.</li>';
        r.items.forEach(function (it) {
          var li = document.createElement("li");
          li.textContent = it.text;
          ul.appendChild(li);
        });
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!slug) { message("err", "No client selected."); return; }
    $("mapping-link").href = "/mapping?client=" + encodeURIComponent(slug);

    $("save-name").addEventListener("click", function () {
      postJSON("/api/config?client=" + encodeURIComponent(slug),
        { display_name: $("display-name").value }).then(function (r) {
        if (!r.ok) { message("err", r.message); return; }
        message("ok", "Name saved.");
        $("client-name").textContent = r.config.display_name;
      });
    });

    $("save-assumptions").addEventListener("click", function () {
      var body = {
        cash_floor: $("cash-floor").value || "0",
        archetype: $("archetype").value,
        manual_billing_schedule: $("manual-billing").value,
        scenario: collectScenario(),
      };
      postJSON("/api/config?client=" + encodeURIComponent(slug), body)
        .then(function (r) {
          if (!r.ok) { message("err", r.message); return; }
          message("ok", "Assumptions saved.");
        });
    });

    $("gen-proposal").addEventListener("click", function () {
      var btn = $("gen-proposal");
      btn.disabled = true;
      postJSON("/api/proposal?client=" + encodeURIComponent(slug), {})
        .then(function (r) {
          btn.disabled = false;
          if (!r.ok) { message("err", r.message); return; }
          var panel = $("proposal-result");
          panel.hidden = false;
          panel.innerHTML =
            "<p>Review doc: <code>" + r.html_path + "</code></p>" +
            "<p>QBO import CSV: <code>" + r.csv_path + "</code></p>" +
            "<p>" + r.counts.renumber + " renumber · " + r.counts.merge +
            " merge · " + r.counts.deactivate + " deactivate</p>";
        });
    });

    loadConfig();
    loadHistory();
    loadAudit();
  });
})();
