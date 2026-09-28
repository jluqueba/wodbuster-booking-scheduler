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
   * until a reload lost it. Reading the current URL and applying only
   * the submitted change keeps every window, whatever order the
   * clicks arrive in.
   */
  function urlFor(form, button) {
    var params = new URLSearchParams(window.location.search);
    if (button && button.name) {
      params.set(button.name, button.value);
    }
    var fragment = new URLSearchParams(params);
    fragment.set("section", sectionOf(form));
    return {
      page: form.action + "?" + params.toString(),
      fragment: form.action + "?" + fragment.toString(),
      params: params
    };
  }

  /* Bring the hidden inputs of every other section back in step with
     the URL, so the no-script fallback and a later click both submit
     the state actually on screen. */
  function syncForms(params) {
    var forms = document.querySelectorAll("form[data-wb-filter], form.wb-monthjump");
    Array.prototype.forEach.call(forms, function (form) {
      Array.prototype.forEach.call(form.querySelectorAll("input[type=hidden]"), function (input) {
        if (params.has(input.name)) {
          input.value = params.get(input.name);
        }
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
        /* The address bar follows the page, not the fragment, so a
           reload or a shared link lands on what is on screen. */
        window.history.replaceState({}, "", urls.page);
        syncForms(urls.params);
        redrawCharts();
      })
      .catch(function () {
        /* Whatever went wrong, the server can still render this. A
           filter that silently does nothing is worse than one that
           costs a page load. */
        window.location.assign(urls.page);
      })
      .then(function () {
        delete inFlight[name];
        host.removeAttribute("aria-busy");
      });
  }

  document.addEventListener("submit", onSubmit);
})();
