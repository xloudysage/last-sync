# Changelog

## 1.1.0 — 2026-11-24

- Preserve the supplied PyQt6 window, artwork widget, activity panel, theme and tray workflow.
- Introduce explicit synchronization states and confirm Spotify playback before reporting success.
- Follow track changes without inventing a source playback position.
- Recognize repeats when idle transitions or newer scrobbles supply evidence.
- Reconcile uncertain writes without blindly replaying playback commands.
- Stop on local playback overrides; recover from missing devices and network failures.
- Remove unrelated fallback playback, first-result matching and swallowed search errors.
- Score title, artist, recording version, duration and ISRC when available; cache mappings in versioned SQLite.
- Allow manual correction of ambiguous matches and isolate mappings by Spotify account.
- Respect Retry-After and service-specific exponential backoff.
- Derive progress from Spotify snapshots with monotonic interpolation and stale-data detection.
- Replace client-secret authorization and plaintext token files with cancellable PKCE and OS credential storage.
- Migrate legacy settings safely and mask API keys by default.
- Cancel background workers without blocking the GUI; retain workers until completion.
- Bound artwork downloads, reject stale results and create QPixmaps on the GUI thread.
- Add deterministic core, HTTP integration and native Qt tests.

PKCE, secure configuration and a small service split were brought forward from 1.2 to close audit findings. This does not mark the broader 1.2 milestone complete.

## Roadmap status

- 1.1: implemented and verified locally; real-account acceptance checks remain.
- 1.2: foundations present; device selector, broader persistence and further UI separation remain.
- 2.0–3.0: not implemented in this milestone. No Friends dashboard, Music DNA, collections, historical analytics or cooperative protocol is claimed.
