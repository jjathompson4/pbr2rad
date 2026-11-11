// Set the theme before first paint to avoid a flash of the wrong mode.
// A file, not an inline script: the Content-Security-Policy allows none.
(function () {
  var stored = null;
  try { stored = localStorage.getItem("theme"); } catch (e) { /* storage blocked */ }
  document.documentElement.dataset.theme =
    stored || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
})();
