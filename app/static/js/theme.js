/*
 * theme.js — runs synchronously in <head>, before the first paint.
 *
 * Why a separate 30-file-size script instead of an inline snippet: the site's CSP
 * is `script-src 'self'` with no 'unsafe-inline'. An external script gives us the
 * same flash-free result (the class is on <html> before any body element is
 * parsed) without weakening the policy.
 *
 * Precedence: explicit stored choice > OS preference. No cookies are involved, so
 * the choice is per-device and disappears with the visitor's site data.
 */
(function () {
  "use strict";
  var KEY = "ssavevideo:theme";
  var root = document.documentElement;

  function stored() {
    try {
      return window.localStorage.getItem(KEY);
    } catch (e) {
      return null; // private mode / disabled storage: fall through to OS preference
    }
  }

  function apply(mode) {
    root.classList.toggle("dark", mode === "dark");
    root.classList.toggle("light", mode === "light");
    root.style.colorScheme = mode; // CSSOM, not a style attribute: CSP-friendly
  }

  var saved = stored();
  var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  apply(saved === "light" || saved === "dark" ? saved : prefersDark ? "dark" : "light");

  // Expose just enough for app.js to toggle without re-reading storage twice.
  window.__ssTheme = {
    toggle: function () {
      var next = root.classList.contains("dark") ? "light" : "dark";
      apply(next);
      try {
        window.localStorage.setItem(KEY, next);
      } catch (e) {
        /* ignore */
      }
      return next;
    },
  };
})();
