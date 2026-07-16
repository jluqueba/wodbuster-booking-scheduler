"""String catalog for English + Spanish (US i18n).

Two flat dicts keyed by dotted namespaces. Kept as Python literals
(no gettext, no ``.po`` files) because the app is single-operator
and adding real Babel infrastructure would triple the maintenance
surface for one locale. Both dicts share the same keys — a missing
key in ``ES`` is a caller mistake that lints show up as a runtime
fallback (see :func:`wodbuster_worker.i18n.t`).

Conventions:

- Keys use dotted namespaces (``dashboard.title``, ``nav.rules``,
  ``flash.telegram.test_sent``). One namespace per feature area.
- Values may contain ``{placeholder}`` markers; :func:`t` calls
  ``.format(**kwargs)`` so callers must pass matching keyword args
  or the call falls back to the raw template string.
- Copy is deliberately terse; UX text is UI, not documentation.
"""

from __future__ import annotations

from typing import Final

DEFAULT_LANG: Final = "en"
SUPPORTED_LANGUAGES: Final = ("en", "es")


EN: dict[str, str] = {
    # -- common ------------------------------------------------------
    "common.save": "Save",
    "common.cancel": "Cancel",
    "common.confirm": "Confirm",
    "common.delete": "Delete",
    "common.edit": "Edit",
    "common.generate": "Generate",
    "common.unbind": "Unbind",
    "common.close": "Close",
    "common.back": "Back",
    "common.loading": "Loading…",
    "common.optional": "optional",
    "common.required": "required",
    "common.language": "Language",
    "common.language.en": "English",
    "common.language.es": "Español",
    # -- chips -------------------------------------------------------
    "chip.active": "active",
    "chip.paused": "paused",
    "chip.upcoming": "upcoming",
    "chip.bound": "bound",
    "chip.not_bound": "not bound",
    "chip.granted": "granted",
    "chip.scheduled": "scheduled",
    "chip.full": "full",
    "chip.cancelled": "cancelled",
    "chip.skipped": "skipped",
    "chip.cookie_invalid": "cookie invalid",
    "chip.class_not_visible": "class not visible",
    "chip.upstream_unavailable": "upstream unavailable",
    # -- nav ---------------------------------------------------------
    "nav.dashboard": "🏠 Dashboard",
    "nav.rules": "📅 Rules",
    "nav.history": "📜 History",
    "nav.vacation": "🏖️ Vacation",
    "nav.cookie": "🍪 Cookie",
    "nav.telegram": "🤖 Telegram",
    "nav.faq": "❓ FAQ",
    "nav.logout": "Log out",
    # -- dashboard ---------------------------------------------------
    "dashboard.eyebrow": "Welcome back",
    "dashboard.title.hero": "Hero",
    "dashboard.title.emoji": "💪",
    "dashboard.subtitle": (
        "Everything below drives your booking automation. Rules define "
        "when, the cookie proves who, the heartbeat catches issues "
        "before they become a missed class."
    ),
    "dashboard.countdown.label": "Next booking window opens in",
    "dashboard.countdown.firing": (
        "Firing now — refresh in a few seconds to see the outcome on History."
    ),
    "dashboard.countdown.empty.label": "No upcoming booking",
    "dashboard.countdown.empty.hint": "Add a rule to schedule your first automatic booking.",
    "dashboard.cards.rules.title": "📅 Rules",
    "dashboard.cards.rules.body": "Manage your recurring weekly bookings and preference chains.",
    "dashboard.cards.cookie.title": "🍪 Cookie",
    "dashboard.cards.cookie.body": (
        "Paste or refresh the .WBAuth value the worker uses to authenticate against WodBuster."
    ),
    "dashboard.cards.history.title": "📜 History",
    "dashboard.cards.history.body": (
        "Recent booking attempts, with a one-tap cancel for any upcoming granted class."
    ),
    "dashboard.cards.vacation.title": "🏖️ Vacation",
    "dashboard.cards.vacation.body": (
        "Enable a date range to bulk-cancel granted bookings and pause "
        "automatic booking until you're back."
    ),
    "dashboard.cards.telegram.title": "🤖 Telegram",
    "dashboard.cards.telegram.body": (
        "Bind your Telegram chat so booking outcomes, cookie-expiring "
        "warnings, and anomaly alerts land on your phone."
    ),
    # -- rules -------------------------------------------------------
    "rules.eyebrow": "Automation",
    "rules.title": "📅 Rules",
    "rules.subtitle": (
        "Weekly bookings on autopilot. Pick your days, pick your class, "
        "tell the worker when the reservation window opens."
    ),
    "rules.new_button": "➕ New rule",
    "rules.empty.title": "✨ No rules yet",
    "rules.empty.body": "Create one to start automating bookings.",
    "rules.table.attend": "Attend",
    "rules.table.primary": "Primary class",
    "rules.table.second_shot": "Second shot",
    "rules.table.window": "Window opens",
    "rules.table.status": "Status",
    "rules.actions.edit": "✏️ Edit",
    "rules.actions.delete": "🗑️ Delete",
    "rules.confirm.delete": "Delete this rule?",
    "rules.back_to_rules": "← Back to rules",
    "rules.form.attendance_days": "Attendance days",
    "rules.form.attendance_days_hint": (
        "Pick every day of the week you want to attend. One rule row "
        "is created per day — edit each row later to tweak just that day."
    ),
    "rules.form.attendance_day": "Attendance day",
    "rules.form.primary_class": "Primary class",
    "rules.form.class_type": "Class type",
    "rules.form.class_time": "Class time",
    "rules.form.booking_window": "Booking window",
    "rules.form.days_before": "Days before class",
    "rules.form.opens_at": "Opens at",
    "rules.form.window_example": (
        "Example: attend Wednesday, opens 3 days before at 22:40 → the "
        "worker fires Sunday at 22:40."
    ),
    "rules.form.second_shot": "Second shot (optional)",
    "rules.form.second_shot_type": "Alternative class type",
    "rules.form.second_shot_time": "Alternative time",
    "rules.form.second_shot_hint": (
        "Tried only when the primary class fills up before the worker "
        "secures a spot. Leave both blank if you have no alternative."
    ),
    "rules.form.picker_unavailable": (
        "Live class list unavailable. Paste a fresh cookie before "
        "saving — the class-type and time dropdowns are seeded from "
        "your WodBuster schedule."
    ),
    "rules.form.not_in_schedule": "{name} (not in current schedule)",
    "rules.form.create_button": "Create rule",
    "rules.form.save_button": "Save changes",
    "rules.form.delete_button": "🗑️ Delete rule",
    # -- history / upcoming -----------------------------------------
    "history.eyebrow": "Activity",
    "history.title": "📜 Booking history",
    "history.subtitle": (
        "Every booking attempt the worker has made, newest first. Use the "
        "Cancel button on any upcoming granted class to release your slot "
        "(this also updates WodBuster and pushes a Telegram notification)."
    ),
    "history.empty.title": "🕓 No bookings yet",
    "history.empty.body": (
        "Once the scheduler fires against one of your rules, the outcome will show up here."
    ),
    "history.upcoming.title": "🗓️ Upcoming bookings",
    "history.upcoming.empty": (
        "No granted or scheduled bookings on the horizon. Create a rule "
        "to start automating attendance."
    ),
    "history.attempts.title": "📜 All attempts",
    "history.table.when": "When",
    "history.table.class": "Class",
    "history.table.result": "Result",
    "history.table.attempted": "Attempted",
    "history.second_shot_tag": "(second shot)",
    "history.cancel_button": "🚫 Cancel",
    "history.confirm.cancel": "Cancel this booking on WodBuster?",
    # -- cookie ------------------------------------------------------
    "cookie.eyebrow": "Access",
    "cookie.title": "🍪 WodBuster cookie",
    "cookie.subtitle": (
        "Paste the .WBAuth cookie value the worker uses to authenticate "
        "against WodBuster. The worker encrypts it at rest and probes it "
        "hourly."
    ),
    "cookie.paste.title": "Paste a fresh cookie",
    "cookie.hint": ("Extract it in devtools: Application → Cookies → .wodbuster.com → .WBAuth."),
    "cookie.paste_button": "Validate and save",
    "cookie.status.empty": "No cookie on file yet. Paste one below to enable booking.",
    "cookie.status.pasted": "Pasted",
    "cookie.status.last_validated": "Last validated",
    "cookie.status.projected_expiry": "Projected expiry",
    "cookie.status.awaiting_first_heartbeat": "awaiting first heartbeat",
    "cookie.status.last_probe": "Last probe",
    "cookie.status.unknown": "unknown",
    # -- vacation ----------------------------------------------------
    "vacation.eyebrow": "Automation",
    "vacation.title": "🏖️ Vacation mode",
    "vacation.subtitle": (
        "Away from the gym? Enable vacation mode for a date range and "
        "the worker cancels every granted booking inside it, then pauses "
        "automatic booking until the range ends."
    ),
    "vacation.form.start": "Start",
    "vacation.form.end": "End (inclusive)",
    "vacation.enable_button": "➕ Enable vacation",
    "vacation.empty.title": "☀️ No vacation windows",
    "vacation.empty.body": (
        "Pick a start and end date above to schedule your first holiday. "
        "Granted bookings inside the range will be cancelled and the "
        "scheduler will skip runs until the range ends."
    ),
    "vacation.table.start": "Start",
    "vacation.table.end": "End",
    "vacation.table.status": "Status",
    "vacation.actions.end_early": "⏹️ End early",
    "vacation.confirm.close": "End this vacation window now?",
    # -- telegram ----------------------------------------------------
    "telegram.eyebrow": "Notifications",
    "telegram.title": "🤖 Telegram bot",
    "telegram.subtitle": (
        "Bind a Telegram chat to your operator profile and every booking "
        "outcome, cookie-expiring warning, and anomaly alert lands on your "
        "phone alongside the Healthchecks watchdog."
    ),
    "telegram.chat_id_label": "Chat id {chat_id}",
    "telegram.bound.hint": (
        "Notifications are being delivered to this chat. Click Send test "
        "to verify the pipeline end-to-end. Unbind if you stopped using "
        "this Telegram account or want to bind a different chat."
    ),
    "telegram.send_test_button": "🧪 Send test message",
    "telegram.unbind_button": "🚫 Unbind",
    "telegram.confirm.unbind": "Unbind Telegram from this operator?",
    "telegram.generate.hint": (
        "Click below to generate a one-shot binding link (valid for 10 minutes)."
    ),
    "telegram.generate_button": "🔗 Generate link",
    "telegram.link_ready.hint": (
        "One-shot link generated. Tap it on the same device where you use "
        "Telegram, then send the pre-filled /start message to the bot. "
        "Refresh this page after and the chip flips to bound."
    ),
    "telegram.link_button": "📱 Open bot in Telegram",
    "telegram.token.hint": ("Or copy this raw token and DM it to the bot as /start <token>:"),
    "telegram.token.ttl": "Token expires in 10 minutes and can only be used once.",
    "telegram.no_bot_username": (
        "The server does not know the bot username yet. Make sure "
        "telegram-bot-token is set in Key Vault and the container has "
        "been restarted since it was seeded."
    ),
    # -- landing -----------------------------------------------------
    "landing.hero.eyebrow": "🏋️ Booking on autopilot",
    "landing.hero.title_pre": "Never miss a ",
    "landing.hero.title_accent": "WOD",
    "landing.hero.title_post": ".",
    "landing.hero.subtitle": (
        "Set a rule once. Paste a cookie. The worker grabs your class the "
        "moment booking opens and pings your phone when it needs you."
    ),
    "landing.cards.rules.title": "📅 Recurring rules",
    "landing.cards.rules.body": (
        "One rule per day-of-week with a preference chain of class types. "
        "Rule changes take effect on the next window."
    ),
    "landing.cards.cookie.title": "💓 Cookie heartbeat",
    "landing.cards.cookie.body": (
        "Hourly probe against WodBuster. Projects expiry, alerts you "
        "24 hours before the next booking window if the cookie is "
        "about to die."
    ),
    "landing.cards.notifications.title": "🔔 Dual-channel notifications",
    "landing.cards.notifications.body": (
        "Every outcome shows up as a banner in-app and a message on "
        "Telegram. Never surprise-fail on a Monday."
    ),
    # -- auth --------------------------------------------------------
    "auth.landing.title": "WodBuster Booking Scheduler",
    "auth.denied.title": "🚫 Access denied",
    "auth.denied.body": ("This account is not authorized to access the WodBuster Booking Worker."),
    "auth.denied.contact": (
        "If you believe this is a mistake, contact the operator who set up this deployment."
    ),
    "auth.denied.back": "← Back to sign-in",
    "auth.signin.with_microsoft": "🪟 Sign in with Microsoft",
    "auth.signin.with_github": "🐙 Sign in with GitHub",
    "auth.signin.with_google": "🌐 Sign in with Google",
    # -- faq ---------------------------------------------------------
    "faq.eyebrow": "Help",
    "faq.title": "❓ Frequently asked questions",
    "faq.subtitle": (
        "Everything you need to run bookings on autopilot. Tap a question to expand it."
    ),
    "faq.section.getting_started": "Getting started",
    "faq.section.cookie": "Cookie",
    "faq.section.rules": "Rules",
    "faq.section.history": "History & cancel",
    "faq.section.vacation": "Vacation mode",
    "faq.section.notifications": "Notifications",
    "faq.section.telegram": "Telegram",
    "faq.section.troubleshooting": "Troubleshooting",
    "faq.q.what_is_app": "What is this app?",
    "faq.a.what_is_app": (
        "A background worker that books your WodBuster classes the moment the reservation "
        "window opens. You configure your weekly schedule once (Rules), keep a valid session "
        "cookie on file (Cookie), and the app fires the booking on your behalf. Every attempt "
        "is logged on the History page."
    ),
    "faq.q.first_booking": "How do I make my first booking?",
    "faq.a.first_booking": (
        "Three steps: (1) paste a fresh <code>.WBAuth</code> cookie on the "
        "<a href='{cookie_url}'>Cookie</a> page, (2) create a rule on the "
        "<a href='{rules_url}'>Rules</a> page describing which class you attend, at what time, "
        "and when WodBuster opens the reservation window for it, (3) wait — the scheduler "
        "fires automatically at the window-open instant."
    ),
    "faq.q.cookie_source": "Where do I get the cookie value from?",
    "faq.a.cookie_source": (
        "Log in to WodBuster normally in your browser, open the developer tools (F12), go to "
        "the Application (or Storage) tab, expand Cookies for the gym subdomain, and copy the "
        "value of the cookie named <code>.WBAuth</code>. Paste it into the Cookie page here."
    ),
    "faq.q.cookie_refresh": "How often do I need to refresh the cookie?",
    "faq.a.cookie_refresh": (
        "WodBuster's session cookie lives for about 30 days. The app checks it hourly and "
        "pushes a banner + Telegram alert 24 h before the projected expiry so you have time "
        "to paste a fresh one without missing a booking window."
    ),
    "faq.q.cookie_rejected": "The dashboard says 'Cookie rejected'. What now?",
    "faq.a.cookie_rejected": (
        "WodBuster refused the stored cookie mid-flight — usually because you logged out from "
        "the website, or the session was invalidated remotely. Grab a fresh cookie from the "
        "browser and paste it. The alert closes automatically on the next successful heartbeat."
    ),
    "faq.q.what_is_rule": "What is a rule?",
    "faq.a.what_is_rule": (
        "A recurring weekly booking. It says: on this day of the week, book this class type "
        "at this time. The app also asks how many days before the class WodBuster opens the "
        "reservation window and at what clock time — this is what the scheduler uses to fire "
        "the booking at the right instant."
    ),
    "faq.q.second_shot": "What is the 'second shot'?",
    "faq.a.second_shot": (
        "An optional fallback. If the primary class is already full when the worker tries to "
        "book it, the second shot is a different class type or time to attempt as a backup. "
        "Leave it blank if you have no alternative."
    ),
    "faq.q.multi_day": "Can I book multiple days from one form?",
    "faq.a.multi_day": (
        "Yes — pick every attendance day in the day pills and the create form fans out into "
        "one rule per selected day. Edit each row afterwards to tweak a specific day."
    ),
    "faq.q.empty_dropdown": "The class-type dropdown is empty. Why?",
    "faq.a.empty_dropdown": (
        "The picker is seeded from a live WodBuster call. If it is empty, the cookie is "
        "missing, invalid, or the upstream call failed. Paste a fresh cookie and refresh. If "
        "it stays empty after that, hit <code>/rules/api/classes/debug</code> in your browser "
        "— the JSON response shows what the picker sees."
    ),
    "faq.q.how_cancel": "How do I cancel a booking?",
    "faq.a.how_cancel": (
        "Go to the <a href='{history_url}'>History</a> page, find the booking (it must be "
        "granted and its class start must still be in the future), and tap Cancel. The app "
        "calls WodBuster, flips the row to <em>cancelled</em>, and sends a Telegram "
        "notification."
    ),
    "faq.q.cancel_twice": "What happens if I tap Cancel twice?",
    "faq.a.cancel_twice": (
        "The second tap is a no-op. The app detects the row is already cancelled and shows "
        "'Already cancelled' without calling WodBuster again."
    ),
    "faq.q.no_cancel_button": "Why do some booked classes have no Cancel button?",
    "faq.a.no_cancel_button": (
        "Cancel is only shown for rows that are <em>granted</em> and whose class start is in "
        "the future. Past bookings, full outcomes, and rows already cancelled cannot be "
        "cancelled from the app."
    ),
    "faq.q.vacation_what": "What is vacation mode?",
    "faq.a.vacation_what": (
        "Vacation mode pauses your automation for a date range. While it is active the worker "
        "stops firing new bookings, so you will not grab classes you cannot attend while you "
        "are away."
    ),
    "faq.q.vacation_enable": "How do I enable vacation mode?",
    "faq.a.vacation_enable": (
        "Open the <a href='{vacation_url}'>Vacation</a> page, pick a start and end date, and "
        "enable it. You can turn it off early at any time — automation resumes for any window "
        "that has not opened yet."
    ),
    "faq.q.vacation_bookings": "What happens to classes I already booked?",
    "faq.a.vacation_bookings": (
        "Enabling a vacation range bulk-cancels the granted bookings that fall inside it and "
        "notifies you, so you free the spots for other athletes. Bookings outside the range "
        "are left untouched."
    ),
    "faq.q.where_notifications": "Where do notifications go?",
    "faq.a.where_notifications": (
        "Every mutating event (booking granted, booking failed, cookie expiring, cookie "
        "rejected) produces a banner on the dashboard <em>and</em> a Telegram message — as "
        "long as you have registered your Telegram chat id against your operator profile. "
        "Web-only alerts still work if Telegram is not wired."
    ),
    "faq.q.telegram_why": "Why connect Telegram?",
    "faq.a.telegram_why": (
        "Telegram is the on-the-go channel. Once linked, every booking outcome, "
        "cookie-expiring warning, and anomaly alert lands on your phone, and you can run "
        "quick actions (cancel a class, check the next booking) without opening the web UI."
    ),
    "faq.q.telegram_setup": "How do I set up Telegram?",
    "faq.a.telegram_setup": (
        "Open the <a href='{telegram_url}'>Telegram</a> page and follow the bind flow: start "
        "a chat with the bot, send it the one-time code shown on the page, and the app links "
        "that chat to your operator profile. Once bound, the page shows a <em>bound</em> chip "
        "and a test-message button."
    ),
    "faq.q.telegram_unbind": "How do I stop Telegram notifications?",
    "faq.a.telegram_unbind": (
        "Open the <a href='{telegram_url}'>Telegram</a> page and tap Unbind. The app forgets "
        "your chat id and falls back to web-only banners until you bind again."
    ),
    "faq.q.scheduler_no_fire": "The scheduler did not fire at the expected time.",
    "faq.a.scheduler_no_fire": (
        "Check the History page: if the row is there with a non-granted outcome (full, "
        "class-not-visible, upstream-unavailable), the scheduler tried but WodBuster refused. "
        "If no row exists at all, the scheduler did not fire — usually because the rule is "
        "inactive, the cookie is missing, or the container restarted moments before the "
        "window and did not re-register the job."
    ),
    "faq.q.different_provider": "I want to sign in from a different provider.",
    "faq.a.different_provider": (
        "Log out, then hit the sign-in provider you want on the landing page. The app matches "
        "identities by subject id provided by the OAuth callback — an account you have not "
        "signed in with before will be rejected because the operator allow-list is "
        "single-user for now."
    ),
    # -- flash messages ---------------------------------------------
    "flash.booking.cancelled": "Booking cancelled. WodBuster and Telegram updated.",
    "flash.booking.already_cancelled": "Already cancelled — no action taken.",
    "flash.booking.cancel_failed": "Cancel failed: {reason}",
    "flash.booking.service_unavailable": (
        "Booking service unavailable — check WodBuster configuration."
    ),
    "flash.vacation.enabled": (
        "Vacation mode enabled from {start} through {end}. Granted "
        "bookings inside the range have been cancelled."
    ),
    "flash.vacation.closed": (
        "Vacation window closed. Automated bookings resume for future dates."
    ),
    "flash.vacation.invalid_date": ("Invalid date. Use YYYY-MM-DD for both start and end."),
    "flash.telegram.test_sent": "Test message sent. Check your Telegram chat.",
    "flash.telegram.unbound": "Telegram unbound.",
    "flash.telegram.no_token": (
        "Bot token not configured. Seed telegram-bot-token in Key Vault "
        "and restart the container app."
    ),
    "flash.telegram.not_bound": (
        "This operator is not bound to a Telegram chat yet. Generate a "
        "link above and tap it to bind first."
    ),
    "flash.telegram.permanent_error": (
        "Telegram refused the message: {reason}. Check the bot token "
        "and that the chat still exists."
    ),
    "flash.telegram.transient_error": (
        "Temporary Telegram error: {reason}. Try again in a moment."
    ),
    "flash.language.updated": "Language updated.",
}


