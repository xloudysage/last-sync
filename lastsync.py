import sys
import os
from PyQt6.QtWidgets import QApplication
from lastsync_core.config import DEFAULT_CONFIG as _DEFAULT_CONFIG
from lastsync_ui.window import LastSyncWindow
from lastsync_core.config import data_directory
from lastsync_core.diagnostics import configure
from lastsync_ui.dialogs import MatchDialog, SettingsDialog


def main(argv=None):
    if sys.version_info < (3, 12):
        raise SystemExit("LastSync requires Python 3.12 or newer")
    argv = list(sys.argv if argv is None else argv)
    check = "--check" in argv
    if check:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    else:
        configure(data_directory())
    app = QApplication(argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    window = LastSyncWindow(dict(_DEFAULT_CONFIG), prompt_settings=False) if check else LastSyncWindow()
    window.show()
    if check:
        app.processEvents()
        if window.grab().isNull():
            return 1
        window._tray.hide()
        window._closing = True
        window.close()
        print("LastSync native startup check passed (no accounts or network used)")
        return 0
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
