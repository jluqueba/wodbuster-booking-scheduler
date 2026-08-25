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
    "chip.vacation": "on vacation",
    "chip.modified": "modified",
    "chip.skipped_day": "will be skipped",
    "chip.full": "full",
    "chip.cancelled": "cancelled",
    "chip.skipped": "skipped",
    "chip.cookie_invalid": "cookie invalid",
    "chip.class_not_visible": "class not visible",
    "chip.upstream_unavailable": "upstream unavailable",
    # Attempt lineage, orthogonal to the terminal status chip above
    # (ADR-0012 Decision 4): which plan drove the attempt, not how it ended.
    "chip.source.override": "modified day",
    "chip.source.override_fallback": "substituted",
    "chip.source.override_skip": "skipped by you",
    # -- nav ---------------------------------------------------------
    "nav.dashboard": "🏠 Dashboard",
    "nav.rules": "📅 Rules",
    "nav.history": "📜 History",
    "nav.vacation": "🏖️ Vacation",
    "nav.cookie": "🍪 Cookie",
    "nav.telegram": "🤖 Telegram",
    "nav.faq": "❓ FAQ",
    "nav.logout": "👋 Log out",
    "nav.gym_switch": "Switch gym",
    "gym.context.label": "🏋️ {name}",
    "gym.select.prompt": "Choose a gym from the selector above to act on it.",
    "modal.cancel": "🚫 Cancel",
    "modal.confirm": "✅ Confirm",
    # -- profile -----------------------------------------------------
    "nav.profile": "🙋 Profile",
    "nav.account": "Account menu",
    "nav.admin": "🛡️ Users",
    "admin.users.title": "🛡️ Users",
    "admin.users.subtitle": "Approve new users and manage access.",
    "admin.users.pending_title": "Pending requests",
    "admin.users.pending_empty": "No pending requests.",
    "admin.users.active_title": "Users",
    "admin.users.col.user": "User",
    "admin.users.col.email": "Email",
    "admin.users.col.provider": "Provider",
    "admin.users.col.role": "Role",
    "admin.users.col.status": "Status",
    "admin.users.col.access": "Access",
    "admin.users.col.remove": "Remove",
    "admin.users.approve": "✅ Approve",
    "admin.users.reject": "🚫 Reject",
    "admin.users.role.admin": "Admin",
    "admin.users.role.user": "User",
    "admin.users.you": "you",
    "admin.users.no_action": "",
    "admin.users.status.active": "Active",
    "admin.users.status.banned_indefinite": "Suspended (indefinite)",
    "admin.users.status.banned_until": "Suspended until",
    "admin.users.ban.duration_label": "Ban duration",
    "admin.users.ban.duration.1d": "1 day",
    "admin.users.ban.duration.7d": "7 days",
    "admin.users.ban.duration.30d": "30 days",
    "admin.users.ban.duration.indefinite": "Indefinite",
    "admin.users.ban.button": "🚫 Ban",
    "admin.users.unban.button": "✅ Un-ban",
    "admin.users.delete.button": "🗑️ Delete",
    "admin.users.ban_confirm": ("Ban this user? They will lose access until the ban expires."),
    "admin.users.delete_confirm": ("Delete this user and all their data? This cannot be undone."),
    "admin.notify.new_request": "🛡️ New access request from {name}. Review it in the app.",
    "profile.title": "Your profile",
    "profile.subtitle": "Manage how you appear and the language of your notifications.",
    "profile.display_name_label": "Display name",
    "profile.short_name_label": "Short name",
    "profile.short_name_placeholder": "Optional shorter label",
    "profile.language_label": "Communication language",
    "profile.lang.en": "English",
    "profile.lang.es": "Spanish",
    "profile.picture.alt": "Profile picture",
    "profile.picture.note": "Your photo comes from WodBuster. To change it, update it there.",
    "profile.save": "💾 Save profile",
    "profile.flash.saved": "Profile saved.",
    "profile.flash.name_required": "Display name cannot be empty.",
    "profile.flash.too_long": "Name is too long.",
    "profile.flash.bad_language": "Choose a supported language.",
    "profile.email_label": "Email address",
    "profile.email_placeholder": "you@example.com",
    "profile.email_prefs_label": "Email notifications",
    "profile.email_pref.bookings": "Booking results",
    "profile.email_pref.session_alerts": "Session and cookie alerts",
    "profile.email_prefs_note": (
        "Account emails (approval, rejection) are always sent, regardless of these settings."
    ),
    "profile.flash.bad_email": "That email address does not look valid.",
    # -- dashboard ---------------------------------------------------
    "dashboard.eyebrow": "Welcome back",
    "dashboard.title.hero": "Hero",
    "dashboard.title.emoji": "💪",
    "dashboard.subtitle": (
        "Everything below drives your booking automation. Rules define "
        "when, the cookie proves who, the heartbeat catches issues "
        "before they become a missed class."
    ),
    "dashboard.pending_requests": "You have {count} access request(s) to review.",
    "dashboard.countdown.label": "Next booking window opens in",
    "dashboard.countdown.firing": (
        "Firing now — refresh in a few seconds to see the outcome on History."
    ),
    "dashboard.countdown.empty.label": "No upcoming booking",
    "dashboard.countdown.empty.hint": "Add a rule to schedule your first automatic booking.",
    "dashboard.cards.profile.title": "🙋 Profile",
    "dashboard.cards.profile.body": (
        "Review your account details and choose the language and email "
        "preferences for your notifications."
    ),
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
    "rules.heading.new": "New rule",
    "rules.heading.edit": "Edit rule",
    "rules.empty.title": "✨ No rules yet",
    "rules.empty.body": "Create one to start automating bookings.",
    "rules.table.term": "Term",
    "rules.table.primary": "Primary Class",
    "rules.table.primary_hour": "Hour",
    "rules.table.second_shot": "Second Shot",
    "rules.table.second_shot_hour": "Second Shot Hour",
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
    "rules.form.create_button": "➕ Create rule",
    "rules.form.save_button": "💾 Save changes",
    "rules.form.delete_button": "🗑️ Delete rule",
    # -- history / upcoming -----------------------------------------
    "history.eyebrow": "Activity",
    "history.title": "📜 Booking history",
    "history.subtitle": (
        "This week's booking attempts, newest first. Use the "
        "Cancel button on any upcoming granted class to release your slot "
        "(this also updates WodBuster and pushes a Telegram notification)."
    ),
    "history.empty.title": "🕓 No attempts this week",
    "history.empty.body": (
        "Once the scheduler fires against one of your rules this week, the outcome will show up here."
    ),
    "history.upcoming.title": "🗓️ Upcoming bookings",
    "history.upcoming.empty": (
        "No granted or scheduled bookings on the horizon. Create a rule "
        "to start automating attendance."
    ),
    "history.upcoming.edit_day_aria": "Edit the booking day of {date}",
    "history.attempts.title": "📜 This week's attempts",
    "history.table.day": "Day",
    "history.table.date": "Date",
    "history.table.class": "Class",
    "history.table.result": "Result",
    "history.table.attempted": "Attempted",
    "history.second_shot_tag": "(second shot)",
    "history.cancel_button": "🚫 Cancel",
    "history.confirm.cancel": "Cancel this booking on WodBuster?",
    # -- single-day override (ADR-0012) ------------------------------
    "override.eyebrow": "Single day",
    "override.title": "✏️ Edit this day",
    "override.back_to_history": "← Back to history",
    "override.rule_values": "This day comes from a rule: {class_type} at {class_time}.",
    "override.rule_original": "Rule: {class_type} at {class_time}",
    "override.form.target": "Class for this day only",
    "override.form.class_type": "Class type",
    "override.form.class_time": "Class time",
    "override.form.target_hint": ("Applies to this date only. The weekly rule is not modified."),
    "override.form.second_shot": "Second shot",
    "override.form.second_shot_value": (
        "The rule's second shot still runs on this day: {class_type} at {class_time}."
    ),
    "override.form.second_shot_none": "This rule has no second shot.",
    "override.form.second_shot_clear": "Skip the second shot on this date only",
    "override.form.second_shot_clear_hint": (
        "The rule keeps its second shot for every other date."
    ),
    "override.warning.not_published": (
        "The gym has not published the schedule for this date yet. The "
        "options below are the combinations known for this weekday; the "
        "class will be re-checked when the booking window opens."
    ),
    "override.warning.probe_unavailable": (
        "Live class list unavailable, so the class cannot be checked "
        "against this date. You can still save; it will be re-checked "
        "when the booking window opens. Paste a fresh cookie:"
    ),
    "override.warning.not_validated": (
        "This day is not validated against a published schedule. If the "
        "class is unavailable when the window opens, the rule's class is "
        "booked instead."
    ),
    "override.error.combination_unavailable": (
        "That class does not run at that time on this date."
    ),
    "override.error.invalid_time": "Use HH:MM in 24-hour format.",
    "override.error.invalid_class_type": "Choose a class type.",
    "override.error.skip_exclusive": (
        "A skipped day carries no class: clear the class type and the class "
        "time, or save a class instead of skipping."
    ),
    "override.skip_hint": (
        "Or skip this day entirely: no booking is attempted and the weekly rule is not modified."
    ),
    "override.skip_active": (
        "This day is skipped. No booking will be attempted. Save a class "
        "above, or go back to the rule, to undo it."
    ),
    "override.edit_button": "✏️ Edit day",
    "override.save_button": "💾 Save this day",
    "override.skip_button": "🚫 Skip this day",
    "override.revert_button": "🚫 Back to rule",
    "override.confirm.revert": "Discard this day's change and go back to the rule?",
    "override.confirm.skip": "Skip this day? No booking will be attempted.",
    # -- cookie ------------------------------------------------------
    "cookie.eyebrow": "Access",
    "cookie.title": "🍪 WodBuster cookie",
    "cookie.subtitle": (
        "Paste the .WBAuth cookie value the worker uses to authenticate "
        "against WodBuster. The worker encrypts it at rest and probes it "
        "hourly."
    ),
    "cookie.all_gyms_note": (
        "One cookie covers every gym you can access on WodBuster. Pasting it "
        "here applies it to all your gyms and refreshes the list automatically."
    ),
    "cookie.paste.title": "Paste a fresh cookie",
    "cookie.hint": ("Extract it in devtools: Application → Cookies → .wodbuster.com → .WBAuth."),
    "cookie.paste_button": "💾 Validate and save",
    "cookie.status.empty": "No cookie on file yet. Paste one below to enable booking.",
    "cookie.status.pasted": "Pasted",
    "cookie.status.last_validated": "Last validated",
    "cookie.status.projected_expiry": "Projected expiry",
    "cookie.status.awaiting_first_heartbeat": "awaiting first heartbeat",
    "cookie.status.last_probe": "Last probe",
    "cookie.status.valid": "valid",
    "cookie.status.rejected": "rejected",
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
    "landing.cards.gyms.title": "🏢 Multiple gyms",
    "landing.cards.gyms.body": (
        "Book at more than one WodBuster gym from a single account. Your "
        "gyms appear automatically and one login covers them all, each "
        "booking independently."
    ),
    # -- auth --------------------------------------------------------
    "auth.landing.title": "WodBuster Booking Scheduler",
    "auth.denied.title": "🚫 Access denied",
    "auth.denied.body": (
        "This account is not authorized to access the WodBuster Booking Scheduler."
    ),
    "auth.denied.contact": (
        "If you believe this is a mistake, contact the operator who set up this deployment."
    ),
    "auth.denied.back": "⬅️ Back to sign-in",
    "auth.pending.title": "⏳ Request received",
    "auth.pending.body": "Your access request is awaiting approval by the administrator.",
    "auth.pending.hint": "You will be able to sign in here once it is approved.",
    "auth.pending.back": "⬅️ Back to sign-in",
    "auth.suspended.title": "⛔ Access suspended",
    "auth.suspended.body": "Your access has been suspended by the administrator.",
    "auth.suspended.back": "⬅️ Back to sign-in",
    # -- email unsubscribe (ADR-0011) --
    "unsubscribe.title": "Unsubscribe",
    "unsubscribe.ok.title": "✅ You're unsubscribed",
    "unsubscribe.ok.body": (
        "You will no longer receive booking or session-alert emails. You can turn them "
        "back on anytime from your profile."
    ),
    "unsubscribe.bad.title": "⚠️ Link not valid",
    "unsubscribe.bad.body": (
        "This unsubscribe link is invalid or has expired. Manage your email preferences "
        "from your profile instead."
    ),
    "unsubscribe.back": "⬅️ Back to sign-in",
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
    "faq.section.account": "Account and profile",
    "faq.section.cookie": "Cookie",
    "faq.section.gyms": "Gyms",
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
    "faq.q.pending_signup": "Why is my first sign-in waiting for approval?",
    "faq.a.pending_signup": (
        "A new OAuth identity creates a pending access request. An administrator must approve "
        "it before the account can use booking data or controls. The app emails you when the "
        "request is received and again when it is approved or rejected."
    ),
    "faq.q.profile_edit": "What can I change in my profile?",
    "faq.a.profile_edit": (
        "On the <a href='{profile_url}'>Profile</a> page you can edit your display name, "
        "optional short name, communication language, email address, and operational email "
        "preferences. Your profile picture comes from WodBuster and is read-only here."
    ),
    "faq.q.profile_language": "What does the communication language control?",
    "faq.a.profile_language": (
        "It controls your signed-in interface and the language used for Telegram and email "
        "notifications. Change it on the <a href='{profile_url}'>Profile</a> page; the new "
        "language takes effect as soon as you save."
    ),
    "faq.q.admin_difference": "What can an administrator do that a regular user cannot?",
    "faq.a.admin_difference": (
        "Regular users manage only their own gyms, rules, bookings, cookie, profile, and "
        "notification settings. Administrators can also review pending access requests and "
        "approve, reject, suspend, restore, or permanently delete non-administrator users. "
        "They cannot suspend or delete themselves or another administrator."
    ),
    "faq.q.access_suspended": "What happens if my access is suspended or deleted?",
    "faq.a.access_suspended": (
        "A suspended account cannot sign in until the administrator restores it or a timed "
        "suspension expires; its data remains stored. Deletion is permanent and removes the "
        "user together with their gyms, rules, history, cookies, and notification data."
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
    "faq.q.gyms_multiple": "Can I book at more than one gym?",
    "faq.a.gyms_multiple": (
        "Yes. Every WodBuster gym your account can access appears automatically in the gym "
        "selector, and one cookie authenticates all of them. Each gym still books, checks the "
        "shared cookie, and raises its own alerts independently."
    ),
    "faq.q.gyms_appear": "How do gyms get added?",
    "faq.a.gyms_appear": (
        "Automatically. When you paste a cookie on the <a href='{cookie_url}'>Cookie</a> page, "
        "and each time you sign in, the app asks WodBuster which gyms your account can access "
        "and adds any new ones. Switch between them from the selector in the top navigation."
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
        "calls WodBuster, flips the row to <em>cancelled</em>, and sends the configured "
        "notifications."
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
        "rejected) produces a dashboard banner. It also goes to Telegram when your chat is "
        "bound and to email when you have an address and the corresponding preference is "
        "enabled. Every message identifies the gym it concerns."
    ),
    "faq.q.email_preferences": "Which email notifications can I control?",
    "faq.a.email_preferences": (
        "The <a href='{profile_url}'>Profile</a> page has separate switches for booking "
        "results and session or cookie alerts. You can also edit the destination email "
        "address there. Changes apply to events produced after you save."
    ),
    "faq.q.email_unsubscribe": "How do I unsubscribe from email?",
    "faq.a.email_unsubscribe": (
        "Use the unsubscribe link in any operational email to turn off booking and session "
        "alert emails without signing in. You can enable either category again from the "
        "<a href='{profile_url}'>Profile</a> page. Telegram and dashboard banners are not "
        "affected."
    ),
    "faq.q.account_emails": "Why do account emails have no off switch?",
    "faq.a.account_emails": (
        "Messages confirming that an access request was received, approved, or rejected are "
        "transactional. They are always sent so the user can follow the access process, even "
        "after unsubscribing from operational email."
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
        "identities by the subject id provided by the OAuth callback. A provider identity "
        "that has not signed in before creates a separate access request and must be approved "
        "by an administrator before it can enter the app."
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
    "flash.override.saved": "Day updated. The weekly rule is unchanged.",
    "flash.override.skipped": "Day skipped. No booking will be attempted.",
    "flash.override.reverted": "Day back to the rule.",
    "flash.override.window_closed": (
        "The booking window for that day has already opened, so it can no "
        "longer be edited. Nothing was saved."
    ),
    "flash.override.already_executed": (
        "That day already ran. Check the result below. Nothing was saved."
    ),
    "flash.override.discarded": (
        "The rule now runs on a different weekday, so the single-day "
        "changes you had saved for {dates} no longer apply and have been "
        "discarded."
    ),
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
    # -- telegram message bodies (rendered at send time in the
    #    recipient's language; every message carries the gym name and,
    #    when it references a booking, its #id, with one date format) --
    "tg.booking.granted": "\u2705 [{gym}] Booked #{id}: {klass} \u2014 {when}.",
    "tg.booking.full": (
        "\u26a0\ufe0f [{gym}] Couldn't book #{id}: {klass} \u2014 {when}. Class was full."
    ),
    "tg.booking.class_not_visible": (
        "\u26a0\ufe0f [{gym}] Couldn't book #{id}: {klass} \u2014 {when}. "
        "Class never appeared on the schedule."
    ),
    "tg.booking.cookie_invalid": (
        "\U0001f512 [{gym}] Booking #{id} skipped: {klass} \u2014 {when}. "
        "WodBuster cookie is invalid or missing \u2014 paste a fresh one to resume."
    ),
    "tg.booking.upstream_unavailable": (
        "\u26a0\ufe0f [{gym}] Booking #{id} failed: {klass} \u2014 {when}. "
        "WodBuster response was unexpected; check the worker logs."
    ),
    "tg.booking.skipped": (
        "\U0001f3d6\ufe0f [{gym}] Booking #{id} skipped: {klass} \u2014 {when}. "
        "Vacation mode is on for this date."
    ),
    "tg.booking.cancelled": "\U0001f6ab [{gym}] Cancelled #{id}: {klass} \u2014 {when}.",
    "tg.booking.unknown": ("\u2139\ufe0f [{gym}] Booking #{id}: {klass} \u2014 {when} ({status})."),
    # Single-day override branches (ADR-0012). Keyed on ``outcome_source``
    # rather than ``terminal_status``: a substitution is never silent
    # (INV-008), so the copy names the booked class, the requested class
    # and the reason.
    "tg.booking.override_skip": (
        "\u23ed\ufe0f [{gym}] Booking #{id} skipped \u2014 {when}. You marked this day "
        "as skipped, so {klass} was not contested."
    ),
    "tg.booking.fallback_granted": (
        "\u26a0\ufe0f [{gym}] Substitution \u2014 booked #{id}: {klass} \u2014 {when}. "
        "You had asked for {requested_class} at {requested_time}, but {reason}, so the "
        "rule's class was booked instead."
    ),
    "tg.booking.fallback_exhausted": (
        "\u26a0\ufe0f [{gym}] Nothing booked #{id} \u2014 {when}. Your {requested_class} "
        "at {requested_time} failed: {reason}. The rule's {klass} also failed: {rule_reason}."
    ),
    # Reason fragments shared by the Telegram copy, the email body (which
    # reuses it) and the dashboard banner. Deliberately namespace-neutral:
    # duplicating the same sentence under ``tg.*`` and ``banner.*`` would
    # guarantee the two drift apart.
    "booking.reason.class_not_visible": "that class never appeared on the schedule",
    "booking.reason.full": "that class was full",
    "booking.reason.upstream_unavailable": "WodBuster returned an unexpected response",
    "booking.reason.cookie_invalid": "the WodBuster session was rejected",
    "booking.reason.unknown": "it was unavailable",
    "tg.alert.cookie_expiring": (
        "\u23f0 [{gym}] WodBuster cookie expires before the next booking window "
        "({when}). Refresh it to keep bookings running."
    ),
    "tg.alert.cookie_invalid": (
        "\U0001f512 [{gym}] WodBuster cookie was rejected. Bookings are paused "
        "until you paste a fresh cookie."
    ),
    "tg.alert.anomaly.one": (
        "\u2757 [{gym}] Anomaly: no outcome recorded for {klass} \u2014 {when}. Check the worker."
    ),
    "tg.alert.anomaly.many": (
        "\u2757 [{gym}] Anomaly: {count} scheduled bookings produced no outcome. Check the worker."
    ),
    # -- telegram bot command replies (rendered in the bound operator's
    #    language; same date format, gym name, and #id as the alerts) --
    "tg.cmd.help": (
        "Commands:\n"
        "/status \u2014 is this chat bound?\n"
        "/next \u2014 next scheduled booking and upcoming slots (with ids)\n"
        "/last \u2014 most recent booking outcome\n"
        "/cancel <booking-id> \u2014 cancel a booking\n"
        "/ack \u2014 acknowledge the cookie-expiring warning\n"
        "Rules are managed in the web UI, not here."
    ),
    "tg.cmd.start.missing_token": (
        "Missing token. Open the web UI (Telegram page) and click "
        "'Generate link' to get a one-shot binding URL."
    ),
    "tg.cmd.start.invalid_token": (
        "Token invalid or expired. Open the web UI (Telegram page) and "
        "generate a fresh link \u2014 tokens live 10 minutes and can only be used once."
    ),
    "tg.cmd.start.no_operator": "Operator profile not found. Contact the deployment owner.",
    "tg.cmd.start.bound": (
        "Bound. This chat will now receive booking outcomes, cookie-expiring "
        "warnings, and anomaly alerts."
    ),
    "tg.cmd.status.unbound": (
        "This chat is not bound. Open the web UI (Telegram page) and click 'Generate link' to bind."
    ),
    "tg.cmd.status.bound": (
        "Bound to operator {operator}. You will receive booking outcomes and alerts here."
    ),
    "tg.cmd.unbound": (
        "This chat is not bound. Open the web UI (Telegram page) and click "
        "'Generate link' to bind it before using this command."
    ),
    "tg.cmd.next.empty": "Nothing scheduled. No active rules have a window on the horizon.",
    "tg.cmd.next.line": "Next booking: {slot} (window opens {opens}).",
    "tg.cmd.next.upcoming_header": "Upcoming slots:",
    "tg.cmd.next.slot_granted": "- #{id} {when} {klass} (granted)",
    "tg.cmd.next.slot_scheduled": "- {when} {klass} (scheduled)",
    "tg.cmd.last.empty": "No bookings yet. Nothing has been attempted for this operator.",
    "tg.cmd.last.none": "No bookings yet.",
    "tg.cmd.last.line": (
        "Last booking #{id}: {klass} on {when} \u2014 {status} (attempted {attempted})."
    ),
    "tg.cmd.cancel.usage": (
        "Usage: /cancel <booking-id>. Find the id in /next, /last, or the web UI."
    ),
    "tg.cmd.cancel.nan": "Booking id must be a number. Usage: /cancel <booking-id>.",
    "tg.cmd.cancel.unavailable": "Cancellation is temporarily unavailable. Try again shortly.",
    "tg.cmd.cancel.not_found": "Booking #{id} not found for this operator.",
    "tg.cmd.cancel.already": "Booking #{id} is already cancelled. Nothing to do.",
    "tg.cmd.cancel.upstream": "Couldn't reach WodBuster to cancel #{id}. Try again in a moment.",
    "tg.cmd.cancel.ok": "Cancelled #{id}: {klass} on {when}.",
    "tg.cmd.ack.none": "No open cookie-expiring warning to acknowledge.",
    "tg.cmd.ack.ok": "Acknowledged. I'll stop nagging about the cookie for this cycle.",
    "tg.cmd.rule_mutation": (
        "Rules can't be changed from Telegram. Open the web UI (Rules page) to "
        "create, edit, or delete a scheduling rule. This chat is for status checks "
        "and one-off actions only."
    ),
    "tg.cmd.unknown": (
        "Unknown command. Send /help to see what I can do, or /start <token> with "
        "the token from the web UI to bind this chat."
    ),
    "tg.test.message": (
        "\U0001f9ea Test message from WodBuster Booking Scheduler. "
        "If you see this, notifications are working."
    ),
    # -- email notifications (ADR-0011): subjects + footer. The message
    #    body reuses the tg.* copy via messages.render. --
    "email.subject.booking": "Booking update · {gym}",
    "email.subject.cookie_expiring": "Action needed: session expiring · {gym}",
    "email.subject.cookie_invalid": "Action needed: session invalid · {gym}",
    "email.subject.anomaly": "Missed booking window · {gym}",
    "email.footer.tagline": (
        "WodBuster Booking Scheduler books your classes the moment the window opens."
    ),
    "email.footer.preferences": "Manage which emails you receive in your profile.",
    "email.footer.unsubscribe": "Unsubscribe from these emails",
    # -- account (signup lifecycle) mail; transactional, always sent --
    "email.account.received.subject": "We received your access request",
    "email.account.received.body": (
        "Thanks for signing up. Your access request is now waiting for an administrator "
        "to review it. We'll email you as soon as it is decided."
    ),
    "email.account.approved.subject": "Your access is approved",
    "email.account.approved.body": (
        "Good news: your access has been approved. You can now sign in and start booking "
        "your classes."
    ),
    "email.account.rejected.subject": "About your access request",
    "email.account.rejected.body": (
        "Your access request was not approved this time. If you think this is a mistake, "
        "sign in again to submit a new request."
    ),
    # -- dashboard alert banners (rendered server-side in the operator's
    #    web language; one date format via format_slot, gym timezone) --
    "banner.aria_label": "System alerts",
    "banner.window_fallback": "the next window",
    "banner.cookie_expiring.heading": "Cookie expiring soon",
    "banner.cookie_expiring.body": (
        "Your WodBuster cookie is projected to expire before {when}. Paste a fresh "
        "cookie on the Cookie page to keep bookings running."
    ),
    "banner.cookie_invalid.heading": "Cookie rejected",
    "banner.cookie_invalid.body": (
        "WodBuster rejected the stored cookie. Bookings are paused until you paste a fresh one."
    ),
    "banner.anomaly.heading": "Silent-run detected",
    "banner.anomaly.body": (
        "No booking outcome was recorded for a window that should have closed. Check the worker."
    ),
    "banner.booking_fallback.heading": "Class substituted",
    "banner.booking_fallback.body": (
        "Booked {klass} \u2014 {when} instead of the {requested_class} at "
        "{requested_time} you asked for, because {reason}."
    ),
    "banner.unknown.heading": "Alert: {kind}",
    "banner.unknown.body": "See logs for details.",
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
    "chip.vacation": "en vacaciones",
    "chip.modified": "modificado",
    "chip.skipped_day": "se saltará",
    "chip.full": "completo",
    "chip.cancelled": "cancelado",
    "chip.skipped": "omitido",
    "chip.cookie_invalid": "cookie inválida",
    "chip.class_not_visible": "clase no visible",
    "chip.upstream_unavailable": "servicio no disponible",
    # Origen del intento, ortogonal al estado terminal de arriba
    # (ADR-0012 Decisión 4): qué plan condujo el intento, no cómo acabó.
    "chip.source.override": "día modificado",
    "chip.source.override_fallback": "sustituida",
    "chip.source.override_skip": "saltado por ti",
    # -- nav ---------------------------------------------------------
    "nav.dashboard": "🏠 Panel",
    "nav.rules": "📅 Reglas",
    "nav.history": "📜 Historial",
    "nav.vacation": "🏖️ Vacaciones",
    "nav.cookie": "🍪 Cookie",
    "nav.telegram": "🤖 Telegram",
    "nav.faq": "❓ Ayuda",
    "nav.logout": "👋 Cerrar sesión",
    "nav.gym_switch": "Cambiar gimnasio",
    "gym.context.label": "🏋️ {name}",
    "gym.select.prompt": "Elige un gimnasio en el selector de arriba para actuar sobre él.",
    "modal.cancel": "🚫 Cancelar",
    "modal.confirm": "✅ Confirmar",
    # -- profile -----------------------------------------------------
    "nav.profile": "🙋 Perfil",
    "nav.account": "Menú de cuenta",
    "nav.admin": "🛡️ Usuarios",
    "admin.users.title": "🛡️ Usuarios",
    "admin.users.subtitle": "Aprueba nuevos usuarios y gestiona el acceso.",
    "admin.users.pending_title": "Solicitudes pendientes",
    "admin.users.pending_empty": "No hay solicitudes pendientes.",
    "admin.users.active_title": "Usuarios",
    "admin.users.col.user": "Usuario",
    "admin.users.col.email": "Correo",
    "admin.users.col.provider": "Proveedor",
    "admin.users.col.role": "Rol",
    "admin.users.col.status": "Estado",
    "admin.users.col.access": "Acceso",
    "admin.users.col.remove": "Eliminar",
    "admin.users.approve": "✅ Aprobar",
    "admin.users.reject": "🚫 Rechazar",
    "admin.users.role.admin": "Administrador",
    "admin.users.role.user": "Usuario",
    "admin.users.you": "tú",
    "admin.users.no_action": "",
    "admin.users.status.active": "Activo",
    "admin.users.status.banned_indefinite": "Suspendido (indefinido)",
    "admin.users.status.banned_until": "Suspendido hasta",
    "admin.users.ban.duration_label": "Duración del baneo",
    "admin.users.ban.duration.1d": "1 día",
    "admin.users.ban.duration.7d": "7 días",
    "admin.users.ban.duration.30d": "30 días",
    "admin.users.ban.duration.indefinite": "Indefinido",
    "admin.users.ban.button": "🚫 Banear",
    "admin.users.unban.button": "✅ Quitar baneo",
    "admin.users.delete.button": "🗑️ Eliminar",
    "admin.users.ban_confirm": (
        "¿Banear a este usuario? Perderá el acceso hasta que expire el baneo."
    ),
    "admin.users.delete_confirm": (
        "¿Eliminar este usuario y todos sus datos? No se puede deshacer."
    ),
    "admin.notify.new_request": (
        "🛡️ Nueva solicitud de acceso de {name}. Revísala en la aplicación."
    ),
    "profile.title": "Tu perfil",
    "profile.subtitle": "Gestiona cómo apareces y el idioma de tus notificaciones.",
    "profile.display_name_label": "Nombre visible",
    "profile.short_name_label": "Nombre corto",
    "profile.short_name_placeholder": "Etiqueta corta opcional",
    "profile.language_label": "Idioma de comunicación",
    "profile.lang.en": "Inglés",
    "profile.lang.es": "Español",
    "profile.picture.alt": "Foto de perfil",
    "profile.picture.note": "Tu foto proviene de WodBuster. Para cambiarla, hazlo en WodBuster.",
    "profile.save": "💾 Guardar perfil",
    "profile.flash.saved": "Perfil guardado.",
    "profile.flash.name_required": "El nombre visible no puede estar vacío.",
    "profile.flash.too_long": "El nombre es demasiado largo.",
    "profile.flash.bad_language": "Elige un idioma admitido.",
    "profile.email_label": "Correo electrónico",
    "profile.email_placeholder": "tu@ejemplo.com",
    "profile.email_prefs_label": "Notificaciones por correo",
    "profile.email_pref.bookings": "Resultados de reservas",
    "profile.email_pref.session_alerts": "Alertas de sesión y cookie",
    "profile.email_prefs_note": (
        "Los correos de cuenta (aprobación, rechazo) se envían siempre, al margen de estos ajustes."
    ),
    "profile.flash.bad_email": "Ese correo no parece válido.",
    # -- dashboard ---------------------------------------------------
    "dashboard.eyebrow": "Hola de nuevo",
    "dashboard.title.hero": "Crack",
    "dashboard.title.emoji": "💪",
    "dashboard.subtitle": (
        "Todo lo que hay debajo alimenta tu automatización de reservas. "
        "Las reglas definen cuándo, la cookie prueba quién eres, y el "
        "heartbeat detecta problemas antes de que pierdas una clase."
    ),
    "dashboard.pending_requests": "Tienes {count} solicitud(es) de acceso por revisar.",
    "dashboard.countdown.label": "Próxima ventana de reserva en",
    "dashboard.countdown.firing": (
        "Ejecutando — refresca en unos segundos para ver el resultado en Historial."
    ),
    "dashboard.countdown.empty.label": "Sin reservas próximas",
    "dashboard.countdown.empty.hint": (
        "Añade una regla para programar tu primera reserva automática."
    ),
    "dashboard.cards.profile.title": "🙋 Perfil",
    "dashboard.cards.profile.body": (
        "Consulta los datos de tu cuenta y elige el idioma y las "
        "preferencias de correo de tus notificaciones."
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
    "rules.heading.new": "Nueva regla",
    "rules.heading.edit": "Editar regla",
    "rules.empty.title": "✨ Aún no hay reglas",
    "rules.empty.body": "Crea una para empezar a automatizar reservas.",
    "rules.table.term": "Término",
    "rules.table.primary": "Clase principal",
    "rules.table.primary_hour": "Hora",
    "rules.table.second_shot": "Alternativa",
    "rules.table.second_shot_hour": "Hora alternativa",
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
    "rules.form.create_button": "➕ Crear regla",
    "rules.form.save_button": "💾 Guardar cambios",
    "rules.form.delete_button": "🗑️ Borrar regla",
    # -- history / upcoming -----------------------------------------
    "history.eyebrow": "Actividad",
    "history.title": "📜 Historial de reservas",
    "history.subtitle": (
        "Los intentos de reserva de esta semana, del más reciente al más antiguo. "
        "Usa el botón Cancelar en cualquier clase reservada próxima para "
        "liberar tu plaza (actualiza también WodBuster y avisa por Telegram)."
    ),
    "history.empty.title": "🕓 Sin intentos esta semana",
    "history.empty.body": (
        "Cuando el scheduler ejecute una de tus reglas esta semana, el resultado aparecerá aquí."
    ),
    "history.upcoming.title": "🗓️ Próximas reservas",
    "history.upcoming.empty": (
        "No hay reservas concedidas ni programadas en el horizonte. Crea "
        "una regla para empezar a automatizar."
    ),
    "history.upcoming.edit_day_aria": "Editar el día de reserva del {date}",
    "history.attempts.title": "📜 Intentos de esta semana",
    "history.table.day": "Día",
    "history.table.date": "Fecha",
    "history.table.class": "Clase",
    "history.table.result": "Resultado",
    "history.table.attempted": "Intentado",
    "history.second_shot_tag": "(alternativa)",
    "history.cancel_button": "🚫 Cancelar",
    "history.confirm.cancel": "¿Cancelar esta reserva en WodBuster?",
    # -- single-day override (ADR-0012) ------------------------------
    "override.eyebrow": "Un solo día",
    "override.title": "✏️ Editar este día",
    "override.back_to_history": "← Volver al historial",
    "override.rule_values": "Este día viene de una regla: {class_type} a las {class_time}.",
    "override.rule_original": "Regla: {class_type} a las {class_time}",
    "override.form.target": "Clase solo para este día",
    "override.form.class_type": "Tipo de clase",
    "override.form.class_time": "Hora de la clase",
    "override.form.target_hint": ("Se aplica solo a esta fecha. La regla semanal no se modifica."),
    "override.form.second_shot": "Alternativa",
    "override.form.second_shot_value": (
        "La alternativa de la regla se sigue intentando este día: {class_type} a las {class_time}."
    ),
    "override.form.second_shot_none": "Esta regla no tiene alternativa.",
    "override.form.second_shot_clear": "Saltar la alternativa solo en esta fecha",
    "override.form.second_shot_clear_hint": (
        "La regla mantiene su alternativa para el resto de fechas."
    ),
    "override.warning.not_published": (
        "El gimnasio aún no ha publicado el horario de esta fecha. Las "
        "opciones de abajo son las combinaciones conocidas para este día "
        "de la semana; la clase se vuelve a comprobar cuando se abra la "
        "ventana de reserva."
    ),
    "override.warning.probe_unavailable": (
        "Lista de clases no disponible, así que la clase no se puede "
        "comprobar contra esta fecha. Puedes guardar igualmente; se "
        "volverá a comprobar cuando se abra la ventana de reserva. Pega "
        "una cookie fresca:"
    ),
    "override.warning.not_validated": (
        "Este día no está validado contra un horario publicado. Si la "
        "clase no está disponible al abrirse la ventana, se reserva la "
        "clase de la regla."
    ),
    "override.error.combination_unavailable": ("Esa clase no se imparte a esa hora en esta fecha."),
    "override.error.invalid_time": "Usa HH:MM en formato de 24 horas.",
    "override.error.invalid_class_type": "Elige un tipo de clase.",
    "override.error.skip_exclusive": (
        "Un día saltado no lleva clase: vacía el tipo y la hora, o guarda "
        "una clase en lugar de saltar el día."
    ),
    "override.skip_hint": (
        "O salta este día por completo: no se intenta ninguna reserva y la "
        "regla semanal no se modifica."
    ),
    "override.skip_active": (
        "Este día está saltado. No se intentará ninguna reserva. Guarda una "
        "clase arriba, o vuelve a la regla, para deshacerlo."
    ),
    "override.edit_button": "✏️ Editar día",
    "override.save_button": "💾 Guardar este día",
    "override.skip_button": "🚫 Saltar este día",
    "override.revert_button": "🚫 Volver a la regla",
    "override.confirm.revert": "¿Descartar el cambio de este día y volver a la regla?",
    "override.confirm.skip": "¿Saltar este día? No se intentará ninguna reserva.",
    # -- cookie ------------------------------------------------------
    "cookie.eyebrow": "Acceso",
    "cookie.title": "🍪 Cookie de WodBuster",
    "cookie.subtitle": (
        "Pega el valor de la cookie .WBAuth que el worker usa para "
        "autenticarse contra WodBuster. Se guarda cifrada y se comprueba "
        "cada hora."
    ),
    "cookie.all_gyms_note": (
        "Una sola cookie sirve para todos los gimnasios a los que puedes acceder "
        "en WodBuster. Al pegarla aquí se aplica a todos tus gimnasios y se "
        "actualiza la lista automáticamente."
    ),
    "cookie.paste.title": "Pega una cookie nueva",
    "cookie.hint": ("Cópiala desde devtools: Application → Cookies → .wodbuster.com → .WBAuth."),
    "cookie.paste_button": "💾 Validar y guardar",
    "cookie.status.empty": "Aún no hay cookie. Pega una debajo para activar las reservas.",
    "cookie.status.pasted": "Pegada",
    "cookie.status.last_validated": "Última validación",
    "cookie.status.projected_expiry": "Caducidad estimada",
    "cookie.status.awaiting_first_heartbeat": "esperando primer heartbeat",
    "cookie.status.last_probe": "Última comprobación",
    "cookie.status.valid": "válida",
    "cookie.status.rejected": "rechazada",
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
    "landing.cards.gyms.title": "🏢 Varios gimnasios",
    "landing.cards.gyms.body": (
        "Reserva en más de un gimnasio de WodBuster desde una sola cuenta. "
        "Tus gimnasios aparecen automáticamente y un solo acceso vale para "
        "todos, reservando cada uno de forma independiente."
    ),
    # -- auth --------------------------------------------------------
    "auth.landing.title": "WodBuster Booking Scheduler",
    "auth.denied.title": "🚫 Acceso denegado",
    "auth.denied.body": (
        "Esta cuenta no está autorizada para acceder al WodBuster Booking Scheduler."
    ),
    "auth.denied.contact": (
        "Si crees que es un error, contacta con la persona que configuró el despliegue."
    ),
    "auth.denied.back": "⬅️ Volver a iniciar sesión",
    "auth.pending.title": "⏳ Solicitud recibida",
    "auth.pending.body": (
        "Tu solicitud de acceso está pendiente de aprobación por el administrador."
    ),
    "auth.pending.hint": "Podrás iniciar sesión aquí en cuanto se apruebe.",
    "auth.pending.back": "⬅️ Volver a iniciar sesión",
    "auth.suspended.title": "⛔ Acceso suspendido",
    "auth.suspended.body": "El administrador ha suspendido tu acceso.",
    "auth.suspended.back": "⬅️ Volver a iniciar sesión",
    # -- baja de correos (ADR-0011) --
    "unsubscribe.title": "Baja de correos",
    "unsubscribe.ok.title": "✅ Te has dado de baja",
    "unsubscribe.ok.body": (
        "Ya no recibirás correos de reservas ni de alertas de sesión. Puedes volver a "
        "activarlos cuando quieras desde tu perfil."
    ),
    "unsubscribe.bad.title": "⚠️ Enlace no válido",
    "unsubscribe.bad.body": (
        "Este enlace de baja no es válido o ha caducado. Gestiona tus preferencias de "
        "correo desde tu perfil."
    ),
    "unsubscribe.back": "⬅️ Volver a iniciar sesión",
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
    "faq.section.account": "Cuenta y perfil",
    "faq.section.cookie": "Cookie",
    "faq.section.gyms": "Gimnasios",
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
    "faq.q.pending_signup": "¿Por qué mi primer acceso está pendiente de aprobación?",
    "faq.a.pending_signup": (
        "Una identidad OAuth nueva crea una solicitud de acceso pendiente. Un administrador "
        "debe aprobarla antes de que la cuenta pueda usar los datos o controles de reservas. "
        "La aplicación te envía un correo al recibir la solicitud y otro cuando se aprueba o "
        "rechaza."
    ),
    "faq.q.profile_edit": "¿Qué puedo cambiar en mi perfil?",
    "faq.a.profile_edit": (
        "En la página de <a href='{profile_url}'>Perfil</a> puedes editar tu nombre visible, "
        "el nombre corto opcional, el idioma de comunicación, el correo electrónico y las "
        "preferencias de correo operativo. La foto procede de WodBuster y aquí es de solo "
        "lectura."
    ),
    "faq.q.profile_language": "¿Qué controla el idioma de comunicación?",
    "faq.a.profile_language": (
        "Controla la interfaz cuando has iniciado sesión y el idioma de las notificaciones "
        "de Telegram y correo. Cámbialo en la página de <a href='{profile_url}'>Perfil</a>; "
        "el nuevo idioma se aplica al guardar."
    ),
    "faq.q.admin_difference": "¿Qué puede hacer un administrador que no pueda un usuario?",
    "faq.a.admin_difference": (
        "Los usuarios gestionan únicamente sus gimnasios, reglas, reservas, cookie, perfil y "
        "ajustes de notificaciones. Los administradores también revisan solicitudes de acceso "
        "y pueden aprobar, rechazar, suspender, rehabilitar o eliminar de forma permanente a "
        "usuarios que no sean administradores. No pueden suspenderse o eliminarse a sí mismos "
        "ni hacerlo con otro administrador."
    ),
    "faq.q.access_suspended": "¿Qué ocurre si suspenden o eliminan mi acceso?",
    "faq.a.access_suspended": (
        "Una cuenta suspendida no puede entrar hasta que el administrador la rehabilite o "
        "termine una suspensión temporal; sus datos se conservan. La eliminación es "
        "permanente y borra al usuario junto con sus gimnasios, reglas, historial, cookies y "
        "datos de notificaciones."
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
    "faq.q.gyms_multiple": "¿Puedo reservar en más de un gimnasio?",
    "faq.a.gyms_multiple": (
        "Sí. Todos los gimnasios de WodBuster a los que tu cuenta puede acceder aparecen "
        "automáticamente en el selector de gimnasios, y una sola cookie autentica a todos. "
        "Cada gimnasio reserva, comprueba la cookie compartida y genera sus propias alertas "
        "de forma independiente."
    ),
    "faq.q.gyms_appear": "¿Cómo se añaden los gimnasios?",
    "faq.a.gyms_appear": (
        "Automáticamente. Al pegar una cookie en la página de <a href='{cookie_url}'>Cookie</a>, "
        "y cada vez que inicias sesión, la aplicación pregunta a WodBuster a qué gimnasios puede "
        "acceder tu cuenta y añade los nuevos. Cambia entre ellos desde el selector de la barra "
        "de navegación."
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
        "aplicación llama a WodBuster, cambia la fila a <em>cancelada</em> y envía las "
        "notificaciones configuradas."
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
        "cookie rechazada) genera un aviso en el panel. También llega a Telegram si el chat "
        "está vinculado y por correo si tienes una dirección y la preferencia correspondiente "
        "está activa. Cada mensaje identifica el gimnasio al que se refiere."
    ),
    "faq.q.email_preferences": "¿Qué notificaciones por correo puedo controlar?",
    "faq.a.email_preferences": (
        "La página de <a href='{profile_url}'>Perfil</a> tiene controles separados para los "
        "resultados de reservas y las alertas de sesión o cookie. También puedes editar allí "
        "la dirección de destino. Los cambios se aplican a los eventos generados después de "
        "guardar."
    ),
    "faq.q.email_unsubscribe": "¿Cómo me doy de baja de los correos?",
    "faq.a.email_unsubscribe": (
        "Usa el enlace de baja de cualquier correo operativo para desactivar los mensajes de "
        "reservas y alertas de sesión sin iniciar sesión. Puedes volver a activar cada "
        "categoría desde la página de <a href='{profile_url}'>Perfil</a>. Telegram y los "
        "avisos del panel no se ven afectados."
    ),
    "faq.q.account_emails": "¿Por qué no puedo desactivar los correos de cuenta?",
    "faq.a.account_emails": (
        "Los mensajes que confirman la recepción, aprobación o rechazo de una solicitud de "
        "acceso son transaccionales. Se envían siempre para que el usuario pueda seguir el "
        "proceso de acceso, incluso si se ha dado de baja del correo operativo."
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
        "identifica las cuentas por el subject id que envía el callback de OAuth. Una identidad "
        "de proveedor que no haya entrado antes crea una solicitud independiente y debe ser "
        "aprobada por un administrador antes de acceder a la aplicación."
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
    "flash.override.saved": "Día actualizado. La regla semanal no cambia.",
    "flash.override.skipped": "Día saltado. No se intentará ninguna reserva.",
    "flash.override.reverted": "Día devuelto a la regla.",
    "flash.override.window_closed": (
        "La ventana de reserva de ese día ya se ha abierto, así que ya no "
        "se puede editar. No se ha guardado nada."
    ),
    "flash.override.already_executed": (
        "Ese día ya se ha ejecutado. Consulta el resultado más abajo. No se ha guardado nada."
    ),
    "flash.override.discarded": (
        "La regla pasa a otro día de la semana, así que los cambios de un "
        "solo día que tenías guardados para {dates} ya no se aplican y se "
        "han descartado."
    ),
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
    # -- cuerpos de mensajes de Telegram (se renderizan al enviar en el
    #    idioma del destinatario; cada mensaje lleva el nombre del
    #    gimnasio y, si referencia una reserva, su #id, con un
    #    \u00fanico formato de fecha) --
    "tg.booking.granted": "\u2705 [{gym}] Reservado #{id}: {klass} \u2014 {when}.",
    "tg.booking.full": (
        "\u26a0\ufe0f [{gym}] No se pudo reservar #{id}: {klass} \u2014 {when}. "
        "La clase estaba completa."
    ),
    "tg.booking.class_not_visible": (
        "\u26a0\ufe0f [{gym}] No se pudo reservar #{id}: {klass} \u2014 {when}. "
        "La clase no apareció en el horario."
    ),
    "tg.booking.cookie_invalid": (
        "\U0001f512 [{gym}] Reserva #{id} omitida: {klass} \u2014 {when}. "
        "La cookie de WodBuster no es válida o falta \u2014 pega una nueva para reanudar."
    ),
    "tg.booking.upstream_unavailable": (
        "\u26a0\ufe0f [{gym}] Reserva #{id} fallida: {klass} \u2014 {when}. "
        "Respuesta inesperada de WodBuster; revisa los registros del worker."
    ),
    "tg.booking.skipped": (
        "\U0001f3d6\ufe0f [{gym}] Reserva #{id} omitida: {klass} \u2014 {when}. "
        "El modo vacaciones está activo para esta fecha."
    ),
    "tg.booking.cancelled": "\U0001f6ab [{gym}] Cancelada #{id}: {klass} \u2014 {when}.",
    "tg.booking.unknown": ("\u2139\ufe0f [{gym}] Reserva #{id}: {klass} \u2014 {when} ({status})."),
    # Ramas del override de un solo día (ADR-0012). Se eligen por
    # ``outcome_source``, no por ``terminal_status``: una sustitución
    # nunca es silenciosa (INV-008), así que el texto nombra la clase
    # reservada, la clase pedida y el motivo.
    "tg.booking.override_skip": (
        "\u23ed\ufe0f [{gym}] Reserva #{id} saltada \u2014 {when}. Marcaste este día "
        "para saltarlo, así que no se intentó reservar {klass}."
    ),
    "tg.booking.fallback_granted": (
        "\u26a0\ufe0f [{gym}] Sustitución \u2014 reservado #{id}: {klass} \u2014 {when}. "
        "Habías pedido {requested_class} a las {requested_time}, pero {reason}, así que "
        "se reservó la clase de la regla."
    ),
    "tg.booking.fallback_exhausted": (
        "\u26a0\ufe0f [{gym}] No se reservó nada #{id} \u2014 {when}. Tu {requested_class} "
        "a las {requested_time} falló: {reason}. La clase de la regla, {klass}, también "
        "falló: {rule_reason}."
    ),
    # Fragmentos de motivo compartidos por el texto de Telegram, el cuerpo
    # del email (que lo reutiliza) y el banner del panel. A propósito sin
    # namespace de canal: duplicar la misma frase bajo ``tg.*`` y
    # ``banner.*`` garantizaría que ambas acaben divergiendo.
    "booking.reason.class_not_visible": "esa clase no apareció en el horario",
    "booking.reason.full": "esa clase estaba completa",
    "booking.reason.upstream_unavailable": "WodBuster devolvió una respuesta inesperada",
    "booking.reason.cookie_invalid": "WodBuster rechazó la sesión",
    "booking.reason.unknown": "no estaba disponible",
    "tg.alert.cookie_expiring": (
        "\u23f0 [{gym}] La cookie de WodBuster caduca antes de la próxima ventana "
        "de reserva ({when}). Renuévala para que las reservas sigan funcionando."
    ),
    "tg.alert.cookie_invalid": (
        "\U0001f512 [{gym}] WodBuster rechazó la cookie. Las reservas están en "
        "pausa hasta que pegues una cookie nueva."
    ),
    "tg.alert.anomaly.one": (
        "\u2757 [{gym}] Anomalía: no se registró resultado para {klass} \u2014 "
        "{when}. Revisa el worker."
    ),
    "tg.alert.anomaly.many": (
        "\u2757 [{gym}] Anomalía: {count} reservas programadas no produjeron "
        "resultado. Revisa el worker."
    ),
    # -- respuestas del bot de Telegram (en el idioma del operador
    #    vinculado; mismo formato de fecha, nombre de gimnasio y #id) --
    "tg.cmd.help": (
        "Comandos:\n"
        "/status \u2014 ¿está vinculado este chat?\n"
        "/next \u2014 próxima reserva programada y huecos próximos (con ids)\n"
        "/last \u2014 resultado de la última reserva\n"
        "/cancel <id-reserva> \u2014 cancelar una reserva\n"
        "/ack \u2014 confirmar el aviso de cookie a punto de caducar\n"
        "Las reglas se gestionan en la web, no aquí."
    ),
    "tg.cmd.start.missing_token": (
        "Falta el token. Abre la web (página de Telegram) y pulsa "
        "'Generar enlace' para obtener una URL de vinculación de un solo uso."
    ),
    "tg.cmd.start.invalid_token": (
        "Token no válido o caducado. Abre la web (página de Telegram) y genera "
        "un enlace nuevo \u2014 los tokens duran 10 minutos y solo se pueden usar una vez."
    ),
    "tg.cmd.start.no_operator": (
        "No se encontró el perfil de operador. Contacta con el responsable del despliegue."
    ),
    "tg.cmd.start.bound": (
        "Vinculado. Este chat recibirá los resultados de reservas, avisos de cookie "
        "a punto de caducar y alertas de anomalías."
    ),
    "tg.cmd.status.unbound": (
        "Este chat no está vinculado. Abre la web (página de Telegram) y pulsa "
        "'Generar enlace' para vincularlo."
    ),
    "tg.cmd.status.bound": (
        "Vinculado al operador {operator}. Recibirás aquí los resultados de reservas y las alertas."
    ),
    "tg.cmd.unbound": (
        "Este chat no está vinculado. Abre la web (página de Telegram) y pulsa "
        "'Generar enlace' para vincularlo antes de usar este comando."
    ),
    "tg.cmd.next.empty": (
        "Nada programado. Ninguna regla activa tiene una ventana en el horizonte."
    ),
    "tg.cmd.next.line": "Próxima reserva: {slot} (la ventana abre {opens}).",
    "tg.cmd.next.upcoming_header": "Próximos huecos:",
    "tg.cmd.next.slot_granted": "- #{id} {when} {klass} (concedida)",
    "tg.cmd.next.slot_scheduled": "- {when} {klass} (programada)",
    "tg.cmd.last.empty": "Aún no hay reservas. No se ha intentado nada para este operador.",
    "tg.cmd.last.none": "Aún no hay reservas.",
    "tg.cmd.last.line": (
        "Última reserva #{id}: {klass} el {when} \u2014 {status} (intentada {attempted})."
    ),
    "tg.cmd.cancel.usage": (
        "Uso: /cancel <id-reserva>. Encuentra el id en /next, /last o en la web."
    ),
    "tg.cmd.cancel.nan": "El id de reserva debe ser un número. Uso: /cancel <id-reserva>.",
    "tg.cmd.cancel.unavailable": (
        "La cancelación no está disponible temporalmente. Inténtalo en breve."
    ),
    "tg.cmd.cancel.not_found": "Reserva #{id} no encontrada para este operador.",
    "tg.cmd.cancel.already": "La reserva #{id} ya está cancelada. Nada que hacer.",
    "tg.cmd.cancel.upstream": (
        "No se pudo contactar con WodBuster para cancelar #{id}. Inténtalo de nuevo en un momento."
    ),
    "tg.cmd.cancel.ok": "Cancelada #{id}: {klass} el {when}.",
    "tg.cmd.ack.none": "No hay ningún aviso de cookie a punto de caducar que confirmar.",
    "tg.cmd.ack.ok": "Confirmado. Dejaré de insistir con la cookie este ciclo.",
    "tg.cmd.rule_mutation": (
        "Las reglas no se pueden cambiar desde Telegram. Abre la web (página de Reglas) "
        "para crear, editar o eliminar una regla de programación. Este chat es solo para "
        "consultar el estado y acciones puntuales."
    ),
    "tg.cmd.unknown": (
        "Comando desconocido. Envía /help para ver qué puedo hacer, o /start <token> "
        "con el token de la web para vincular este chat."
    ),
    "tg.test.message": (
        "\U0001f9ea Mensaje de prueba de WodBuster Booking Scheduler. "
        "Si ves esto, las notificaciones funcionan."
    ),
    # -- email notifications (ADR-0011): asuntos + pie. El cuerpo reutiliza
    #    el texto tg.* vía messages.render. --
    "email.subject.booking": "Actualización de reserva · {gym}",
    "email.subject.cookie_expiring": "Acción necesaria: sesión por caducar · {gym}",
    "email.subject.cookie_invalid": "Acción necesaria: sesión inválida · {gym}",
    "email.subject.anomaly": "Ventana de reserva perdida · {gym}",
    "email.footer.tagline": (
        "WodBuster Booking Scheduler reserva tus clases en cuanto se abre la ventana."
    ),
    "email.footer.preferences": "Gestiona qué correos recibes en tu perfil.",
    "email.footer.unsubscribe": "Darte de baja de estos correos",
    # -- correos de cuenta (ciclo de alta); transaccionales, siempre se envían --
    "email.account.received.subject": "Hemos recibido tu solicitud de acceso",
    "email.account.received.body": (
        "Gracias por registrarte. Tu solicitud de acceso está pendiente de que un "
        "administrador la revise. Te avisaremos por correo en cuanto se decida."
    ),
    "email.account.approved.subject": "Tu acceso ha sido aprobado",
    "email.account.approved.body": (
        "Buenas noticias: tu acceso ha sido aprobado. Ya puedes iniciar sesión y empezar "
        "a reservar tus clases."
    ),
    "email.account.rejected.subject": "Sobre tu solicitud de acceso",
    "email.account.rejected.body": (
        "Tu solicitud de acceso no ha sido aprobada esta vez. Si crees que es un error, "
        "inicia sesión de nuevo para enviar una nueva solicitud."
    ),
    # -- banners de alerta del panel (renderizados en el servidor en el
    #    idioma web del operador; un formato de fecha vía format_slot) --
    "banner.aria_label": "Alertas del sistema",
    "banner.window_fallback": "la próxima ventana",
    "banner.cookie_expiring.heading": "La cookie caduca pronto",
    "banner.cookie_expiring.body": (
        "Se prevé que tu cookie de WodBuster caduque antes de {when}. Pega una cookie "
        "nueva en la página de Cookie para que las reservas sigan funcionando."
    ),
    "banner.cookie_invalid.heading": "Cookie rechazada",
    "banner.cookie_invalid.body": (
        "WodBuster rechazó la cookie almacenada. Las reservas están en pausa hasta que "
        "pegues una nueva."
    ),
    "banner.anomaly.heading": "Ejecución silenciosa detectada",
    "banner.anomaly.body": (
        "No se registró ningún resultado de reserva para una ventana que debería "
        "haberse cerrado. Revisa el worker."
    ),
    "banner.booking_fallback.heading": "Clase sustituida",
    "banner.booking_fallback.body": (
        "Se reservó {klass} \u2014 {when} en lugar de {requested_class} a las "
        "{requested_time}, que es lo que habías pedido, porque {reason}."
    ),
    "banner.unknown.heading": "Alerta: {kind}",
    "banner.unknown.body": "Consulta los registros para más detalles.",
}


CATALOGS: dict[str, dict[str, str]] = {
    "en": EN,
    "es": ES,
}


__all__ = ["CATALOGS", "DEFAULT_LANG", "EN", "ES", "SUPPORTED_LANGUAGES"]
