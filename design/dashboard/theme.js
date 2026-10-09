/* Opcional: alterna o tema e lembra a escolha. Sem este arquivo, vale prefers-color-scheme. */
(function () {
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem("af-theme");
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) { /* armazenamento indisponível: segue o tema do sistema */ }

  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener("click", function () {
      var current = root.getAttribute("data-theme") ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      var next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("af-theme", next); } catch (e) { /* ignora */ }
    });
  }

  /* Copiar hash: <button data-copy="valor completo"> */
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button || !navigator.clipboard) return;
    navigator.clipboard.writeText(button.getAttribute("data-copy")).then(function () {
      var label = button.getAttribute("aria-label");
      button.setAttribute("aria-label", "Copiado");
      setTimeout(function () { button.setAttribute("aria-label", label); }, 1500);
    });
  });

  /* Esc fecha o diálogo aberto via :target */
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && location.hash) location.hash = "";
  });
})();
