/* Opcional: alterna o tema (lembra a escolha) e melhora o teclado nos diálogos.
   Sem este arquivo, vale prefers-color-scheme e tudo continua funcionando por CSS. */
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

  /* Fragmentos HTMX (run a cada 2 s, connectors a cada 30 s) são trocados por outerHTML.
     Se o foco está dentro do trecho, a troca o jogaria para o body e o leitor de tela
     reanunciaria o conteúdo; adia a troca até o foco sair (o próximo ciclo atualiza). */
  document.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail && event.detail.target;
    var active = document.activeElement;
    if (target && active && active !== document.body && target.contains(active)) {
      event.detail.shouldSwap = false;
    }
  });

  /* Diálogos abertos via :target (sem este bloco continuam abrindo e fechando por CSS):
     leva o foco para o diálogo, mantém Tab dentro dele, fecha com Esc/Fechar/Cancelar sem
     rolar a página ao topo e devolve o foco a quem o abriu. */
  var opener = null;
  var savedScroll = 0;

  function openDialog() {
    return document.querySelector(".dialog-backdrop:target");
  }

  /* Conteúdo de <details> fechado mantém caixas de layout, mas não recebe foco. */
  function insideClosedDetails(element) {
    var details = element.parentElement && element.parentElement.closest("details");
    while (details) {
      var ownSummary = element.tagName === "SUMMARY" && element.parentElement === details;
      if (!details.open && !ownSummary) return true;
      details = details.parentElement && details.parentElement.closest("details");
    }
    return false;
  }

  function focusableIn(root) {
    var selector = "a[href], button, input, select, textarea, summary, [tabindex]";
    return Array.prototype.filter.call(root.querySelectorAll(selector), function (element) {
      return element.tabIndex >= 0 && !element.disabled && !element.hidden &&
        !insideClosedDetails(element) &&
        element.type !== "hidden" && element.getClientRects().length > 0 &&
        getComputedStyle(element).visibility !== "hidden";
    });
  }

  var closing = false;

  function closeDialog() {
    savedScroll = window.scrollY;
    closing = true;
    location.hash = "";
  }

  function focusDialog() {
    var dialog = openDialog();
    var box = dialog && dialog.querySelector(".dialog");
    if (box) box.focus({ preventScroll: true });
  }

  function restoreAfterClose() {
    window.scrollTo(window.scrollX, savedScroll);
    var target = opener;
    opener = null;
    if (!target || !document.contains(target)) return;
    if (target.getClientRects().length === 0) {
      var menu = target.closest("details");
      target = (menu && menu.querySelector("summary")) || null;
    }
    if (target) target.focus({ preventScroll: true });
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[href^='#']") : null;
    if (!link) return;
    var id = link.getAttribute("href").slice(1);
    if (id === "" && link.closest(".dialog-backdrop")) {
      event.preventDefault();
      closeDialog();
      return;
    }
    var target = id ? document.getElementById(id) : null;
    if (target && target.classList.contains("dialog-backdrop") && !openDialog()) {
      opener = link;
      savedScroll = window.scrollY;
    }
  });

  window.addEventListener("hashchange", function () {
    if (openDialog()) {
      focusDialog();
    } else if (closing) {
      closing = false;
      restoreAfterClose();
    }
  });
  /* Abertura por URL direta: :target só vale depois do passo de rolagem ao fragmento, que
     pode vir depois deste script; tenta de novo em load e só se o foco ainda estiver fora. */
  function focusDialogIfOutside() {
    var dialog = openDialog();
    if (dialog && !dialog.contains(document.activeElement)) focusDialog();
  }
  focusDialogIfOutside();
  window.addEventListener("load", focusDialogIfOutside);

  document.addEventListener("keydown", function (event) {
    var dialog = openDialog();
    if (!dialog) {
      /* gaveta do menu no mobile */
      var toggleBox = document.querySelector(".nav-toggle");
      if (event.key === "Escape" && toggleBox && toggleBox.checked &&
          window.matchMedia("(max-width: 56rem)").matches) {
        toggleBox.checked = false;
        toggleBox.focus();
      }
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeDialog();
      return;
    }
    if (event.key !== "Tab") return;
    var box = dialog.querySelector(".dialog");
    if (!box) return;
    var items = focusableIn(box);
    if (items.length === 0) {
      event.preventDefault();
      box.focus();
      return;
    }
    var first = items[0];
    var last = items[items.length - 1];
    var active = document.activeElement;
    if (!box.contains(active)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && (active === first || active === box)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  });
})();
