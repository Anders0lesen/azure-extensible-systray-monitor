from __future__ import annotations

import ctypes
import os
import tkinter as tk
from tkinter import ttk

PALETTES = {
    "dark": {
        "bg": "#0b1017",
        "surface": "#121a24",
        "surface_alt": "#182331",
        "input": "#0d141d",
        "text": "#f1f5f9",
        "muted": "#9caabd",
        "border": "#2a394b",
        "accent": "#2f81f7",
        "accent_active": "#1f6feb",
        "select": "#214f86",
    },
    "light": {
        "bg": "#eef3f8",
        "surface": "#ffffff",
        "surface_alt": "#e7eef6",
        "input": "#ffffff",
        "text": "#17202b",
        "muted": "#536273",
        "border": "#c6d2df",
        "accent": "#0969da",
        "accent_active": "#0558b7",
        "select": "#bdd7f5",
    },
}


def apply_theme(root: tk.Misc, mode: str) -> None:
    palette = PALETTES[mode if mode in PALETTES else "dark"]
    style = ttk.Style(root)
    style.theme_use("clam")
    root.option_add("*Font", "{Segoe UI} 10")
    root.option_add("*tearOff", False)
    style.configure(".", background=palette["bg"], foreground=palette["text"])
    style.configure("TFrame", background=palette["bg"])
    style.configure("Surface.TFrame", background=palette["surface"])
    style.configure("TLabel", background=palette["bg"], foreground=palette["text"])
    style.configure(
        "Surface.TLabel", background=palette["surface"], foreground=palette["text"]
    )
    style.configure("Muted.TLabel", foreground=palette["muted"])
    style.configure("Title.TLabel", font=("Segoe UI Variable Display", 20, "bold"))
    style.configure("Section.TLabel", font=("Segoe UI Variable", 11, "bold"))
    style.configure(
        "Healthy.TLabel",
        foreground="#3fb950" if mode == "dark" else "#16752b",
        font=("Segoe UI Variable", 10, "bold"),
    )
    style.configure(
        "Failed.TLabel",
        foreground="#ff6b6b" if mode == "dark" else "#c62828",
        font=("Segoe UI Variable", 10, "bold"),
    )
    style.configure(
        "Unknown.TLabel",
        foreground=palette["muted"],
        font=("Segoe UI Variable", 10, "bold"),
    )
    style.configure(
        "TButton",
        background=palette["surface_alt"],
        foreground=palette["text"],
        bordercolor=palette["border"],
        padding=(12, 7),
    )
    style.map(
        "TButton",
        background=[("active", palette["border"]), ("pressed", palette["select"])],
    )
    style.configure(
        "Accent.TButton",
        background=palette["accent"],
        foreground="#ffffff",
        bordercolor=palette["accent"],
    )
    style.map(
        "Accent.TButton",
        background=[("active", palette["accent_active"])],
    )
    style.configure("Theme.TButton", padding=(9, 5))
    style.configure(
        "TEntry",
        fieldbackground=palette["input"],
        foreground=palette["text"],
        insertcolor=palette["text"],
        bordercolor=palette["border"],
        padding=7,
    )
    style.configure(
        "TCombobox",
        fieldbackground=palette["input"],
        background=palette["surface_alt"],
        foreground=palette["text"],
        arrowcolor=palette["text"],
        bordercolor=palette["border"],
        padding=5,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette["input"])],
        foreground=[("readonly", palette["text"])],
        selectbackground=[("readonly", palette["input"])],
        selectforeground=[("readonly", palette["text"])],
    )
    style.configure(
        "TCheckbutton", background=palette["bg"], foreground=palette["text"]
    )
    style.configure(
        "TRadiobutton", background=palette["bg"], foreground=palette["text"]
    )
    style.configure(
        "TLabelframe",
        background=palette["surface"],
        bordercolor=palette["border"],
        relief="solid",
    )
    style.configure(
        "TLabelframe.Label",
        background=palette["surface"],
        foreground=palette["text"],
        font=("Segoe UI Variable", 10, "bold"),
    )
    style.configure(
        "Treeview",
        background=palette["input"],
        fieldbackground=palette["input"],
        foreground=palette["text"],
        bordercolor=palette["border"],
        rowheight=28,
    )
    style.map(
        "Treeview",
        background=[("selected", palette["select"])],
        foreground=[("selected", palette["text"])],
    )
    style.configure(
        "Treeview.Heading",
        background=palette["surface_alt"],
        foreground=palette["text"],
        bordercolor=palette["border"],
        padding=7,
    )
    style.configure("TNotebook", background=palette["bg"], borderwidth=0)
    style.configure(
        "TNotebook.Tab",
        background=palette["surface_alt"],
        foreground=palette["muted"],
        padding=(14, 8),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", palette["surface"])],
        foreground=[("selected", palette["text"])],
    )
    style.configure("TPanedwindow", background=palette["border"])
    style.configure("TSeparator", background=palette["border"])
    _theme_widget_tree(root, palette)
    _theme_title_bars(root, mode == "dark")


def _theme_widget_tree(widget: tk.Misc, palette: dict[str, str]) -> None:
    try:
        widget.configure(background=palette["bg"])
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        if isinstance(child, (tk.Text, tk.Listbox)):
            child.configure(
                background=palette["input"],
                foreground=palette["text"],
                insertbackground=palette["text"],
                selectbackground=palette["select"],
                selectforeground=palette["text"],
                highlightbackground=palette["border"],
                highlightcolor=palette["accent"],
                relief="flat",
            )
        _theme_widget_tree(child, palette)


def _theme_title_bars(root: tk.Misc, dark: bool) -> None:
    if os.name != "nt":
        return
    windows = [
        root,
        *(child for child in root.winfo_children() if isinstance(child, tk.Toplevel)),
    ]
    for window in windows:
        try:
            window.update_idletasks()
            enabled = ctypes.c_int(1 if dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                window.winfo_id(), 20, ctypes.byref(enabled), ctypes.sizeof(enabled)
            )
        except (AttributeError, OSError, tk.TclError):
            continue
