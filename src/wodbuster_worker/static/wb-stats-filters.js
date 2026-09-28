/* Section filters for the statistics page.
 *
 * Progressive enhancement over three real GET forms. Without this file
 * a filter click navigates and the server renders the whole page with
 * the chosen window, which is the behaviour FR-032 requires. With it,
 * the click fetches that section alone and swaps it in, so choosing a
 * window for the charts does not rebuild the calendar above them or
 * re-run a capture the reader did not ask for.
 *
 * No copy and no colour live here. The fragment arrives rendered by
 * the server, already translated and already styled (INV-011).
 */
(function () {
  "use strict";

  var SECTION_ATTR = "data-wb-section";
  var inFlight = {};

  function sectionOf(node) {
    var host = node.closest("[" + SECTION_ATTR + "]");
    return host ? host.getAttribute(SECTION_ATTR) : null;
  }

  /* The address bar is the canonical state, not the form.
   *
   * Only one section is replaced per click, so the hidden inputs in
   * the other two still carry the window that section had when the
   * page was rendered. Building the next URL from a form would then
   * undo a change made a moment earlier, and the page would look right
   * until a reload lost it.
   *
   * Read at the moment it is needed rather than captured at click
   * time: two requests can be in flight at once, and the one that
   * resolves second would otherwise write back a snapshot taken
   * before the first had landed.
   */
  function stateWith(change) {
    var params = new URLSearchParams(window.location.search);
    if (change && change.name) {
      params.set(change.name, change.value);
    }
    return params;
  }

  function urlFor(form, button) {
    var change = button && button.name ? { name: button.name, value: button.value } : null;
    var fragment = stateWith(change);
    fragment.set("section", sectionOf(form));
    return {
      change: change,
      action: form.action,
      fragment: form.action + "?" + fragment.toString()
    };
  }

  /* Bring every form back in step with the URL, so the no-script
     fallback and a later click both submit the state actually on
     screen.

     Creates the input when it is missing rather than only updating
     what the server happened to render. A visible control owns its
     own value, which is how the calendar keeps its month field. */
  function syncForms(params) {
    var forms = document.querySelectorAll("form[data-wb-filter], form.wb-monthjump");
    Array.prototype.forEach.call(forms, function (form) {
      var own = form.getAttribute("data-wb-filter");
      params.forEach(function (value, key) {
        if (key === "section" || key === own) {
          return;
        }
        var existing = form.querySelector("[name='" + key + "']");
        if (existing) {
          if (existing.type === "hidden") {
            existing.value = value;
          }
          return;
        }
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = key;
        input.value = value;
        form.appendChild(input);
      });
    });
  }

  /* Replaces the contents rather than the node. The section is a live
     region, and a live region only announces when the element that
     carries the attribute survives the update: swapping the node
     itself would change the numbers silently for anyone listening. */
  function swap(host, markup) {
    var parsed = new DOMParser().parseFromString(markup, "text/html");
    var fresh = parsed.querySelector("[" + SECTION_ATTR + "]");
    if (!fresh) {
      return false;
    }
    host.innerHTML = fresh.innerHTML;
    return true;
  }

  /* Chart.js keeps its instances keyed by canvas. Replacing the markup
     detaches the old canvases without telling it, so the instances are
     destroyed and rebuilt rather than left pointing at nodes that are
     no longer in the document. */
  function redrawCharts() {
    if (window.wbCharts && typeof window.wbCharts.render === "function") {
      window.wbCharts.render();
    }
  }

  function onSubmit(event) {
    var form = event.target;
    if (!form.matches || !form.matches("form[data-wb-filter]")) {
      return;
    }
    var name = sectionOf(form);
    var host = document.querySelector("[" + SECTION_ATTR + "='" + name + "']");
    if (!name || !host || inFlight[name]) {
      return;
    }

    event.preventDefault();
    var urls = urlFor(form, event.submitter);
    inFlight[name] = true;
    host.setAttribute("aria-busy", "true");

    fetch(urls.fragment, {
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin"
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("fragment " + response.status);
        }
        return response.text();
      })
      .then(function (markup) {
        if (!swap(host, markup)) {
          throw new Error("no section in fragment");
        }
        /* Recomputed here, not at click time: another section may have
           landed while this request was in flight, and writing back a
           stale snapshot would drop its window from the URL. */
        var params = stateWith(urls.change);
        window.history.replaceState({}, "", urls.action + "?" + params.toString());
        syncForms(params);
        redrawCharts();
      })
      .catch(function () {
        /* Whatever went wrong, the server can still render this. A
           filter that silently does nothing is worse than one that
           costs a page load. */
        window.location.assign(urls.action + "?" + stateWith(urls.change).toString());
      })
      .then(function () {
        delete inFlight[name];
        host.removeAttribute("aria-busy");
      });
  }

  document.addEventListener("submit", onSubmit);
})();