# Spanish translations. Same keys as EN, same placeholders.
ES: dict[str, str] = {
    # -- common ------------------------------------------------------
    "common.save": "Guardar",
    "common.cancel": "Cancelar",
    "common.confirm": "Confirmar",
    "common.delete": "Borrar",
    "common.edit": "Editar",
    "common.generate": "Generar",
    "common.unbind": "Desvincular",
    "common.close": "Cerrar",
    "common.back": "Volver",
    "common.loading": "Cargando…",
    "common.optional": "opcional",
    "common.required": "obligatorio",
    "common.language": "Idioma",
    "common.language.en": "English",
    "common.language.es": "Español",
    # -- chips -------------------------------------------------------
    "chip.active": "activa",
    "chip.paused": "pausada",
    "chip.upcoming": "próximo",
    "chip.bound": "vinculado",
    "chip.not_bound": "no vinculado",
    "chip.granted": "reservado",
    "chip.scheduled": "programado",
    "chip.full": "completo",
    "chip.cancelled": "cancelado",
    "chip.skipped": "omitido",
    "chip.cookie_invalid": "cookie inválida",
    "chip.class_not_visible": "clase no visible",
    "chip.upstream_unavailable": "servicio no disponible",
    # -- nav ---------------------------------------------------------
    "nav.dashboard": "🏠 Panel",
    "nav.rules": "📅 Reglas",
    "nav.history": "📜 Historial",
    "nav.vacation": "🏖️ Vacaciones",
    "nav.cookie": "🍪 Cookie",
    "nav.telegram": "🤖 Telegram",
    "nav.faq": "❓ Ayuda",
    "nav.logout": "Cerrar sesión",
    # -- dashboard ---------------------------------------------------
    "dashboard.eyebrow": "Hola de nuevo",
    "dashboard.title.hero": "Crack",
    "dashboard.title.emoji": "💪",
    "dashboard.subtitle": (
        "Todo lo que hay debajo alimenta tu automatización de reservas. "
        "Las reglas definen cuándo, la cookie prueba quién eres, y el "
        "heartbeat detecta problemas antes de que pierdas una clase."
    ),
    "dashboard.countdown.label": "Próxima ventana de reserva en",
    "dashboard.countdown.firing": (
        "Ejecutando — refresca en unos segundos para ver el resultado en Historial."
    ),
    "dashboard.countdown.empty.label": "Sin reservas próximas",
    "dashboard.countdown.empty.hint": (
        "Añade una regla para programar tu primera reserva automática."
    ),
    "dashboard.cards.rules.title": "📅 Reglas",
    "dashboard.cards.rules.body": (
        "Gestiona tus reservas semanales recurrentes y las cadenas de preferencia."
    ),
    "dashboard.cards.cookie.title": "🍪 Cookie",
    "dashboard.cards.cookie.body": (
        "Pega o actualiza el valor .WBAuth que usa el worker para autenticarse contra WodBuster."
    ),
    "dashboard.cards.history.title": "📜 Historial",
    "dashboard.cards.history.body": (
        "Últimos intentos de reserva, con un botón para cancelar "
        "cualquier clase reservada que aún no haya empezado."
    ),
    "dashboard.cards.vacation.title": "🏖️ Vacaciones",
    "dashboard.cards.vacation.body": (
        "Activa un rango de fechas para cancelar en bloque las reservas "
        "concedidas y pausar la reserva automática hasta que vuelvas."
    ),
    "dashboard.cards.telegram.title": "🤖 Telegram",
    "dashboard.cards.telegram.body": (
        "Vincula tu chat de Telegram para recibir en el móvil los "
        "resultados de reservas, avisos de cookie caducada y anomalías."
    ),
    # -- rules -------------------------------------------------------
    "rules.eyebrow": "Automatización",
    "rules.title": "📅 Reglas",
    "rules.subtitle": (
        "Reservas semanales en piloto automático. Elige tus días, tu clase "
        "y cuándo se abre la ventana de reserva."
    ),
    "rules.new_button": "➕ Nueva regla",
    "rules.empty.title": "✨ Aún no hay reglas",
    "rules.empty.body": "Crea una para empezar a automatizar reservas.",
    "rules.table.attend": "Asistir",
    "rules.table.primary": "Clase principal",
    "rules.table.second_shot": "Alternativa",
    "rules.table.window": "Ventana abre",
    "rules.table.status": "Estado",
    "rules.actions.edit": "✏️ Editar",
    "rules.actions.delete": "🗑️ Borrar",
    "rules.confirm.delete": "¿Borrar esta regla?",
    "rules.back_to_rules": "← Volver a reglas",
    "rules.form.attendance_days": "Días de asistencia",
    "rules.form.attendance_days_hint": (
        "Elige cada día de la semana al que quieras ir. Se crea una "
        "regla por día — edita cada fila después para ajustar solo ese día."
    ),
    "rules.form.attendance_day": "Día de asistencia",
    "rules.form.primary_class": "Clase principal",
    "rules.form.class_type": "Tipo de clase",
    "rules.form.class_time": "Hora de clase",
    "rules.form.booking_window": "Ventana de reserva",
    "rules.form.days_before": "Días antes de la clase",
    "rules.form.opens_at": "Abre a las",
    "rules.form.window_example": (
        "Ejemplo: asistir el miércoles, abre 3 días antes a las 22:40 "
        "→ el worker se ejecuta el domingo a las 22:40."
    ),
    "rules.form.second_shot": "Alternativa (opcional)",
    "rules.form.second_shot_type": "Tipo de clase alternativa",
    "rules.form.second_shot_time": "Hora alternativa",
    "rules.form.second_shot_hint": (
        "Se intenta solo cuando la clase principal se llena antes de "
        "que el worker consiga plaza. Deja ambos en blanco si no "
        "tienes alternativa."
    ),
    "rules.form.picker_unavailable": (
        "Lista de clases no disponible. Pega una cookie fresca antes "
        "de guardar — los desplegables de tipo y hora se rellenan con "
        "tu horario de WodBuster."
    ),
    "rules.form.not_in_schedule": "{name} (no está en el horario actual)",
    "rules.form.create_button": "Crear regla",
    "rules.form.save_button": "Guardar cambios",
    "rules.form.delete_button": "🗑️ Borrar regla",
    # -- history / upcoming -----------------------------------------
    "history.eyebrow": "Actividad",
    "history.title": "📜 Historial de reservas",
    "history.subtitle": (
        "Cada intento de reserva del worker, del más reciente al más antiguo. "
        "Usa el botón Cancelar en cualquier clase reservada próxima para "
        "liberar tu plaza (actualiza también WodBuster y avisa por Telegram)."
    ),
    "history.empty.title": "🕓 Aún no hay reservas",
    "history.empty.body": (
        "Cuando el scheduler ejecute una de tus reglas, el resultado aparecerá aquí."
    ),
    "history.upcoming.title": "🗓️ Próximas reservas",
    "history.upcoming.empty": (
        "No hay reservas concedidas ni programadas en el horizonte. Crea "
        "una regla para empezar a automatizar."
    ),
    "history.attempts.title": "📜 Todos los intentos",
    "history.table.when": "Cuándo",
    "history.table.class": "Clase",
    "history.table.result": "Resultado",
    "history.table.attempted": "Intentado",
    "history.second_shot_tag": "(alternativa)",
    "history.cancel_button": "🚫 Cancelar",
    "history.confirm.cancel": "¿Cancelar esta reserva en WodBuster?",
    # -- cookie ------------------------------------------------------
    "cookie.eyebrow": "Acceso",
    "cookie.title": "🍪 Cookie de WodBuster",
    "cookie.subtitle": (
        "Pega el valor de la cookie .WBAuth que el worker usa para "
        "autenticarse contra WodBuster. Se guarda cifrada y se comprueba "
        "cada hora."
    ),
    "cookie.paste.title": "Pega una cookie nueva",
    "cookie.hint": ("Cópiala desde devtools: Application → Cookies → .wodbuster.com → .WBAuth."),
    "cookie.paste_button": "Validar y guardar",
    "cookie.status.empty": "Aún no hay cookie. Pega una debajo para activar las reservas.",
    "cookie.status.pasted": "Pegada",
    "cookie.status.last_validated": "Última validación",
    "cookie.status.projected_expiry": "Caducidad estimada",
    "cookie.status.awaiting_first_heartbeat": "esperando primer heartbeat",
    "cookie.status.last_probe": "Última comprobación",
    "cookie.status.unknown": "desconocido",
    # -- vacation ----------------------------------------------------
    "vacation.eyebrow": "Automatización",
    "vacation.title": "🏖️ Modo vacaciones",
    "vacation.subtitle": (
        "¿Fuera del gimnasio? Activa el modo vacaciones para un rango de "
        "fechas y el worker cancelará cada reserva concedida dentro y "
        "pausará las reservas automáticas hasta que termine el rango."
    ),
    "vacation.form.start": "Inicio",
    "vacation.form.end": "Fin (incluido)",
    "vacation.enable_button": "➕ Activar vacaciones",
    "vacation.empty.title": "☀️ Sin ventanas de vacaciones",
    "vacation.empty.body": (
        "Elige una fecha de inicio y fin arriba para programar tus "
        "primeras vacaciones. Las reservas concedidas dentro del rango "
        "se cancelarán y el scheduler saltará las ejecuciones hasta que "
        "termine el rango."
    ),
    "vacation.table.start": "Inicio",
    "vacation.table.end": "Fin",
    "vacation.table.status": "Estado",
    "vacation.actions.end_early": "⏹️ Terminar ya",
    "vacation.confirm.close": "¿Terminar esta ventana de vacaciones ahora?",
    # -- telegram ----------------------------------------------------
    "telegram.eyebrow": "Notificaciones",
    "telegram.title": "🤖 Bot de Telegram",
    "telegram.subtitle": (
        "Vincula un chat de Telegram a tu perfil y recibirás en el móvil "
        "cada resultado de reserva, aviso de cookie caducada y anomalía, "
        "junto con el watchdog de Healthchecks."
    ),
    "telegram.chat_id_label": "Chat id {chat_id}",
    "telegram.bound.hint": (
        "Las notificaciones se están entregando a este chat. Pulsa Enviar "
        "prueba para verificar el pipeline de extremo a extremo. Desvincula "
        "si has dejado de usar esta cuenta de Telegram o quieres vincular "
        "otro chat."
    ),
    "telegram.send_test_button": "🧪 Enviar mensaje de prueba",
    "telegram.unbind_button": "🚫 Desvincular",
    "telegram.confirm.unbind": "¿Desvincular Telegram de este operador?",
    "telegram.generate.hint": (
        "Pulsa abajo para generar un enlace de vinculación de un solo uso (válido 10 minutos)."
    ),
    "telegram.generate_button": "🔗 Generar enlace",
    "telegram.link_ready.hint": (
        "Enlace generado. Púlsalo en el mismo dispositivo donde usas "
        "Telegram y envía el mensaje /start prellenado al bot. Refresca "
        "esta página después y el chip cambiará a vinculado."
    ),
    "telegram.link_button": "📱 Abrir bot en Telegram",
    "telegram.token.hint": ("O copia este token y envíaselo al bot como /start <token>:"),
    "telegram.token.ttl": "El token caduca en 10 minutos y solo se puede usar una vez.",
    "telegram.no_bot_username": (
        "El servidor aún no sabe el nombre del bot. Comprueba que "
        "telegram-bot-token está en Key Vault y que el contenedor se "
        "reinició después de guardarlo."
    ),
    # -- landing -----------------------------------------------------
    "landing.hero.eyebrow": "🏋️ Reservas en piloto automático",
    "landing.hero.title_pre": "No te pierdas ningún ",
    "landing.hero.title_accent": "WOD",
    "landing.hero.title_post": ".",
    "landing.hero.subtitle": (
        "Define una regla una vez. Pega una cookie. El worker reserva tu "
        "clase en cuanto se abre la inscripción y te avisa al móvil cuando "
        "te necesita."
    ),
    "landing.cards.rules.title": "📅 Reglas recurrentes",
    "landing.cards.rules.body": (
        "Una regla por día de la semana con una cadena de preferencias de "
        "tipos de clase. Los cambios se aplican en la próxima ventana."
    ),
    "landing.cards.cookie.title": "💓 Latido de la cookie",
    "landing.cards.cookie.body": (
        "Sondeo cada hora contra WodBuster. Estima la caducidad y te avisa "
        "24 horas antes de la próxima ventana de reserva si la cookie está "
        "a punto de expirar."
    ),
    "landing.cards.notifications.title": "🔔 Notificaciones en dos canales",
    "landing.cards.notifications.body": (
        "Cada resultado aparece como aviso en la app y como mensaje en "
        "Telegram. Sin sustos de última hora un lunes."
    ),
    # -- auth --------------------------------------------------------
    "auth.landing.title": "WodBuster Booking Scheduler",
    "auth.denied.title": "🚫 Acceso denegado",
    "auth.denied.body": (
        "Esta cuenta no está autorizada para acceder al WodBuster Booking Worker."
    ),
    "auth.denied.contact": (
        "Si crees que es un error, contacta con la persona que configuró el despliegue."
    ),
    "auth.denied.back": "← Volver a iniciar sesión",
    "auth.signin.with_microsoft": "🪟 Entrar con Microsoft",
    "auth.signin.with_github": "🐙 Entrar con GitHub",
    "auth.signin.with_google": "🌐 Entrar con Google",
    # -- faq ---------------------------------------------------------
    "faq.eyebrow": "Ayuda",
    "faq.title": "❓ Preguntas frecuentes",
    "faq.subtitle": (
        "Todo lo que necesitas para reservar en piloto automático. Toca una pregunta para "
        "desplegarla."
    ),
    "faq.section.getting_started": "Primeros pasos",
    "faq.section.cookie": "Cookie",
    "faq.section.rules": "Reglas",
    "faq.section.history": "Historial y cancelaciones",
    "faq.section.vacation": "Modo vacaciones",
    "faq.section.notifications": "Notificaciones",
    "faq.section.telegram": "Telegram",
    "faq.section.troubleshooting": "Resolución de problemas",
    "faq.q.what_is_app": "¿Qué es esta aplicación?",
    "faq.a.what_is_app": (
        "Un worker en segundo plano que reserva tus clases de WodBuster en cuanto se abre la "
        "ventana de reserva. Configuras tu horario semanal una vez (Reglas), mantienes una "
        "cookie de sesión válida guardada (Cookie) y la aplicación hace la reserva por ti. "
        "Cada intento queda registrado en la página de Historial."
    ),
    "faq.q.first_booking": "¿Cómo hago mi primera reserva?",
    "faq.a.first_booking": (
        "Tres pasos: (1) pega una cookie <code>.WBAuth</code> reciente en la página de "
        "<a href='{cookie_url}'>Cookie</a>, (2) crea una regla en la página de "
        "<a href='{rules_url}'>Reglas</a> indicando a qué clase asistes, a qué hora y cuándo "
        "abre WodBuster la ventana de reserva, (3) espera: el planificador se dispara "
        "automáticamente en el instante en que se abre la ventana."
    ),
    "faq.q.cookie_source": "¿De dónde saco el valor de la cookie?",
    "faq.a.cookie_source": (
        "Inicia sesión en WodBuster normalmente en tu navegador, abre las herramientas de "
        "desarrollador (F12), ve a la pestaña Aplicación (o Almacenamiento), despliega las "
        "Cookies del subdominio del box y copia el valor de la cookie llamada "
        "<code>.WBAuth</code>. Pégalo en la página de Cookie de aquí."
    ),
    "faq.q.cookie_refresh": "¿Cada cuánto tengo que renovar la cookie?",
    "faq.a.cookie_refresh": (
        "La cookie de sesión de WodBuster dura unos 30 días. La aplicación la comprueba cada "
        "hora y te muestra un aviso + alerta de Telegram 24 h antes de la caducidad prevista "
        "para que te dé tiempo a pegar una nueva sin perder ninguna ventana de reserva."
    ),
    "faq.q.cookie_rejected": "El panel dice «Cookie rechazada». ¿Y ahora qué?",
    "faq.a.cookie_rejected": (
        "WodBuster rechazó la cookie guardada a mitad de una operación, normalmente porque "
        "cerraste sesión en la web o la sesión se invalidó de forma remota. Consigue una "
        "cookie nueva desde el navegador y pégala. La alerta se cierra sola en el siguiente "
        "latido correcto."
    ),
    "faq.q.what_is_rule": "¿Qué es una regla?",
    "faq.a.what_is_rule": (
        "Una reserva semanal recurrente. Dice: este día de la semana, reserva este tipo de "
        "clase a esta hora. La aplicación también pregunta cuántos días antes de la clase "
        "abre WodBuster la ventana de reserva y a qué hora exacta: eso es lo que usa el "
        "planificador para disparar la reserva en el momento justo."
    ),
    "faq.q.second_shot": "¿Qué es el «segundo intento»?",
    "faq.a.second_shot": (
        "Una alternativa opcional. Si la clase principal ya está llena cuando el worker "
        "intenta reservarla, el segundo intento es otro tipo de clase u hora que probar como "
        "respaldo. Déjalo en blanco si no tienes alternativa."
    ),
    "faq.q.multi_day": "¿Puedo reservar varios días desde un mismo formulario?",
    "faq.a.multi_day": (
        "Sí: elige todos los días de asistencia en las pastillas de días y el formulario de "
        "creación genera una regla por cada día seleccionado. Edita luego cada fila para "
        "ajustar un día concreto."
    ),
    "faq.q.empty_dropdown": "El desplegable de tipo de clase está vacío. ¿Por qué?",
    "faq.a.empty_dropdown": (
        "El selector se rellena con una llamada en vivo a WodBuster. Si está vacío, la cookie "
        "falta, no es válida o la llamada falló. Pega una cookie nueva y recarga. Si sigue "
        "vacío, abre <code>/rules/api/classes/debug</code> en tu navegador: la respuesta JSON "
        "muestra lo que ve el selector."
    ),
    "faq.q.how_cancel": "¿Cómo cancelo una reserva?",
    "faq.a.how_cancel": (
        "Ve a la página de <a href='{history_url}'>Historial</a>, busca la reserva (debe "
        "estar concedida y su inicio de clase debe seguir en el futuro) y toca Cancelar. La "
        "aplicación llama a WodBuster, cambia la fila a <em>cancelada</em> y envía una "
        "notificación de Telegram."
    ),
    "faq.q.cancel_twice": "¿Qué pasa si toco Cancelar dos veces?",
    "faq.a.cancel_twice": (
        "El segundo toque no hace nada. La aplicación detecta que la fila ya está cancelada y "
        "muestra «Ya cancelada» sin volver a llamar a WodBuster."
    ),
    "faq.q.no_cancel_button": "¿Por qué algunas clases reservadas no tienen botón de Cancelar?",
    "faq.a.no_cancel_button": (
        "Cancelar solo aparece en las filas <em>concedidas</em> cuyo inicio de clase está en "
        "el futuro. Las reservas pasadas, los resultados llenos y las filas ya canceladas no "
        "se pueden cancelar desde la aplicación."
    ),
    "faq.q.vacation_what": "¿Qué es el modo vacaciones?",
    "faq.a.vacation_what": (
        "El modo vacaciones pausa tu automatización durante un rango de fechas. Mientras está "
        "activo, el worker deja de lanzar nuevas reservas, así no coges clases a las que no "
        "puedes asistir mientras estás fuera."
    ),
    "faq.q.vacation_enable": "¿Cómo activo el modo vacaciones?",
    "faq.a.vacation_enable": (
        "Abre la página de <a href='{vacation_url}'>Vacaciones</a>, elige una fecha de inicio "
        "y de fin, y actívalo. Puedes desactivarlo antes en cualquier momento: la "
        "automatización se reanuda para cualquier ventana que aún no se haya abierto."
    ),
    "faq.q.vacation_bookings": "¿Qué pasa con las clases que ya tenía reservadas?",
    "faq.a.vacation_bookings": (
        "Al activar un rango de vacaciones se cancelan en bloque las reservas concedidas que "
        "caen dentro y se te notifica, para que liberes las plazas para otros atletas. Las "
        "reservas fuera del rango no se tocan."
    ),
    "faq.q.where_notifications": "¿Dónde llegan las notificaciones?",
    "faq.a.where_notifications": (
        "Cada evento que cambia algo (reserva concedida, reserva fallida, cookie por caducar, "
        "cookie rechazada) genera un aviso en el panel <em>y</em> un mensaje de Telegram, "
        "siempre que hayas registrado tu chat de Telegram en tu perfil de operador. Los "
        "avisos solo web siguen funcionando aunque Telegram no esté conectado."
    ),
    "faq.q.telegram_why": "¿Para qué conectar Telegram?",
    "faq.a.telegram_why": (
        "Telegram es el canal para cuando estás fuera. Una vez enlazado, cada resultado de "
        "reserva, aviso de cookie por caducar y alerta de anomalía llega a tu móvil, y puedes "
        "hacer acciones rápidas (cancelar una clase, consultar la próxima reserva) sin abrir "
        "la interfaz web."
    ),
    "faq.q.telegram_setup": "¿Cómo configuro Telegram?",
    "faq.a.telegram_setup": (
        "Abre la página de <a href='{telegram_url}'>Telegram</a> y sigue el flujo de enlace: "
        "inicia un chat con el bot, envíale el código de un solo uso que se muestra en la "
        "página y la aplicación enlaza ese chat con tu perfil de operador. Una vez enlazado, "
        "la página muestra una etiqueta <em>enlazado</em> y un botón de mensaje de prueba."
    ),
    "faq.q.telegram_unbind": "¿Cómo dejo de recibir notificaciones de Telegram?",
    "faq.a.telegram_unbind": (
        "Abre la página de <a href='{telegram_url}'>Telegram</a> y toca Desenlazar. La "
        "aplicación olvida tu chat y vuelve a los avisos solo web hasta que lo enlaces de "
        "nuevo."
    ),
    "faq.q.scheduler_no_fire": "El planificador no se disparó a la hora esperada.",
    "faq.a.scheduler_no_fire": (
        "Revisa la página de Historial: si la fila está ahí con un resultado no concedido "
        "(llena, clase no visible, servicio no disponible), el planificador lo intentó pero "
        "WodBuster lo rechazó. Si no existe ninguna fila, el planificador no llegó a "
        "dispararse, normalmente porque la regla está inactiva, falta la cookie o el "
        "contenedor se reinició justo antes de la ventana y no volvió a registrar el trabajo."
    ),
    "faq.q.different_provider": "Quiero iniciar sesión con otro proveedor.",
    "faq.a.different_provider": (
        "Cierra sesión y pulsa el proveedor que quieras en la página de inicio. La aplicación "
        "identifica las cuentas por el subject id que envía el callback de OAuth: una cuenta "
        "con la que no hayas iniciado sesión antes será rechazada porque la lista de "
        "operadores permitidos es de un solo usuario por ahora."
    ),
    # -- flash messages ---------------------------------------------
    "flash.booking.cancelled": "Reserva cancelada. WodBuster y Telegram actualizados.",
    "flash.booking.already_cancelled": "Ya cancelada — sin acción.",
    "flash.booking.cancel_failed": "Fallo al cancelar: {reason}",
    "flash.booking.service_unavailable": (
        "Servicio de reservas no disponible — revisa la configuración de WodBuster."
    ),
    "flash.vacation.enabled": (
        "Modo vacaciones activado del {start} al {end}. Las reservas "
        "concedidas dentro del rango se han cancelado."
    ),
    "flash.vacation.closed": (
        "Ventana de vacaciones cerrada. Las reservas automáticas se reanudan para fechas futuras."
    ),
    "flash.vacation.invalid_date": ("Fecha inválida. Usa YYYY-MM-DD para inicio y fin."),
    "flash.telegram.test_sent": "Mensaje de prueba enviado. Revisa tu chat de Telegram.",
    "flash.telegram.unbound": "Telegram desvinculado.",
    "flash.telegram.no_token": (
        "Token del bot no configurado. Guarda telegram-bot-token en Key "
        "Vault y reinicia el contenedor."
    ),
    "flash.telegram.not_bound": (
        "Este operador aún no está vinculado a un chat de Telegram. "
        "Genera un enlace arriba y púlsalo para vincular primero."
    ),
    "flash.telegram.permanent_error": (
        "Telegram rechazó el mensaje: {reason}. Comprueba el token del "
        "bot y que el chat sigue existiendo."
    ),
    "flash.telegram.transient_error": (
        "Error temporal de Telegram: {reason}. Inténtalo de nuevo en un momento."
    ),
    "flash.language.updated": "Idioma actualizado.",
}


CATALOGS: dict[str, dict[str, str]] = {
    "en": EN,
    "es": ES,
}


__all__ = ["CATALOGS", "DEFAULT_LANG", "EN", "ES", "SUPPORTED_LANGUAGES"]
