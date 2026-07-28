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
  `display: inline-flex; white-space: nowrap; gap: 0.35rem`, with no `min-width`
  and no stretch. Buttons are as wide as their content.
- Inline and in-table action buttons use the compact selector
  `.wb-cell-actions .wb-btn` (padding `0.35rem 0.7rem`, font `0.82rem`).
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

## Related gotchas

- A `<label for="X">` must match the id the picker macro emits, which is hyphenated
  (`class-time`, `booking-opens-at`), not the underscore i18n key.
- Never write literal `<input>` or `<input type="date">` text inside Jinja `{# #}`
  comments. The Edge Tools axe scan parses that comment text as real unlabeled form
  elements and reports phantom "Form elements must have labels" errors.
