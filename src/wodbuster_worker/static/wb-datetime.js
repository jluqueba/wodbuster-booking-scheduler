// Shared client-side date/time formatter.
//
// Consolidates every user-facing timestamp on the same locale-aware
// rendering the dashboard countdown pioneered: the browser's own
// `toLocaleString` picks month/weekday names, 12h/24h and field order
// from the device locale, while the instant is pinned to the gym
// timezone (WORKER_TIMEZONE, surfaced via <meta name="wb-timezone">)
// so class times never shift when the operator travels.
//
// Server templates emit `<time datetime="{iso}" data-wb-datetime="{mode}">
// {server fallback}</time>`. This script upgrades the text content in
// place on load and after every HTMX swap. Without JS the server
// fallback stays visible (progressive enhancement).
//
// Modes: stamp (weekday+date+time), date (weekday+date), dateyear
// (weekday+date+year), time (time only).
(function () {
  var OPTS = {
    stamp: {
      weekday: 'short',
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    },
    date: { weekday: 'short', day: '2-digit', month: 'short' },
    dateyear: {
      weekday: 'short',
      day: '2-digit',
      month: 'short',
      year: 'numeric',
    },
    time: { hour: '2-digit', minute: '2-digit', hour12: false },
  };

  function defaultTz() {
    var meta = document.querySelector('meta[name="wb-timezone"]');
    var value = meta && meta.getAttribute('content');
    return value || undefined;
  }

  function format(date, mode, tz) {
    var base = OPTS[mode] || OPTS.stamp;
    var opts = base;
    if (tz) {
      opts = {};
      for (var key in base) {
        if (Object.prototype.hasOwnProperty.call(base, key)) {
          opts[key] = base[key];
        }
      }
      opts.timeZone = tz;
    }
    return date.toLocaleString(undefined, opts);
  }

  function upgrade(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var tz = defaultTz();
    var nodes = scope.querySelectorAll('[data-wb-datetime]');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var iso = el.getAttribute('datetime') || el.getAttribute('data-wb-iso');
      if (!iso) continue;
      var parsed = new Date(iso);
      if (isNaN(parsed.getTime())) continue;
      var mode = el.getAttribute('data-wb-datetime') || 'stamp';
      var elTz = el.getAttribute('data-wb-tz') || tz;
      el.textContent = format(parsed, mode, elTz);
    }
  }

  // Exposed so the dashboard countdown can reuse the exact same
  // formatting instead of redefining it.
  window.wbDateTime = { format: format, upgrade: upgrade, defaultTz: defaultTz };

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  ready(function () {
    upgrade(document);
  });

  // HTMX swaps (e.g. the cookie status card) replace DOM subtrees that
  // never ran through the initial pass. The event bubbles to document.
  document.addEventListener('htmx:afterSwap', function (event) {
    upgrade(event.target || document);
  });
})();
