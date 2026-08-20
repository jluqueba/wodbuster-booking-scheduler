---
name: 'Frontend change acceptance'
description: 'A frontend change is not valid until VS Code Problems is zero; plus how to verify layout'
applyTo: 'src/wodbuster_worker/templates/**/*.html, src/wodbuster_worker/static/**'
---

Rules for any change to HTML templates, CSS, or static frontend assets. These
are durable project conventions (kept here so they survive branch and context
switches).

## Zero VS Code Problems before a change is valid

- After ANY change to a template or CSS/static asset, the change is only
  acceptable once the VS Code Problems view shows **zero errors AND zero
  warnings** for the touched files. Verify before declaring the change done and
  iterate until clean. The diagnostics come from the Edge Tools (webhint) and
  axe integrations plus the HTML/CSS language servers.

## Verify layout with evidence, not by eye

- Do not theorize about CSS cascade or alignment. Verify against the real,
  rendered page. The Pico CSS base (loaded before `brand.css`) frequently wins
  on specificity; check the actual computed style.
- Reliable in-repo method when the built-in browser tools are unavailable:
  serve a small static harness from the running local server under `/static/`,
  render it headless with Playwright (`python -m playwright install chromium`),
  and read each element's `getBoundingClientRect` to prove alignment
  numerically. Delete the harness afterwards.

## Known Edge Tools / axe gotchas

- `<ul>` / `<ol>` must directly contain only `<li>`. Do not place a Jinja
  `{% if %}` / `{% for %}` / `{% set %}` as a direct child of a list: the static
  linter parses `{% %}` as a `#text` node and flags it. Keep the list's source
  children literal `<li>` only.
- Literal HTML tags inside Jinja comments (`{# ... <input> ... #}`) are parsed
  by axe as real elements and produce phantom errors (e.g. "form elements must
  have labels"). Describe in words inside comments; never put tags in them.
- CSS `user-select` needs `-webkit-user-select` alongside it (Safari).
- Prefer `background-color` over the `background` shorthand on inputs, or the
  shorthand wipes a picker `background-image` icon.
- A `<label for="X">` must match the id the picker macro emits (hyphenated,
  e.g. `class-time`), not the underscore i18n key.

## Pico specificity trap (recurring)

- Pico ships `button[type=submit] { width: 100% }` (specificity 0,1,1) and
  `select { width: 100% }`. A single `.wb-btn` class (0,1,0) does not beat them;
  use the doubled-class rule `.wb-btn.wb-btn { width: auto }`. Scope input
  overrides under a wrapper to outrank Pico's `input:not(...)` rules.
- Pico adds `margin-bottom` to real form controls (`button`, `select`) but not
  to `<a>`. In a table action cell that made a `<button>` sit ~10px higher than
  an `<a class="wb-btn">`; reset the margin on the button/select. Do not chase
  this with `vertical-align`.
