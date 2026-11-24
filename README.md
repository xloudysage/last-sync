# LastSync 1.1

A native Windows companion that follows a public Last.fm user's reported current track on your authorized Spotify account.

This release repairs the supplied application before the larger LastSync 2.0 expansion. It preserves the native PyQt6 interface and tray workflow. It implements **Track Follow**: Last.fm does not provide a reliable live position, so a newly detected listening event starts at the beginning on your Spotify.

## Requirements

- Windows 11; Python 3.12+ when running from source.
- Spotify Premium for playback control and a developer app authorized for your account.
- Development Mode currently requires the app owner to have Premium and allows up to five authorized users.
- Spotify open on an active, unrestricted playback device. Play a track once to activate it.
- A Last.fm API key and public now-playing visibility for the source profile.

## Run

For a portable package containing `LastSync.exe`, extract the whole package and double-click the executable. Python is not needed for that executable.

From source, use an installed Python 3.12+ interpreter, for example:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe lastsync.py
```

Alternatively, `python -m pip install .` installs the `lastsync` launcher. The `.env.example` file is documentation; the application does not load `.env` files.

## Connect

1. Create a [Last.fm API key](https://www.last.fm/api/account/create).
2. Create an app in the [Spotify developer dashboard](https://developer.spotify.com/dashboard).
3. Register exactly `http://127.0.0.1:8888/callback` as a redirect URI.
4. In Settings, enter the source Last.fm username, API key and Spotify **Client ID**. A client secret is no longer needed.
5. Open Spotify, play once, then click **Start Sync** and authorize in your system browser.

PKCE authorization checks a random state, binds only to loopback and expires after three minutes. Cancel interrupts the listener. Port 8888 must be available. HTTP requests have bounded timeouts.

## Following and matching

- New reported tracks are ranked by title, artist and recording version. Duration and ISRC contribute when supplied; missing data is not fabricated.
- Unambiguous high-confidence matches start from zero on your active device. Following is displayed only after Spotify confirms playback.
- Same-song repeats count as new events when an idle boundary or a newer same-track scrobble supplies evidence. An uninterrupted identical now-playing record without new evidence cannot reveal a repeat; LastSync does not guess from duration.
- Unavailable or ambiguous matches leave playback unchanged. **Choose Spotify Match** selects a candidate or accepts a pasted Spotify track link. Your explicit choice is saved for this source recording and Spotify account.
- A manual Spotify pause, track change or device switch stops following. Press Start to opt back in.
- Device loss does not restart the current recording. A future source event can start after a device becomes active again.
- Stop ends LastSync control and leaves Spotify playing. An already-sent command cannot be recalled.

The progress bar describes **your Spotify playback**, never an estimated source position. It uses observed snapshots, briefly interpolates with a monotonic clock and marks data stale after ten seconds without an update. Match confidence is a metadata score, not a calibrated probability; a manual correction is a user decision.

## Desktop behavior

Closing hides to the tray when available. Use **Show LastSync** to restore it and **Quit** to cancel workers and exit after in-flight requests finish. Enter toggles Start/Stop. Stop the session before changing Settings. Network operations run outside the GUI thread. Artwork downloads are bounded and stale responses are ignored.

## Data and migration

`~/.lastsync_config.json` holds the source username and public Client ID. On Windows, Last.fm keys and Spotify tokens use Windows Credential Manager under `LastSync/` entries. Other platforms require Python keyring with an OS backend. There is no plaintext credential fallback.

Legacy configuration is migrated on load: the Last.fm key is verified in credential storage before secret fields are removed from JSON. The obsolete client secret is no longer used. The legacy `.lastsync_spotify_cache` is removed after successful PKCE authorization; expect to authorize once again. Failed migration preserves the original configuration and displays an error.

Mappings live in `%LOCALAPPDATA%/LastSync/matches.sqlite3` on Windows. Automatic mappings expire after 30 days; negative mappings after five minutes. The automatic cache is bounded to 5,000 records. Manual corrections last up to ten years. Mappings are isolated by app and authenticated Spotify account. Full histories and sessions are not stored in this milestone.

To reset mappings, stop the app and remove the match database; this also removes corrections. Remove `LastSync/` entries in Windows Credential Manager to remove saved account credentials. Do not distribute your settings, database or credential exports.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q lastsync.py lastsync_core
.\.venv\Scripts\python.exe lastsync.py --check
```

Tests use deterministic clocks, mocked HTTP and offscreen native Qt. They neither contact music services nor touch real settings. See [the audit](docs/AUDIT.md) for findings and remaining real-account checks.

## Build executable and installer

```powershell
.\.venv\Scripts\python.exe -m pip install '.[build]'
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm packaging/LastSync.spec
```

Build on Windows with Python 3.12+. The output is `dist/LastSync.exe`, a native windowed executable with Qt/httpx and no UPX.

For an installer, install [Inno Setup 6](https://jrsoftware.org/isinfo.php) and compile `packaging/LastSync.iss` with `ISCC.exe`. It installs per-user without administrator rights and creates Start Menu shortcuts. Uninstall preserves credentials and settings. An uncompiled `.iss` file is not an installer.

## Architecture and roadmap

`lastsync.py` retains the original native widgets and window. `lastsync_core/` separates models, adapters, matching, SQLite, synchronization, secure configuration, PKCE and Qt workers.

This is the **1.1 stabilization milestone**, including security foundations brought forward from 1.2. The device selector and broader storage, multi-user dashboard, Music DNA, collections, historical analytics and optional direct protocol remain later milestones. No placeholder screens stand in for these features.

## Verified service references

- [Spotify playback](https://developer.spotify.com/documentation/web-api/reference/start-a-users-playback)
- [PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow) and [redirect URIs](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)
- [Rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits)
- [Development Mode migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)
- [Last.fm recent tracks](https://www.last.fm/api/show/user.getRecentTracks)

Search requests use at most ten results each and tolerate missing catalog fields. Actual access remains subject to the app's scopes and access mode. LastSync controls your own client; it does not stream audio or bypass platform restrictions.
