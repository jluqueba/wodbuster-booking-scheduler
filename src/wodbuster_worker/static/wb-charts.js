/*
 * Chart bootstrap for the statistics page (ADR-0014).
 *
 * Draws what the server sends. It computes nothing, holds no colour and
 * holds no user-visible string:
 *
 *   - colours come from CSS custom properties declared in brand.css and
 *     are read with getComputedStyle at chart creation, so a theme
 *     change recolours every chart without touching this file;
 *   - every label, unit and tooltip phrase arrives inside the chart's
 *     own JSON block, already translated by the server;
 *   - the numbers are shaped by statistics/charts.py.
 *
 * If Chart.js did not load, the tables the server already rendered are
 * the page. Nothing here throws in that case.
 */
(function () {
  "use strict";

  var instances = {};

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v || "").trim() || fallback;
  }

  function palette() {
    return {
      series: [
        cssVar("--wb-chart-1", "#f8ff40"),
        cssVar("--wb-chart-2", "#60a5fa"),
        cssVar("--wb-chart-3", "#4ade80"),
        cssVar("--wb-chart-4", "#c084fc"),
        cssVar("--wb-chart-5", "#fbbf24"),
        cssVar("--wb-chart-6", "#f87171")
      ],
      good: cssVar("--wb-success", "#4ade80"),
      bad: cssVar("--wb-danger", "#f87171"),
      grid: cssVar("--wb-chart-grid", "rgba(148,163,184,0.18)"),
      axis: cssVar("--wb-chart-axis", "#94a3b8"),
      ink: cssVar("--wb-text", "#f1f5f9")
    };
  }

  function readConfig(canvas) {
    var holder = document.getElementById(canvas.dataset.wbChart);
    if (!holder) {
      return null;
    }
    try {
      return JSON.parse(holder.textContent);
    } catch (err) {
      return null;
    }
  }

  function baseOptions(colours) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          callbacks: {}
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: colours.axis, autoSkip: true, maxRotation: 0 }
        },
        y: {
          beginAtZero: true,
          grid: { color: colours.grid },
          ticks: { color: colours.axis, precision: 0 }
        }
      }
    };
  }

  /* Zoom only where a series can outgrow the viewport. On a seven-bar
     chart it is a gesture that can only get in the way. */
  function zoomOptions() {
    return {
      pan: { enabled: true, mode: "x" },
      zoom: {
        wheel: { enabled: true },
        pinch: { enabled: true },
        mode: "x"
      },
      limits: { x: { minRange: 3 } }
    };
  }

  function buildDropRate(canvas, cfg, colours) {
    var opts = baseOptions(colours);
    /* An all-zero drop rate must not stretch the axis to fit a single
       hair; a perfect month should look perfect. */
    opts.scales.y.suggestedMax = Math.min(cfg.meta.max || 100, 100);
    opts.scales.y.ticks.callback = function (value) {
      return value + "%";
    };
    opts.plugins.tooltip.callbacks.label = function (item) {
      var i = item.dataIndex;
      return item.parsed.y + "% " + cfg.strings.of
        .replace("{dropped}", cfg.meta.dropped[i])
        .replace("{booked}", cfg.meta.booked[i]);
    };
    return new window.Chart(canvas, {
      type: "bar",
      data: {
        labels: cfg.labels,
        datasets: [{
          data: cfg.values,
          /* Worse rates read redder: the colour carries the ranking so
             the reader finds the problem hour without comparing bars. */
          backgroundColor: cfg.values.map(function (v) {
            return v >= 33 ? colours.bad : (v >= 20 ? colours.series[4] : colours.good);
          }),
          borderWidth: 0,
          borderRadius: 3,
          maxBarThickness: 44
        }]
      },
      options: opts
    });
  }

  function buildTrend(canvas, cfg, colours) {
    var opts = baseOptions(colours);
    opts.scales.x.stacked = true;
    opts.scales.y.stacked = true;
    opts.plugins.legend.display = true;
    opts.plugins.legend.labels = { color: colours.ink, boxWidth: 12 };
    opts.plugins.zoom = zoomOptions();
    opts.plugins.tooltip.displayColors = true;
    return new window.Chart(canvas, {
      type: "bar",
      data: {
        labels: cfg.labels,
        datasets: cfg.series.map(function (s, i) {
          return {
            label: s.label,
            data: s.values,
            backgroundColor: i === 0 ? colours.good : colours.bad,
            borderWidth: 0,
            maxBarThickness: 28
          };
        })
      },
      options: opts
    });
  }

  function buildLead(canvas, cfg, colours) {
    var opts = baseOptions(colours);
    opts.indexAxis = "y";
    opts.scales.x.beginAtZero = true;
    opts.scales.x.grid = { color: colours.grid };
    opts.scales.x.ticks = { color: colours.axis, precision: 0 };
    opts.scales.y.grid = { display: false };
    opts.plugins.tooltip.callbacks.label = function (item) {
      return item.parsed.x + " " + cfg.strings.unit;
    };
    return new window.Chart(canvas, {
      type: "bar",
      data: {
        labels: cfg.labels,
        datasets: [{
          data: cfg.values,
          backgroundColor: colours.series[1],
          borderWidth: 0,
          borderRadius: 3,
          maxBarThickness: 30
        }]
      },
      options: opts
    });
  }

  function buildGrid(canvas, cfg, colours) {
    var peak = cfg.meta.peak || 1;
    var slots = cfg.meta.slots || [];
    var opts = baseOptions(colours);
    /* Category axes on both sides. A linear hour axis interpolated
       between integers and produced ticks like 08.5:00, and a gym's
       slots are not evenly spaced anyway. */
    opts.scales.x = {
      type: "category",
      labels: slots,
      offset: true,
      grid: { display: false },
      ticks: { color: colours.axis, autoSkip: false, maxRotation: 90, minRotation: 0 }
    };
    opts.scales.y = {
      type: "category",
      labels: cfg.labels,
      offset: true,
      grid: { display: false },
      ticks: { color: colours.axis }
    };
    opts.plugins.tooltip.callbacks.title = function () {
      return "";
    };
    opts.plugins.tooltip.callbacks.label = function (item) {
      var d = item.raw;
      return cfg.strings.cell
        .replace("{count}", d.v)
        .replace("{weekday}", d.y)
        .replace("{hour}", d.x);
    };
    return new window.Chart(canvas, {
      type: "matrix",
      data: {
        datasets: [{
          data: cfg.points,
          /* Intensity carries the count, so a busy slot reads without
             a legend. The floor keeps a single session visible. */
          backgroundColor: function (ctx) {
            var v = ctx.raw ? ctx.raw.v : 0;
            return "rgba(74, 222, 128, " + (0.18 + 0.72 * (v / peak)) + ")";
          },
          borderWidth: 0,
          width: function (ctx) {
            var a = ctx.chart.chartArea;
            if (!a || !slots.length) { return 0; }
            return (a.right - a.left) / slots.length - 3;
          },
          height: function (ctx) {
            var a = ctx.chart.chartArea;
            if (!a || !cfg.labels.length) { return 0; }
            return (a.bottom - a.top) / cfg.labels.length - 3;
          }
        }]
      },
      options: opts
    });
  }

  var BUILDERS = {
    "bar": buildDropRate,
    "bar-stacked": buildTrend,
    "bar-horizontal": buildLead,
    "matrix": buildGrid
  };

  function destroyAll() {
    Object.keys(instances).forEach(function (id) {
      try {
        instances[id].destroy();
      } catch (err) {
        /* A canvas already detached from the DOM is not a problem. */
      }
      delete instances[id];
    });
  }

  function render() {
    if (!window.Chart) {
      return;
    }
    destroyAll();
    var colours = palette();
    var canvases = document.querySelectorAll("canvas[data-wb-chart]");
    Array.prototype.forEach.call(canvases, function (canvas) {
      var cfg = readConfig(canvas);
      if (!cfg) {
        return;
      }
      var hasData = (cfg.values && cfg.values.length) ||
        (cfg.points && cfg.points.length) ||
        (cfg.series && cfg.series.length);
      var builder = BUILDERS[cfg.kind];
      if (!hasData || !builder) {
        return;
      }
      try {
        instances[canvas.id] = builder(canvas, cfg, colours);
      } catch (err) {
        /* One chart failing must not take the rest of the page with
           it; the table beside it still carries the numbers. */
        if (window.console) {
          window.console.warn("wb-charts: " + canvas.id, err);
        }
      }
    });
  }

  window.wbCharts = { render: render, destroyAll: destroyAll };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
