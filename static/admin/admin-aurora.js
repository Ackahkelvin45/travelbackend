/**
 * Azura admin behaviors.
 *
 * Sidebar persistence: Unfold's stock `theme()` (unfold/js/app.js) force-
 * closes the sidebar whenever the window is <= 1280px wide and refuses to
 * persist its state there — on a 13" laptop (1280 CSS px) the sidebar
 * vanishes on every page load. This patch lowers the off-canvas breakpoint
 * to 1024px and honors localStorage above it, so the sidebar stays put
 * across navigation on laptop screens.
 *
 * This file loads before unfold/js/app.js (UNFOLD["SCRIPTS"] are emitted
 * earlier in the skeleton), so the patch waits for alpine:init — Alpine is
 * deferred and fires it after app.js has defined window.theme, but before
 * any x-data="theme(...)" evaluates.
 */
(function () {
  var BREAKPOINT = 1024;

  document.addEventListener("alpine:init", function () {
    var original = window.theme;
    if (typeof original !== "function") return;

    window.theme = function (defaultTheme) {
      var t = original(defaultTheme);

      t.sidebarOpen = function () {
        if (window.innerWidth <= BREAKPOINT) return false;
        var stored = localStorage.getItem("sidebarOpen");
        return stored === null ? true : stored !== "0";
      };

      t.sidebarToggle = function () {
        this.sidebarOpen = !this.sidebarOpen;
        if (window.innerWidth > BREAKPOINT) {
          localStorage.setItem("sidebarOpen", this.sidebarOpen ? "1" : "0");
        }
      };

      if (t.themeBindings) {
        t.themeBindings["x-resize.window"] = function () {
          if (window.innerWidth <= BREAKPOINT) {
            this.sidebarOpen = false;
          } else {
            this.sidebarOpen = localStorage.getItem("sidebarOpen") !== "0";
          }
        };
      }

      return t;
    };
  });
})();

/**
 * Confirmation before anything takes effect. Covers every bulk action and the
 * one-click buttons on a record's page. Delete, cancel-with-refund style pages
 * that already ask (their own screens) are left alone.
 */
