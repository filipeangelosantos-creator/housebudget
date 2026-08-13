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
    var pageMonthEl = document.getElementById("page-month");
    var pageMonth = pageMonthEl ? pageMonthEl.getAttribute("data-month") : "";
    var openDrills = [];       // {anchor, host, node}

    // Where the panel goes. A bar lives inside an <svg>, which can't hold
    // HTML, so its panel goes after the whole chart — which means every bar in
    // one chart shares an anchor, and opening one has to close the last.
    var anchorFor = function (host) {
      return host.ownerSVGElement || host;
    };
    var closeAt = function (anchor) {
      openDrills = openDrills.filter(function (p) {
        if (p.anchor !== anchor) return true;
        p.node.remove();
        p.host.setAttribute("aria-expanded", "false");
        return false;
      });
    };
    var makePanel = function (anchor) {
      // A <div> after a <tr> gets hoisted out of the table by the parser, so a
      // row in a table needs a row of its own.
      if (anchor.tagName === "TR") {
        var tr = document.createElement("tr");
        tr.className = "drill";
        var td = document.createElement("td");
        td.colSpan = anchor.children.length || 2;
        tr.appendChild(td);
        return { node: tr, target: td };
      }
      var div = document.createElement("div");
      div.className = "drill";
      return { node: div, target: div };
    };
    drillables.forEach(function (host) {
      var open = function (e) {
        if (e.target.closest && e.target.closest("a")) return;   // links navigate
        e.preventDefault();
        var anchor = anchorFor(host);
        var wasOpen = host.getAttribute("aria-expanded") === "true";
        closeAt(anchor);
        if (wasOpen) return;                        // tapping it again closes it
        var made = makePanel(anchor);
        var panel = made.target;
        panel.innerHTML = '<p class="small muted drill-inner">Loading…</p>';
        anchor.insertAdjacentElement("afterend", made.node);
        host.setAttribute("aria-expanded", "true");
        openDrills.push({ anchor: anchor, host: host, node: made.node });
        var url = "/insights/drill?kind=" +
          encodeURIComponent(host.getAttribute("data-drill-kind")) +
          "&key=" + encodeURIComponent(host.getAttribute("data-drill-key") || "") +
          "&month=" + encodeURIComponent(host.getAttribute("data-drill-month") || "") +
          "&day=" + encodeURIComponent(host.getAttribute("data-drill-day") || "0") +
          "&page_month=" + encodeURIComponent(pageMonth);
        fetch(url)
          .then(function (r) { return r.text(); })
          .then(function (html) { panel.innerHTML = html; })
          .catch(function () {
            panel.innerHTML = '<p class="small muted drill-inner">' +
              "Couldn't load those transactions.</p>";
          });
      };
      host.addEventListener("click", open);
      // role="button" on a div (or an SVG bar) gets no free keyboard activation.
      host.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") open(e);
      });
    });

    // Re-filing from inside a panel. The select is moved into the row being
    // edited rather than cloned per row — sixty selects is thousands of
    // options in something opened by one tap.
    document.addEventListener("click", function (e) {
      var chip = e.target.closest(".cat-chip");
      if (!chip) return;
      var panel = chip.closest(".drill-inner");
      var holder = panel.querySelector(".drill-cat-holder");
      var select = holder.querySelector("select");
      if (select.__chip === chip) return;           // already editing this row
      if (select.__chip) select.__chip.hidden = false;
      select.__chip = chip;
      select.value = chip.getAttribute("data-cat") || "";
      chip.hidden = true;
      chip.insertAdjacentElement("afterend", select);
      select.focus();
    });
    document.addEventListener("change", function (e) {
      var select = e.target;
      var chip = select.__chip;         // only set while it sits in a row
      if (!chip) return;
      var panel = select.closest(".drill-inner");
      var body = new URLSearchParams();
      body.set("csrf", panel.getAttribute("data-csrf") || "");
      body.set("txn_id", chip.getAttribute("data-txn"));
      body.set("category_id", select.value);
      select.disabled = true;
      fetch("/insights/drill/categorize", { method: "POST", body: body })
        .then(function (r) {
          if (!r.ok) throw new Error("failed");
          return r.json();
        })
        .then(function (data) {
          chip.textContent = data.category || "uncategorized";
          chip.setAttribute("data-cat", select.value);
          chip.classList.add("changed");
          // The figures above were computed before this change, so say so
          // rather than leaving a total that no longer matches its rows.
          if (!panel.querySelector(".drill-stale")) {
            var note = document.createElement("p");
            note.className = "small muted drill-stale";
            note.innerHTML = 'Saved. The totals and charts still show the old ' +
              'figures — <a href="">reload</a> to bring them up to date.';
            note.querySelector("a").href = location.href;
            panel.appendChild(note);
          }
        })
        .catch(function () {
          chip.classList.add("failed");
          chip.textContent = "couldn't save — open it instead";
        })
        .then(function () {
          select.disabled = false;
          select.__chip = null;
          chip.hidden = false;
          panel.querySelector(".drill-cat-holder").appendChild(select);
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

  // Budget editor: keep "does this fit inside my income?" answered while you
  // type, not only after saving. The wording matches budgets.html, which is
  // what renders without JS.
  var balance = document.getElementById("balance");
  if (balance) {
    var symbol = balance.getAttribute("data-currency") || "";
    var expected = parseInt(balance.getAttribute("data-expected"), 10) || 0;
    var cents = function (text) {
      var t = String(text).replace(/[^\d.,-]/g, "");
      if (!t) return 0;
      var dot = t.lastIndexOf("."), comma = t.lastIndexOf(",");
      var dec = Math.max(dot, comma);
      // Whichever separator comes last is the decimal one; a group of three
      // digits after it means it was a thousands separator after all.
      if (dec > -1 && t.length - dec - 1 === 3) dec = -1;
      var whole = dec > -1 ? t.slice(0, dec) : t;
      var frac = dec > -1 ? t.slice(dec + 1) : "";
      var n = parseInt(whole.replace(/[^\d-]/g, ""), 10) || 0;
      var f = parseInt((frac + "00").slice(0, 2), 10) || 0;
      return n * 100 + (n < 0 ? -f : f);
    };
    var show = function (v) {
      return symbol + (Math.abs(v) / 100).toFixed(2)
        .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    };
    var sumOf = function (kind) {
      var total = 0;
      document.querySelectorAll('input[data-kind="' + kind + '"]').forEach(
        function (i) { total += cents(i.value); });
      return total;
    };
    var verdict = document.getElementById("bal-verdict");
    var note = document.getElementById("bal-note");
    var recalcBalance = function () {
      var inc = sumOf("income"), exp = sumOf("expense");
      var base = inc || expected;
      var gap = base - exp;
      document.getElementById("bal-income").textContent = show(inc);
      document.getElementById("bal-spending").textContent = show(exp);
      if (!base) {
        verdict.textContent = "Set a budget to see whether it fits your income";
        note.textContent = "Budget your income first, then your spending.";
      } else if (gap > 0) {
        verdict.textContent = show(gap) + " left to allocate";
        note.textContent = "Planned spending fits inside planned income.";
      } else if (gap === 0) {
        verdict.textContent = "Every " + symbol + " of income is allocated";
        note.textContent = "Planned spending fits inside planned income.";
      } else {
        verdict.textContent = show(gap) + " more than your income";   // show() is abs
        note.textContent = "Your plan spends more than you plan to earn — trim a " +
          "category or raise the income budget.";
      }
      if (!inc && expected) {
        note.textContent = "No income budgeted yet, so this is measured against " +
          "the " + show(expected) + " your paydays are expected to bring in.";
      }
      verdict.classList.toggle("neg", gap < 0 && !!base);
      verdict.classList.toggle("pos", gap >= 0 || !base);
    };
    document.addEventListener("input", function (e) {
      if (e.target.hasAttribute && e.target.hasAttribute("data-kind")) recalcBalance();
    });
    recalcBalance();
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
