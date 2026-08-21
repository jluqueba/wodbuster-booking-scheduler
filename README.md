# 🏋️ WodBuster Booking Worker

> Popular CrossFit classes open at a fixed time and fill up in under 10 seconds. Booking by hand means logging in, racing the clock, and often losing the spot anyway. **This project books the class for you.**

You set up your preferred classes once. A small service then watches the clock and reserves your spot the instant each booking window opens, so you never land on the waitlist again.

## What it does

- **Books automatically** the moment a class window opens, with an optional backup class if the first one is full.
- **Books at every gym you can access.** Your WodBuster gyms appear automatically and one login covers them all; each gym books independently.
- **Simple web page** to manage everything: preferred classes, session cookie, booking history, and worker health.
- **Dashboard, Telegram, and email notifications** for booking results and session alerts. Every message names the gym it concerns, and email categories can be controlled from your profile.
- **Personal profiles in English or Spanish** with editable name, email address, communication language, and notification preferences.
- **Controlled multi-user access.** New sign-ins wait for administrator approval; administrators can suspend, restore, or delete regular users without seeing or changing their booking data.
- **Early warnings** (hours ahead) when your WodBuster session is about to expire, so you are never caught out at booking time.
- **Never fails silently.** If a scheduled run does not happen, that itself raises an alert.
- **Keeps your credentials safe.** Your WodBuster username and password are never stored. Only the session cookie is kept, encrypted.

## How it works, in plain terms

1. You sign in with Microsoft, GitHub, or Google. New accounts wait for administrator approval; the first administrator is created with the bootstrap command.
2. You paste your WodBuster session cookie once; it applies to every gym you can access, and your gyms are detected automatically.
3. You create rules for the classes you want (for example, "Tuesday 19:00 CrossFit, or 20:00 if that is full").
4. The service runs quietly in the background on Azure. When a booking window opens, it makes the reservation in the first second.
5. The result appears in the dashboard and, when configured, in Telegram and email.

It runs as a single always-on service on Microsoft Azure, using PostgreSQL for durable state, Azure Communication Services for email, and a scheduler for precise timing.

## Documentation

- [Developer Guide](docs/DEVELOPER_GUIDE.md): the deep dive. Architecture and diagrams, the Azure services in use, how to run the project locally, how to deploy the infrastructure, and a full tour of the features, the Telegram commands, and every page.
- [Contributing](CONTRIBUTING.md): how to propose changes and the workflow to follow.
- [Security policy](SECURITY.md): how to report a security issue.
- [Code of Conduct](CODE_OF_CONDUCT.md): the standards expected in this community.
- [License](LICENSE): the terms this project is released under.

## For developers

Want to run the project locally, deploy it, or understand how it is built? Head to the [Developer Guide](docs/DEVELOPER_GUIDE.md) for the full walkthrough.

## Status

Personal project supporting several approved users and multiple gyms per user. It is built for reliable, low-latency bookings with deliberately small operational overhead.
