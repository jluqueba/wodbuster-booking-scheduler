# 🏋️ WodBuster Booking Worker

> Popular CrossFit classes open at a fixed time and fill up in under 10 seconds. Booking by hand means logging in, racing the clock, and often losing the spot anyway. **This project books the class for you.**

You set up your preferred classes once. A small service then watches the clock and reserves your spot the instant each booking window opens, so you never land on the waitlist again.

## What it does

- **Books automatically** the moment a class window opens, with an optional backup class if the first one is full.
- **Simple web page** to manage everything: preferred classes, session cookie, booking history, and worker health.
- **Telegram notifications** on every outcome, so a booking is never a mystery. You can also check status, cancel a class, or book a one-off class straight from the chat.
- **Early warnings** (hours ahead) when your WodBuster session is about to expire, so you are never caught out at booking time.
- **Never fails silently.** If a scheduled run does not happen, that itself raises an alert.
- **Keeps your credentials safe.** Your WodBuster username and password are never stored. Only the session cookie is kept, encrypted.

## How it works, in plain terms

1. You sign in to the web page and paste your WodBuster session cookie once.
2. You create rules for the classes you want (for example, "Tuesday 19:00 CrossFit, or 20:00 if that is full").
3. The service runs quietly in the background on Azure. When a booking window opens, it makes the reservation in the first second.
4. You get a Telegram notification with the result, and you can check or cancel anything on the go.

It runs as a single always-on service on Microsoft Azure, using a scheduler for the timing, a small database for your rules and history, and Telegram for notifications.

## Documentation

- [Developer Guide](docs/DEVELOPER_GUIDE.md): the deep dive. Architecture and diagrams, the Azure services in use, how to run the project locally, how to deploy the infrastructure, and a full tour of the features, the Telegram commands, and every page.
- [Contributing](CONTRIBUTING.md): how to propose changes and the workflow to follow.
- [Security policy](SECURITY.md): how to report a security issue.
- [Code of Conduct](CODE_OF_CONDUCT.md): the standards expected in this community.
- [License](LICENSE): the terms this project is released under.

## For developers

Want to run the project locally, deploy it, or understand how it is built? Head to the [Developer Guide](docs/DEVELOPER_GUIDE.md) for the full walkthrough.

## Status

Personal project, designed for a single user with room to invite a friend or two later. Built for reliability and low latency first, at a cost that stays reasonable for personal use.
