from __future__ import annotations

import logging
import sys
from pathlib import Path
from tkinter import Misc, TclError

LOGGER = logging.getLogger(__name__)


def brand_icon_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        root = Path(__file__).resolve().parents[2]
    return root / "assets" / "AzureHealthBeacon.ico"


def apply_window_branding(window: Misc) -> None:
    """Apply the brand icon to app windows, never to the status tray surface."""
    icon = brand_icon_path()
    if not icon.exists():
        return
    try:
        window.iconbitmap(default=str(icon))
    except TclError:
        LOGGER.debug("Could not apply the window brand icon", exc_info=True)
