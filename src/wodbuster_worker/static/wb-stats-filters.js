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
  var pending = {};

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
    /* Transport, never state. It reaches the address bar whenever the
       route answers a fragment request with the whole page, and from
       there it would be copied into every form and survive a reload,
       which would then serve a bare block with no page around it. */
    params.delete("section");
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

  /* The windows this page owns. Everything else in the query string
     is somebody else's, and copying an arbitrary parameter into a
     form would submit it back as though the page had meant it. */
  var CARRIED = ["month", "attendance", "points", "patterns"];

  /* Bring every form back in step with the URL, so the no-script
     fallback and a later click both submit the state actually on
     screen.

     Creates the input when it is missing rather than only updating
     what the server happened to render. A visible control owns its
     own value, which is how the calendar keeps its month field.

     Looked up through ``form.elements`` rather than a built selector:
     a name is data, and interpolating one into CSS makes a crafted
     query string a parse error rather than a no-op. */
  function syncForms(params) {
    var forms = document.querySelectorAll("form[data-wb-filter], form.wb-monthjump");
    Array.prototype.forEach.call(forms, function (form) {
      var own = form.getAttribute("data-wb-filter");
      CARRIED.forEach(function (key) {
        if (key === own || !params.has(key)) {
          return;
        }
        var value = params.get(key);
        var existing = form.elements.namedItem(key);
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
     no longer in the document.

     Asked for only where canvases are involved, before or after. The
     rebuild is global, so running it after an attendance click would
     throw away a zoom the reader had applied to a chart that did not
     change. Testing only the new markup is not enough: a period with
     no data renders no canvas at all, and skipping the rebuild there
     would leave every old instance registered against a detached
     node. */
  function redrawCharts(host, hadCharts) {
    if (!hadCharts && !host.querySelector("canvas[data-wb-chart]")) {
      return;
    }
    if (window.wbCharts && typeof window.wbCharts.render === "function") {
      window.wbCharts.render();
    }
  }

  /* Replacing the markup destroys the control that was clicked, so a
     keyboard user is left on the document body with no way back to
     where they were. Focus is moved to the block itself, which is
     where the heading and the window control both sit.

     Only when focus was inside the block to begin with: a reader who
     has tabbed on while a request was in flight should not have it
     taken from them. */
  function restoreFocus(host, wasInside) {
    if (!wasInside) {
      return;
    }
    host.setAttribute("tabindex", "-1");
    host.focus({ preventScroll: true });
  }

  function onSubmit(event) {
    var form = event.target;
    if (!form.matches || !form.matches("form[data-wb-filter]")) {
      return;
    }
    var name = sectionOf(form);
    var host = document.querySelector("[" + SECTION_ATTR + "='" + name + "']");
    if (!name || !host) {
      return;
    }

    event.preventDefault();
    var urls = urlFor(form, event.submitter);
    var hadCharts = !!host.querySelector("canvas[data-wb-chart]");
    var hadFocus = host.contains(document.activeElement);

    /* A second click on the same block replaces the first rather than
       being dropped. Dropping it left the reader looking at a window
       they did not choose, with nothing on screen to say why. */
    if (pending[name]) {
      pending[name].abort();
    }
    var controller = new AbortController();
    pending[name] = controller;
    host.setAttribute("aria-busy", "true");

    fetch(urls.fragment, {
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
      signal: controller.signal
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("fragment " + response.status);
        }
        return response.text();
      })
      .then(function (markup) {
        /* A superseded request must not land. Aborting does not
           guarantee a rejection once the body has been read, so
           identity is checked here rather than only in the cleanup
           below: without it a slow first response could overwrite the
           fragment a later click already swapped in, and put the
           address bar back on the older window. */
        if (pending[name] !== controller) {
          return;
        }
        if (!swap(host, markup)) {
          throw new Error("no section in fragment");
        }
        /* Recomputed here, not at click time: another section may have
           landed while this request was in flight, and writing back a
           stale snapshot would drop its window from the URL. */
        var params = stateWith(urls.change);
        window.history.replaceState({}, "", urls.action + "?" + params.toString());
        syncForms(params);
        redrawCharts(host, hadCharts);
        restoreFocus(host, hadFocus);
      })
      .catch(function (error) {
        /* An abort is this handler replacing its own request, not a
           failure: navigating away would undo the click that caused
           it. Same for any other failure of a request already
           superseded, whose window is no longer the one wanted. */
        if (error && error.name === "AbortError") {
          return;
        }
        if (pending[name] !== controller) {
          return;
        }
        /* Whatever else went wrong, the server can still render this.
           A filter that silently does nothing is worse than one that
           costs a page load. */
        window.location.assign(urls.action + "?" + stateWith(urls.change).toString());
      })
      .then(function () {
        /* Only the request still current cleans up. An aborted one
           clearing the flag would take the busy state off a block
           whose replacement is still on its way. */
        if (pending[name] === controller) {
          delete pending[name];
          host.removeAttribute("aria-busy");
        }
      });
  }

  document.addEventListener("submit", onSubmit);
})();
