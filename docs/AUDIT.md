# Existing-code audit and validation

## Inspection and architecture

Inspected every supplied file before editing: `lastsync.py`, `README.md`, `.env.example`, `.gitignore` and `LICENSE`. The extraction had no package manifest, tests, database, installer or Git metadata.

The original 43 KB single-file application combined custom Qt widgets, plaintext configuration, Spotipy authorization, a Last.fm polling QThread, catalog matching, playback and an artwork QThread. The track card, pulse indicator, progress bar, activity panel, main layout, theme, settings and tray workflow are preserved.

## Findings

| Original defect | Consequence | Repair |
| --- | --- | --- |
| First observation treated as song start | Invented source position | Track Follow starts at zero; source position remains unknown |
| First catalog result accepted | Wrong artist or performance could play | Rank candidates, reject recording-version conflicts, require confidence and disambiguation |
| Hardcoded fallback URI | Failed matches played unrelated music repeatedly | Remove fallback playback |
| Identity used only title and artist | Same-song repeats and idle returns missed | Event transitions and scrobble evidence |
| Failed resolution retried each poll | API pressure and repeated starts | Cache negative results and throttle re-resolution |
| Playback writes not confirmed | False success without playback | STARTING distinct from confirmed FOLLOWING |
| Lost write response confused with failure | Retried command could restart playback | Reconcile actual state; never blindly replay uncertain starts |
| User overrides ignored | App could fight local controls | Stop on pause, changed track or changed device |
| Search errors swallowed | Auth/network errors appeared as absent songs | Typed, redacted service errors and recovery states |
| Empty history indexed at zero | Idle responses threw exceptions | Parse empty, list and singleton responses |
| Last.fm HTTP-200 error bodies ignored | Invalid keys/users misclassified | Parse service error codes first |
| URL query manually concatenated | Special characters changed query meaning | Encoded parameters |
| Plaintext config and token files | Credentials exposed | PKCE, OS storage and safe migration |
| API key initially unmasked | Secret visible despite reveal control | Password mode at creation |
| GUI called QThread.wait | Stop/close froze during network/OAuth | Cooperative cancellation and asynchronous shutdown |
| Art worker references overwritten | Running thread could be destroyed | Retain until finished, cap concurrent loaders |
| QPixmap created in worker | GUI resource used outside GUI thread | Transfer bytes, build pixmap in GUI thread |
| Wall-clock-only progress | Pauses and failures displayed fictitious progress | Observed playback, monotonic interpolation, stale-data state |
| Metadata labels auto-detected rich text | Remote text could be markup | Explicit plain text |
| Save failure only printed | UI accepted unsaved settings | Preserve settings and display failure |

The small service split permits deterministic testing without Qt. Moving transport to httpx directly supports bounded operations, cancellable PKCE, typed failures and uncertain-write handling. Only official Spotify and Last.fm endpoints are used. No Rust or C++ component is justified by this workload.

The Windows packaging smoke test also exposed a build-host PATH collision: PyInstaller selected a Poppler ICU DLL with a different ABI from the built-in Windows ICU used by Qt. The build specification restores System32 search precedence and omits the conflicting private ICU DLLs. This is separate from application synchronization behavior.

## Local validation

Windows, Python 3.13.15, PyQt6 6.11.0 / Qt runtime 6.11.2, httpx 0.28.1. All 65 automated tests pass. The suite covers synchronization, parsing, Unicode/version matching, manual corrections, missing devices, network failures, rate limits, cancellation, command confirmation, database migration, credential migration, PKCE/state checks, revoked grants and native Qt rendering/lifecycle behavior, including a real QThread worker. The native window is also rendered and visually inspected offscreen. No listening data or credentials are fabricated as live results.

## Remaining external acceptance

With valid credentials and permitted API access:

1. Authorize an allowlisted Premium account using the registered loopback redirect.
2. Activate a device and follow two different Last.fm reported tracks.
3. Repeat a recording with an observable idle/scrobble boundary; check one start per detected event.
4. Pause/change local playback; verify following stops.
5. Disconnect the device and network; verify recovery does not restart the current recording.
6. Save a manual correction and verify it survives restarting under the same Spotify account.
7. Cancel authorization, stop during a request and quit from the tray; check responsiveness.

Missing credentials and restricted networking prevent these live checks in this workspace. A native Credential Manager read also returned access denied in the managed execution sandbox; real OS credential operations must be checked in the normal signed-in Windows session. Automated credential tests use an injected memory backend, and the application fails closed if the OS store is unavailable. Automated tests do not prove a particular developer app's real permissions. No live Spotify or Last.fm integration result is claimed.

## Limits

Last.fm reported activity is not verified remote Spotify playback. An identical now-playing record without new evidence cannot identify a repeat. Stop cannot recall an already-sent playback command. Shutdown waits asynchronously for bounded operations instead of forcibly terminating threads. Match confidence is a deterministic metadata score; a manual choice is an explicit override. Future roadmap versions are not included in this milestone.