(function () {
  // Open their own page, or only produce a file: nothing happens yet.
  var NO_CONFIRM = ["", "delete_selected", "export_csv", "assign_coordinator", "move_allocation"];
  // What each serious action really does. Anything else gets the plain question.
  var DETAILS = {
    cancel_with_refund: ["Cancels the booking, frees its rooms and creates refunds under the policy. Tell the guest yourself.", true],
    cancel_as_organizer: ["Cancels the booking with a full refund. Tell the guest yourself.", true],
    mark_completed: ["Closes the booking as completed. It can no longer be paid.", false],
    forfeit: ["Forfeits the ticket. The guest loses access to the event.", true],
    mark_processed: ["Records the refund as paid out. Do this only after the money has gone back on Paystack.", true],
    reject_refunds: ["Rejects the refund. The guest will not be paid.", true],
    approve: ["Approves the cancellation request and starts the refund.", true],
    reject: ["Rejects the cancellation request.", true],
    clear_chargeback: ["Only do this once Paystack shows the dispute closed. Refunds on these payments open up again.", true],
    mark_lost: ["Closes the lead and stops any reminders.", true],
    deactivate_users: ["These accounts will no longer be able to sign in.", true],
    deactivate_packages: ["Guests can no longer see or book these. Existing bookings are kept.", true],
    block_units: ["These rooms stop being sold.", true],
    send_newsletter_announcement: ["Emails every active subscriber. This can't be recalled.", true],
    send_balance_reminders: ["Emails each guest a balance reminder.", false],
    publish_documents: ["Guests will have to accept the new version from now on.", false],
    hide_reviews: ["They leave the site and the star ratings. You can show them again.", false],
    take_off_sale: ["Takes effect for guests once you publish the price list.", false],
    release_holds: ["Those rooms go back on sale, even if a guest is at checkout.", true]
  };
  // One-click buttons on a record's page, by the end of their link.
  var BUTTONS = {
    "send-reminder": "Emails the guest now.",
    "resend-confirmation": "Emails the confirmation and receipt to the guest again.",
    "mark-contacted": "Records that the lead guest has been contacted.",
    "mark-replied": "Records that this lead has been replied to.",
    "check-in": "Checks this guest in to the event.",
    "publish-prices": "New prices go live for guests straight away. Existing bookings keep their prices.",
    "capture-lead": "Creates a lead from this conversation, or links the guest's open one.",
    "mark-processed": "Records the money as returned and updates the booking. Save the reference first.",
    "undo-check-in": "Takes this guest back to not checked in.",
    "undo-forfeit": "Gives this guest their ticket back.",
    "cancel-booking": "Cancels the booking, frees its rooms and creates refunds under the policy.",
    "approve": "Cancels the booking and creates the refund. Save any staff note first.",
    "reject": "Keeps the booking and reopens payments. Save any staff note first.",
    "mark-donation-reviewed": "Clears the review flag on this donation."
  };
  var DANGER_BUTTONS = ["cancel-booking", "mark-processed", "approve", "reject"];

  var dialog, resolve;

  function ask(title, body, danger) {
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.className = "az-confirm";
      dialog.innerHTML = '<form method="dialog"><h2></h2><p></p><div class="az-confirm__actions">' +
        '<button value="no" class="az-confirm__cancel">Cancel</button>' +
        '<button value="yes" class="az-confirm__ok">Confirm</button></div></form>';
      dialog.addEventListener("close", function () {
        if (resolve) resolve(dialog.returnValue === "yes");
        resolve = null;
      });
      document.body.appendChild(dialog);
    }
    dialog.querySelector("h2").textContent = title;
    dialog.querySelector("p").textContent = body;
    dialog.querySelector("p").hidden = !body;
    dialog.querySelector(".az-confirm__ok").classList.toggle("az-confirm__ok--danger", !!danger);
    dialog.returnValue = "no";
    dialog.showModal();
    dialog.querySelector(danger ? ".az-confirm__cancel" : ".az-confirm__ok").focus();
    return new Promise(function (done) { resolve = done; });
  }

  // Bulk actions.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.id !== "changelist-form" || form.dataset.confirmed) return;
    var select = form.querySelector('select[name="action"]');
    if (!select || NO_CONFIRM.indexOf(select.value) !== -1) return;
    if (!event.submitter || event.submitter.name !== "index") return;  // saving editable rows
    var all = form.querySelector('input[name="select_across"]');
    var count = form.querySelectorAll("input.action-select:checked").length;
    if (!count) return;  // the server's "select some items" message explains it
    var label = select.options[select.selectedIndex].text;
    var detail = DETAILS[select.value] || ["", false];
    var what = all && all.value === "1" ? "all matching items" : count + (count === 1 ? " item" : " items");
    event.preventDefault();
    var submitter = event.submitter;
    ask(label + "?", (detail[0] ? detail[0] + " " : "") + "This applies to " + what + ".", detail[1]).then(function (yes) {
      if (!yes) return;
      form.dataset.confirmed = "1";
      form.requestSubmit(submitter);
    });
  }, true);

  // One-click buttons.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link) return;
    var name = (link.getAttribute("href").match(/\/([a-z-]+)\/?$/) || [])[1];
    if (!BUTTONS[name] || !/\/admin\//.test(link.href)) return;
    event.preventDefault();
    // The link's text starts with its Material icon's name; the label is the last line.
    var title = link.textContent.trim().split("\n").pop().trim() + "?";
    ask(title, BUTTONS[name], DANGER_BUTTONS.indexOf(name) !== -1).then(function (yes) {
      if (!yes) return;
      // These change something, so they POST with the page's CSRF token.
      var form = document.createElement("form");
      form.method = "post";
      form.action = link.href;
      var token = document.createElement("input");
      token.type = "hidden";
      token.name = "csrfmiddlewaretoken";
      token.value = document.querySelector('input[name="csrfmiddlewaretoken"]').value;
      form.appendChild(token);
      document.body.appendChild(form);
      form.submit();
    });
  }, true);
})();
