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
