/*
 * Account disclosure menu enhancement (WAI-ARIA disclosure pattern).
 *
 * The menu is a native <details>/<summary>, so it already toggles and is
 * keyboard-operable without JavaScript. This layer adds the two behaviours
 * a native disclosure lacks: close when the user clicks outside the menu,
 * and close on Escape (returning focus to the trigger). No dependencies.
 */
(function () {
  "use strict";

  function enhance(menu) {
    var summary = menu.querySelector("summary");
    if (!summary) {
      return;
    }

    document.addEventListener("click", function (event) {
      if (menu.open && !menu.contains(event.target)) {
        menu.open = false;
      }
    });

    menu.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && menu.open) {
        menu.open = false;
        summary.focus();
      }
    });
  }

  function init() {
    var menus = document.querySelectorAll("[data-wb-usermenu]");
    for (var i = 0; i < menus.length; i += 1) {
      enhance(menus[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
