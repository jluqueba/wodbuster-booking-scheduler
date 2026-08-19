---
name: 'UI Button Conventions'
description: 'Design and markup rules for every button in the web UI'
applyTo: 'src/wodbuster_worker/templates/**/*.html, src/wodbuster_worker/static/brand.css, src/wodbuster_worker/i18n/catalog.py'
---

Conventions agreed with the product owner on 2026-07-28. Apply them to every
button, submit input, and link styled as a button (`.wb-btn`, `[role="button"]`).

## Emoji first

- Every button carries an emoji, and the emoji always comes first: `emoji + space + text`.
- The emoji lives inside the translated label in `i18n/catalog.py` (both EN and ES),
  never hardcoded in the template. Example: `"gyms.actions.deactivate": "🚫 Deactivate"`.
- Reference emoji map:

  | Action | Emoji |
  |--------|-------|
  | Save / Validate | 💾 |
  | Create / Add | ➕ |
  | Delete / Remove | 🗑️ |
  | Edit | ✏️ |
  | Cancel / Deactivate / Unbind / Stop | 🚫 |
  | Confirm / Reactivate | ✅ |
  | End early | ⏹️ |
  | Refresh | 🔄 |
  | Back | ⬅️ |
  | Log out | 👋 |
  | Test | 🧪 |
  | Telegram link | 📱 |
  | Generate link | 🔗 |
  | Sign in: Microsoft / Google / GitHub | 🪟 / 🌐 / 🐙 |

## Buttons hug their text

- The base rule (`button, .wb-btn, [role="button"]` in `brand.css`) is
  `display: inline-flex; width: auto; white-space: nowrap; gap: 0.35rem`, with no
  `min-width` and no stretch. Buttons are as wide as their content.
- `width: auto` is load-bearing. Pico CSS (loaded before `brand.css`) ships
  `button[type=submit], input:not([type=checkbox],[type=radio]), select, textarea { width: 100% }`.
  The `button[type=submit]` selector has specificity (0,1,1), which OUTRANKS a single
  `.wb-btn` class (0,1,0). Since every action button is a `type="submit"`, a plain
  `.wb-btn { width: auto }` does NOT win. The override must beat (0,1,1): brand.css uses
  the doubled-class rule `.wb-btn.wb-btn { width: auto }` (specificity 0,2,0). Do not
  downgrade it back to a single class. Anchors styled as buttons (`<a class="wb-btn">`)
  are unaffected by Pico, but the doubled-class rule covers them too, harmlessly.
- Inline and in-table action buttons use the compact selector
  `.wb-cell-actions .wb-btn` (padding `0.35rem 0.7rem`, font `0.82rem`).
- Wrap a table-cell action button's `<form>` in `class="wb-inline-form"` so the
  form stays inline and the button right-aligns inside the `.wb-cell-actions` cell.
- Always wrap the label with Jinja whitespace control: `{{- t("key") -}}` inside
  `<button>` and button-styled `<a>`. A bare `{{ t("key") }}` on its own line
  injects whitespace text nodes that, combined with `white-space: nowrap`, render
  as leading and trailing space and make the button wider than its text.

## Color semantics

- `wb-btn wb-btn--primary` (accent / yellow): the main affirmative action.
  Save, Create, Add, Validate, Confirm, Refresh cookie.
- `wb-btn wb-btn--danger` (red): destructive or stop actions.
  Delete, Cancel, Deactivate, End early, Unbind, the modal Cancel.
- `wb-btn` (neutral, default): everything else. Edit, Reactivate, Back, Open bot.
  Reactivate is neutral, not green.
- Every button includes the `wb-btn` base class plus an optional modifier.

## Table actions

- One action per column. Do not group actions under a single "Actions" column.
- The gyms table columns are Gym, Slug, Status, Cookie, Activation. The Cookie
  column holds the refresh form, the Activation column holds deactivate or reactivate.
- All data tables use the same shell: `<div class="wb-table-wrap"><table class="wb-rules-table">`.
  Do not wrap a table in `.wb-card` or use a bespoke `.wb-table` class; that made the
  gyms table look different from the history and vacation tables.
- Cells are vertically centered: `.wb-rules-table` cells use `vertical-align: middle`
  (a single global rule in `brand.css`). Do not set per-table `vertical-align`.
- Every in-table action control shares one fixed height so buttons and combos line up on
  one centre line: `.wb-cell-actions .wb-btn` is `height: 2.15rem` (plus padding
  `0.35rem 0.7rem`, font `0.82rem`). Any `<select>` placed in a table cell must match that
  height (`height: 2.15rem`, `padding: 0 1.6rem 0 0.7rem`, `width: auto`, `min-width: 5rem`,
  `font-size: 0.82rem`) — see `.wb-ban-duration`. Pico forces `select { width: 100% }`, so a
  class override is mandatory or the combo overflows the cell and overlaps the next column.
- Action columns are right-aligned by default. Left-align them only when the column header is
  left-aligned and you want the control under the header text (the admin Users table uses
  `.wb-users-table .wb-cell-actions { text-align: left }`).
- A `<button>` or `<select>` in an action cell must carry `margin: 0`. Pico adds
  `margin-bottom: var(--pico-spacing)` (~19px) to real form controls but not to `<a>`, so a
  `<button class="wb-btn">` (Delete, Ban) rendered about 10px higher than an
  `<a class="wb-btn">` (Edit) in the neighbouring cell. The base `button, .wb-btn, [role="button"]`
  rule and `.wb-ban-duration` reset the margin. Do not try to fix this with `vertical-align` or
  `line-height`; the offset is the phantom margin, not the alignment.

## Related gotchas

- A `<label for="X">` must match the id the picker macro emits, which is hyphenated
  (`class-time`, `booking-opens-at`), not the underscore i18n key.
- Never write literal `<input>` or `<input type="date">` text inside Jinja `{# #}`
  comments. The Edge Tools axe scan parses that comment text as real unlabeled form
  elements and reports phantom "Form elements must have labels" errors.
