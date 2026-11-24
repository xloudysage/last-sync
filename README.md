# LastSync 2.0

Follow public Last.fm profiles in one native Windows dashboard. See each friend's reported activity, browse their recent tracks, and play a match on your own Spotify device.

## Friends

Open **Settings** and enter a Last.fm API key. Spotify authorization is only required when you choose playback. Type a Last.fm username in the search box and press **Add**. The same box filters your roster. Select a friend to see their profile picture and cover art when available, artist, album, last check, last scrobble and Spotify matching status. Use **Move up**, **Move down** or **Remove** to organize the list.

| Mode | How to use it |
| --- | --- |
| Track Follow | Click **Listen along**. New source tracks start at zero on your chosen Spotify output. |
| Best-Effort Sync | If you know the source player's current position, enter it as minutes:seconds just before clicking **Listen along**. LastSync adjusts for elapsed time at the start of playback. The position expires after 30 seconds and requires the same observed play, a fresh report and a known matching track duration. Otherwise it falls back to Track Follow. |
| Discovery | Pick a reported track from the friend's recent list, then click **Play selected track**. After Spotify confirms the start, the session ends and your player keeps playing. Browsing never starts a song. |

To switch during playback, open **Friends**, select another profile and start the new choice. LastSync finishes stopping the old session before reusing your authorized Spotify connection and output. **Player** shows matches, corrections, your playback progress and session activity. To change the output, stop and start a new session.

A Last.fm now-playing flag is the source's reported activity. The scrobble timestamp and local check time appear separately. Spotify confirms playback on your own device. Last.fm does not report a friend's live playhead.

## Requirements

- Windows 11; Python 3.12+ when running from source.
- Spotify Premium for playback control and a developer app authorized for your account.
- Development Mode currently requires the app owner to have Premium and allows up to five authorized users.
- Spotify running on an available, unrestricted playback device.
- A Last.fm API key and public now-playing visibility for the source profile.

## Run

On Windows x64, run `LastSync-2.0.0-Setup.exe` for a per-user installation. For the portable build, extract `LastSync-v2.0-Windows.zip` and open `LastSync.exe`. Both include the Python and Qt runtime.

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
4. In Settings, enter your Last.fm API key and Spotify **Client ID**. A default Last.fm username is optional. A client secret is no longer needed.
5. Open Spotify on the output you want to use. Click **Listen along** on a friend or **Start Sync** in Player, then authorize in your system browser.
6. Choose an available device and click **Start following**. **Refresh** reads a new device list. Restricted devices and devices without a usable ID are disabled. **Use the active Spotify device** keeps the v1.1 behavior. If Spotify lists nothing, open the Spotify client, play once, and refresh.
7. Stop and start again to choose another output. Explicit device selections last for the session. If the chosen device disappears, LastSync waits for it. Playback must be confirmed on the target device before a start is counted.

PKCE authorization checks a random state, binds only to loopback and expires after three minutes. Cancel interrupts the listener. Port 8888 must be available. HTTP requests have bounded timeouts.

## Following and matching

- New reported tracks are ranked by title, artist and recording version. Duration and ISRC contribute when supplied; missing data is not fabricated.
- Unambiguous high-confidence matches start on your chosen output. Following is displayed only after Spotify confirms playback.
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

The v1.1/v1.2 database at `%LOCALAPPDATA%/LastSync/matches.sqlite3` upgrades in place to schema 3 when the application opens. The migration runs in a transaction and retains manual corrections. Automatic mappings expire after 30 days, empty results after five minutes, and manual corrections after ten years. The database holds at most 5,000 automatic mappings.

The database also records each followed profile and each session's start, end, outcome, device-selection mode and number of confirmed playback starts. These times describe local sessions. If Windows terminates the process, the unfinished session keeps an empty end time. Catalog search responses are cached per Spotify account and client for ten minutes, up to 256 entries of 256 KiB each. Device lists and Spotify playback are fetched on demand. A single Last.fm worker shares recent observations between the player and Friends. It schedules the source being followed every two seconds, other active profiles every twelve seconds, and idle profiles every thirty-five seconds, subject to network delays and service backoff. Requests are serialized and spaced at least 350 ms apart. A service rate limit pauses the entire roster. The player requires a successful observation no older than eight seconds.

The ordered friend list is local to this installation. The previous default source is imported once. Removing a friend preserves historical sessions. Avatars are refreshed every six hours. Artwork uses two concurrent thumbnail downloads and a bounded in-memory cache. Last.fm errors keep prior observations visible with an error status.

The activity panel shows recent user-facing events. Rotating diagnostic logs in the same directory contain session lifecycle messages only. Quit LastSync before removing the database to clear sessions, profiles, cached searches and corrections. Removing the `LastSync/` entries in Windows Credential Manager clears stored credentials.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q lastsync.py lastsync_core lastsync_ui
.\.venv\Scripts\python.exe lastsync.py --check
```

Tests use deterministic clocks, mocked HTTP and offscreen native Qt. They neither contact music services nor touch real settings. See [the audit](docs/AUDIT.md) for findings and remaining real-account checks.

## Build executable and installer

```powershell
.\.venv\Scripts\python.exe -m pip install '.[build]'
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm packaging/LastSync.spec
```

Build on Windows with Python 3.12+. The output is `dist/LastSync.exe`, a native windowed executable with Qt/httpx and no UPX.

For an installer, install [Inno Setup 6](https://jrsoftware.org/isinfo.php) and compile `packaging/LastSync.iss` with `ISCC.exe`. It installs per-user without administrator rights and creates Start Menu shortcuts. Uninstall preserves credentials and settings. The installer is emitted alongside the portable executable in `dist/`.

## Architecture and roadmap

`lastsync.py` is the launcher. `lastsync_ui/` owns the native window, dialogs, theme, reusable widgets and activity panel. In `lastsync_core/`, `lastfm.py` and `spotify.py` share the bounded transport in `transport.py`. Separate modules own PKCE, secure configuration, background workers, track resolution, synchronization, SQLite and diagnostics. The v1.1 service import path remains available.

The next roadmap milestone is v2.1, Music DNA and profile comparison.

## Verified service references

- [Spotify devices](https://developer.spotify.com/documentation/web-api/reference/get-a-users-available-devices)
- [Spotify playback](https://developer.spotify.com/documentation/web-api/reference/start-a-users-playback)
- [PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow) and [redirect URIs](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)
- [Rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits)
- [Development Mode migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)
- [Last.fm profile information](https://www.last.fm/api/show/user.getInfo)
- [Last.fm recent tracks](https://www.last.fm/api/show/user.getRecentTracks)

Search requests use at most ten results each and tolerate missing catalog fields. Actual access remains subject to the app's scopes and access mode. LastSync controls your own client; it does not stream audio or bypass platform restrictions.
