"""Public settings plus OS-protected credentials; no plaintext fallback."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile

DEFAULT_CONFIG = {"lastfm_user": "", "lastfm_api_key": "", "spotify_client_id": ""}
CONFIG_FILE = Path.home() / ".lastsync_config.json"


def data_directory():
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "LastSync"


class CredentialError(RuntimeError):
    pass


class WindowsCredentials:
    """Windows Credential Manager fallback when Python keyring is unavailable."""

    def __init__(self):
        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]
        self.CREDENTIAL = CREDENTIAL
        self.api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredFree.restype = None

    def get_password(self, service, account):
        ptr = ctypes.POINTER(self.CREDENTIAL)()
        if not self.api.CredReadW(service + "/" + account, 1, 0, ctypes.byref(ptr)):
            if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
                return None
            raise CredentialError("Windows Credential Manager could not be read")
        try:
            return ctypes.string_at(ptr.contents.CredentialBlob,
                                    ptr.contents.CredentialBlobSize).decode("utf-8")
        finally:
            self.api.CredFree(ptr)

    def set_password(self, service, account, value):
        raw = value.encode("utf-8")
        if len(raw) > 2560:
            raise CredentialError("Credential is too large for Windows Credential Manager")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = self.CREDENTIAL()
        credential.Type, credential.Persist = 1, 2
        credential.TargetName, credential.UserName = service + "/" + account, account
        credential.CredentialBlobSize, credential.CredentialBlob = len(raw), blob
        if not self.api.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError("Windows Credential Manager could not save the credential")


class SecretStore:
    def __init__(self, backend=None):
        if backend is not None:
            self.backend = backend
        elif sys.platform == "win32":
            # Use a stable backend identity: installing keyring later must not orphan tokens.
            self.backend = WindowsCredentials()
        else:
            try:
                import keyring
                self.backend = keyring
            except ImportError:
                raise CredentialError("Install keyring and configure an OS credential backend") from None

    def get(self, account):
        try:
            return self.backend.get_password("LastSync", account)
        except Exception:
            raise CredentialError("OS credential storage is unavailable; no plaintext fallback is used") from None

    def set(self, account, value):
        try:
            self.backend.set_password("LastSync", account, value)
            if self.get(account) != value:
                raise CredentialError("Credential verification failed")
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("OS credential storage could not save the credential") from None


def save_config(cfg, path=CONFIG_FILE, secrets=None):
    secrets = secrets or SecretStore()
    # Commit the credential first. On failure the original legacy file remains intact.
    secrets.set("lastfm-api-key", cfg.get("lastfm_api_key", ""))
    public = {k: cfg.get(k, "") for k in ("lastfm_user", "spotify_client_id")}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="lastsync-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(public, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(path=CONFIG_FILE, secrets=None):
    path, secrets = Path(path), secrets or SecretStore()
    try:
        public = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(public, dict):
            raise ValueError
    except (OSError, ValueError):
        raise CredentialError("Settings file is unreadable; preserve it and repair its JSON") from None
    cfg = {k: str(public.get(k, "")) for k in DEFAULT_CONFIG}
    if public.get("lastfm_api_key") or "spotify_client_secret" in public:
        # Public client PKCE no longer needs a client secret.
        if not cfg["lastfm_api_key"]:
            cfg["lastfm_api_key"] = secrets.get("lastfm-api-key") or ""
        save_config(cfg, path, secrets)
    else:
        cfg["lastfm_api_key"] = secrets.get("lastfm-api-key") or ""
    return cfg
