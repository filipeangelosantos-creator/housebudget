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
