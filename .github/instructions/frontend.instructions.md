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

## Never put JavaScript in an attribute

- Templates carry **no inline event handlers** (`onsubmit`, `onclick`, ...) that
  contain Jinja. The editor hands an event attribute's contents to the
  TypeScript parser, which knows nothing about Jinja and reports `{{` as the
  start of an object literal, so every such site emits permanent, unfixable
  syntax errors in the Problems view.
- It is also a correctness trap: Jinja autoescapes an apostrophe to `&#39;`, the
  browser decodes it **before** parsing the script, and the JS string literal
  closes early. The handler then fails to compile and the form submits with no
  confirmation at all. This shipped once and was caught in review.
- Pass values through `data-` attributes and read them from a script file or a
  delegated listener. In a data attribute the value is text, so autoescape is
  exactly the right treatment. The confirm modal (`_confirm_modal.html`,
  `data-wb-confirm`) and the dashboard countdown (`data-fires-at`) both follow
  this.
- `tests/unit/test_template_hygiene.py` enforces it. Do not weaken that test.

## Run the template gate before committing

- `djlint` understands `{% %}` as template syntax rather than text, which no
  other tool in the stack does. It catches malformed HTML, unclosed tags,
  invalid nesting, and broken Jinja without the phantom findings that source
  linters produce on templates.

  ```powershell
  djlint src/wodbuster_worker/templates --lint
  ```

- It runs inside `.\check.ps1`, `make check`, and CI, so the whole gate is
  `.\check.ps1`. Configuration lives under `[tool.djlint]` in `pyproject.toml`;
  it lints only and never reformats.
- An empty `<th></th>` is reported as a header with no name. A column with no
  header (an actions column) uses `<td></td>` in the header row instead.

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

## Email templates are the exception (`templates/email/**`)

- HTML email is a different medium: mail clients strip `<head>`/external CSS, so
  **inline styles are mandatory** and layout is table-based. The `<html lang>`
  is a Jinja variable (per-recipient), not a literal.
- To keep the web HTML linter (Edge Tools/webhint, axe) from flagging those as
  false positives, the email template uses a **`.jinja` extension**
  (`notification.html.jinja`) so it is not analyzed as HTML. Because the name no
  longer ends in `.html`, the Jinja `Environment` sets `autoescape=True`
  explicitly (name-based `select_autoescape` would switch off and leak unescaped
  user data). The "zero VS Code Problems" rule is satisfied because there is no
  linted HTML email file.
- Preview and iterate with `scripts/preview_email.py`, which renders the samples
  into the OS temp dir (outside the workspace, so those throwaway `.html` files
  are never linted) and opens the folder. Keep colors/values literal hex (no CSS
  variables) so every mail client renders them.

