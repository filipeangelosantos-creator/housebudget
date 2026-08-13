// Small enhancements only — every feature works without JS.
(function () {
  "use strict";

  // Auto-submit forms when a select marked data-autosubmit changes.
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (el.matches("select[data-autosubmit], input[data-autosubmit]") && el.form) {
      el.form.submit();
    }
  });

  // Confirmation prompts (delete buttons etc.)
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
    }
  });

  // Review queue: picking a category copies it to untouched rows from the
  // same merchant, and the save-all bar counts what's chosen.
  var reviewForm = document.getElementById("review-form");
  if (reviewForm) {
    var updateCount = function () {
      var out = document.getElementById("chosen-count");
      if (!out) return;
      var n = 0;
      reviewForm.querySelectorAll('select[name^="cat_"]').forEach(function (s) {
        if (s.value) n += 1;
      });
      out.textContent = String(n);
    };
    reviewForm.addEventListener("change", function (e) {
      var el = e.target;
      if (!el.matches('select[name^="cat_"]')) return;
      el.dataset.touched = "1";
      var merchant = el.getAttribute("data-merchant");
      if (merchant && el.value) {
        reviewForm.querySelectorAll(
          'select[data-merchant]:not([data-touched])').forEach(function (other) {
          if (other !== el && other.getAttribute("data-merchant") === merchant
              && !other.value) {
            other.value = el.value;
          }
        });
      }
      updateCount();
    });
    updateCount();
  }

  // Transaction list: tick rows to recategorize them together.
  var bulkForm = document.getElementById("bulk-form");
  if (bulkForm) {
    var bar = document.getElementById("bulk-bar");
    var countOut = document.getElementById("bulk-count");
    var allHint = document.getElementById("bulk-all-hint");
    var selectAll = document.getElementById("select-all");
    var refresh = function () {
      var checked = bulkForm.querySelectorAll(".txn-check:checked").length;
      if (countOut) countOut.textContent = String(checked);
      if (bar) bar.hidden = checked === 0;
      if (allHint) allHint.hidden = checked === 0;
      if (selectAll) {
        var boxes = bulkForm.querySelectorAll(".txn-check").length;
        selectAll.checked = checked > 0 && checked === boxes;
        selectAll.indeterminate = checked > 0 && checked < boxes;
      }
    };
    bulkForm.addEventListener("change", function (e) {
      if (e.target === selectAll) {
        bulkForm.querySelectorAll(".txn-check").forEach(function (box) {
          box.checked = selectAll.checked;
        });
      }
      if (e.target.classList.contains("txn-check") || e.target === selectAll) refresh();
    });
    bulkForm.addEventListener("submit", function (e) {
      var trigger = e.submitter;
      if (trigger && trigger.getAttribute("data-confirm-bulk")) {
        if (!window.confirm(trigger.getAttribute("data-confirm-bulk"))) e.preventDefault();
      }
    });
    refresh();
    window.__hbRefreshBulk = refresh;   // rows appended by infinite scroll
  }

  // Transaction list: load the next page as you reach the bottom, so a year of
  // statements is one list rather than six pages you have to page through.
  var pager = document.getElementById("txn-pager");
  var rowHost = document.getElementById("txn-rows");
  if (pager && rowHost && window.IntersectionObserver) {
    var label = document.getElementById("txn-pager-label");
    var pages = parseInt(pager.getAttribute("data-pages"), 10);
    var base = pager.getAttribute("data-base") || "/transactions";
    var next = parseInt(pager.getAttribute("data-next-page"), 10);
    var loading = false;
    pager.querySelectorAll("a").forEach(function (a) { a.remove(); });
    if (label) label.textContent = "Loading more…";
    var loadNext = function () {
      if (loading || next > pages) return;
      loading = true;
      var url = base + (base.indexOf("?") === -1 ? "?" : "&") +
        "rows_only=1&page=" + next;
      fetch(url)
        .then(function (r) { return r.text(); })
        .then(function (html) {
          rowHost.insertAdjacentHTML("beforeend", html);
          next += 1;
          loading = false;
          if (next > pages) {
            observer.disconnect();
            if (label) label.textContent = "That's everything.";
          } else if (label) {
            label.textContent = "Loading more…";
          }
          // Newly added rows count towards the selection bar.
          if (typeof window.__hbRefreshBulk === "function") window.__hbRefreshBulk();
        })
        .catch(function () {
          loading = false;
          if (label) label.textContent = "Couldn't load more — reload to retry.";
          observer.disconnect();
        });
    };
    var observer = new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting) loadNext();
    }, { rootMargin: "400px" });
    observer.observe(pager);
  }

  // Insights: open any figure into the transactions behind it. Every number
  // there is an aggregate, and one you can't open is a claim you have to take
  // on trust. One tap, not two — a double-click has no reliable touch
  // equivalent, and these are read on a phone.
  var drillables = document.querySelectorAll("[data-drill-kind]");
  if (drillables.length) {
    var closeDrill = function (host) {
      var panel = host.nextElementSibling;
      if (panel && panel.classList.contains("drill")) panel.remove();
      host.setAttribute("aria-expanded", "false");
    };
    var makePanel = function (host) {
      // A <div> after a <tr> gets hoisted out of the table by the parser, so a
      // row in a table needs a row of its own.
      if (host.tagName === "TR") {
        var tr = document.createElement("tr");
        tr.className = "drill";
        var td = document.createElement("td");
        td.colSpan = host.children.length || 2;
        tr.appendChild(td);
        return { node: tr, target: td };
      }
      var div = document.createElement("div");
      div.className = "drill";
      return { node: div, target: div };
    };
    drillables.forEach(function (host) {
      var open = function (e) {
        if (e.target.closest("a")) return;          // links inside still navigate
        e.preventDefault();
        if (host.getAttribute("aria-expanded") === "true") {
          closeDrill(host);
          return;
        }
        var made = makePanel(host);
        var panel = made.target;
        panel.innerHTML = '<p class="small muted drill-inner">Loading…</p>';
        host.insertAdjacentElement("afterend", made.node);
        host.setAttribute("aria-expanded", "true");
        var url = "/insights/drill?kind=" +
          encodeURIComponent(host.getAttribute("data-drill-kind")) +
          "&key=" + encodeURIComponent(host.getAttribute("data-drill-key") || "") +
          "&month=" + encodeURIComponent(host.getAttribute("data-drill-month") || "");
        fetch(url)
          .then(function (r) { return r.text(); })
          .then(function (html) { panel.innerHTML = html; })
          .catch(function () {
            panel.innerHTML = '<p class="small muted drill-inner">' +
              "Couldn't load those transactions.</p>";
          });
      };
      host.addEventListener("click", open);
      // role="button" on a div gets no free keyboard activation.
      host.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") open(e);
      });
    });
  }

  // Classification audit: only the merchants you actually changed get saved,
  // so say how many that is rather than implying the whole page is rewritten.
  var classifyForm = document.getElementById("classify-form");
  if (classifyForm) {
    var classifyBar = document.getElementById("classify-bar");
    var classifyCount = document.getElementById("classify-count");
    var countChanged = function () {
      var n = 0;
      classifyForm.querySelectorAll('select[name^="cat_"]').forEach(function (sel) {
        var was = classifyForm.querySelector('[name="was_' + sel.name.slice(4) + '"]');
        if (was && sel.value !== was.value) n++;
      });
      if (classifyCount) classifyCount.textContent = String(n);
      if (classifyBar) classifyBar.hidden = n === 0;
    };
    classifyForm.addEventListener("change", countChanged);
    countChanged();
  }

  // Rule patterns: say how many transactions the text you typed would catch,
  // so "too short" and "too broad" are visible before you commit to it.
  var patternInputs = document.querySelectorAll(".pattern-input[data-match-hint]");
  if (patternInputs.length) {
    var describe = function (input) {
      var hint = document.getElementById(input.getAttribute("data-match-hint"));
      if (!hint) return;
      var value = input.value.trim();
      if (!value) {
        hint.textContent = "Enter some text from the merchant name.";
        return;
      }
      fetch("/rules/match-count?pattern=" + encodeURIComponent(value))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (input.value.trim() !== value) return;   // typed on since
          var n = data.count, total = data.total;
          if (n === 0) {
            hint.textContent = "Matches nothing yet — it will apply to future imports.";
          } else if (total && n > total * 0.3) {
            hint.textContent = "Matches " + n + " of " + total +
              " transactions — probably too broad.";
          } else {
            hint.textContent = "Matches " + n + " transaction" + (n === 1 ? "" : "s") + ".";
          }
        })
        .catch(function () { /* hint is optional */ });
    };
    var timers = new WeakMap();
    patternInputs.forEach(function (input) {
      describe(input);
      input.addEventListener("input", function () {
        clearTimeout(timers.get(input));
        timers.set(input, setTimeout(function () { describe(input); }, 350));
      });
    });
  }

  // On the import form: show what was chosen.
  var file = document.querySelector('input[type="file"][data-show-name]');
  if (file) {
    file.addEventListener("change", function () {
      var out = document.getElementById("file-name-out");
      if (!out || !file.files.length) return;
      if (file.files.length === 1) {
        out.textContent = file.files[0].name;
      } else {
        var names = Array.prototype.map.call(file.files, function (f) {
          return f.name;
        });
        out.textContent = file.files.length + " files: " + names.join(", ");
      }
    });
  }

  // Split editor: keep a live total so you can see what's left to allocate.
  var splitForm = document.querySelector("form[data-split-total]");
  if (splitForm) {
    var target = Math.abs(parseInt(splitForm.getAttribute("data-split-total"), 10)) / 100;
    var sumOut = document.getElementById("split-sum");
    var remOut = document.getElementById("split-remainder");
    var summary = sumOut ? sumOut.closest(".split-summary") : null;
    var recalc = function () {
      var total = 0;
      splitForm.querySelectorAll(".split-amt").forEach(function (input) {
        var v = parseFloat(input.value.replace(/[^0-9.,-]/g, "").replace(",", "."));
        if (!isNaN(v)) total += Math.abs(v);
      });
      total = Math.round(total * 100) / 100;
      if (sumOut) sumOut.textContent = total.toFixed(2);
      var diff = Math.round((target - total) * 100) / 100;
      if (summary) {
        summary.classList.toggle("ok", diff === 0);
        summary.classList.toggle("off", diff !== 0);
      }
      if (remOut) {
        remOut.textContent = diff === 0 ? "Adds up exactly."
          : (diff > 0 ? diff.toFixed(2) + " left to allocate."
                      : Math.abs(diff).toFixed(2) + " too much.");
      }
    };
    splitForm.addEventListener("input", function (e) {
      if (e.target.classList.contains("split-amt")) recalc();
    });
    recalc();
  }

  // Import form: reveal new-account fields when "new" is selected.
  var acct = document.getElementById("account-select");
  if (acct) {
    var toggle = function () {
      var box = document.getElementById("new-account-fields");
      if (box) box.style.display = acct.value === "new" ? "" : "none";
    };
    acct.addEventListener("change", toggle);
    toggle();
  }
})();
