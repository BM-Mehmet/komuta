#!/usr/bin/env python3
"""Komuta: a small keyboard-first launcher and clipboard manager for Linux."""

from __future__ import annotations

import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QSize, QThread, QObject, pyqtSignal, pyqtSlot, QByteArray, QBuffer, QIODevice, QUrl, QMimeData
from PyQt6.QtGui import QAction, QKeySequence, QIcon, QPixmap, QImage, QImageReader, QColor, QTextCursor
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QGraphicsDropShadowEffect,
    QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget,
)

APP_NAME = "Komuta"
DEFAULT_CATEGORIES = ["Genel", "Bağlantılar", "Kod", "Metin", "Görseller"]
MAX_HISTORY = 200
MAX_PINNED = 50
MAX_FILES = 100
MAX_TEXT_BYTES = 200 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_STORAGE = 100 * 1024 * 1024
FILE_SEARCH_BATCH = 100
URL_ONLY = re.compile(r"^(?:https?://|ftp://|www\.)[^\s]+$", re.IGNORECASE)
HOME = Path.home()
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "komuta"
DB_PATH = STATE_DIR / "clipboard.db"


def database() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS clips (
        id INTEGER PRIMARY KEY, content TEXT NOT NULL UNIQUE, category TEXT NOT NULL DEFAULT 'Genel',
        created REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 0)""")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(clips)")}
    if "kind" not in columns:
        db.execute("ALTER TABLE clips ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")
    if "payload" not in columns:
        db.execute("ALTER TABLE clips ADD COLUMN payload BLOB")
    if "mime" not in columns:
        db.execute("ALTER TABLE clips ADD COLUMN mime TEXT")
    if "thumbnail" not in columns:
        db.execute("ALTER TABLE clips ADD COLUMN thumbnail BLOB")
    if "dimensions" not in columns:
        db.execute("ALTER TABLE clips ADD COLUMN dimensions TEXT")
    if "category_auto" not in columns:
        db.execute("ALTER TABLE clips ADD COLUMN category_auto INTEGER NOT NULL DEFAULT 1")
    db.execute("CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY, position INTEGER NOT NULL, regex TEXT)")
    category_columns = {row["name"] for row in db.execute("PRAGMA table_info(categories)")}
    if "regex" not in category_columns:
        db.execute("ALTER TABLE categories ADD COLUMN regex TEXT")
    if not db.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
        db.executemany("INSERT INTO categories(name, position) VALUES(?, ?)",
                       [(name, i) for i, name in enumerate(DEFAULT_CATEGORIES)])
    db.commit()
    return db


@dataclass
class Result:
    kind: str
    title: str
    subtitle: str
    value: str
    icon: QIcon | None = None
    category: str = "Genel"
    pinned: bool = False
    clip_id: int | None = None
    clip_kind: str = "text"
    payload: bytes | None = None
    mime: str = ""
    thumbnail: bytes | None = None


class CatalogWorker(QThread):
    ready = pyqtSignal(list)

    def run(self):
        entries: list[Result] = []
        seen: set[str] = set()
        app_dirs = [Path("/usr/share/applications"), HOME / ".local/share/applications"]
        for folder in app_dirs:
            if not folder.is_dir():
                continue
            try:
                for desktop in folder.glob("*.desktop"):
                    try:
                        raw = desktop.read_text(errors="replace")
                        if "NoDisplay=true" in raw or "Hidden=true" in raw:
                            continue
                        name = self._field(raw, "Name")
                        command = self._field(raw, "Exec")
                        if not name or not command or name.lower() in seen:
                            continue
                        seen.add(name.lower())
                        icon_name = self._field(raw, "Icon")
                        entries.append(Result("app", name, "Uygulama", command,
                                              QIcon.fromTheme(icon_name) if icon_name else QIcon()))
                    except (OSError, UnicodeError):
                        continue
            except OSError:
                continue

        self.ready.emit(entries)

    @staticmethod
    def _field(raw: str, field: str) -> str:
        match = re.search(rf"^{re.escape(field)}=(.*)$", raw, re.MULTILINE)
        return match.group(1).replace("%U", "").replace("%u", "").replace("%F", "").replace("%f", "").strip() if match else ""

class FileSearchWorker(QThread):
    matchesFound = pyqtSignal(object, str, list)

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self):
        query = self.query.casefold()
        batch: list[Result] = []
        for current, dirs, files in os.walk("/", followlinks=False):
            if self.isInterruptionRequested():
                return
            if current == "/":
                dirs[:] = [name for name in dirs if name not in {"proc", "sys", "dev", "run"}]
            for dirname in dirs:
                if self.isInterruptionRequested():
                    return
                path = Path(current) / dirname
                if query in (dirname + " " + current).casefold():
                    batch.append(Result("directory", dirname, current, str(path)))
                if len(batch) >= FILE_SEARCH_BATCH:
                    self.matchesFound.emit(self, self.query, batch)
                    batch = []
            for filename in files:
                if self.isInterruptionRequested():
                    return
                if query in (filename + " " + current).casefold():
                    path = Path(current) / filename
                    batch.append(Result("file", filename, current, str(path)))
                if len(batch) >= FILE_SEARCH_BATCH:
                    self.matchesFound.emit(self, self.query, batch)
                    batch = []
        if batch and not self.isInterruptionRequested():
            self.matchesFound.emit(self, self.query, batch)


class ToggleService(QObject):
    """Small D-Bus endpoint so the KDE hotkey reuses this running process."""

    def __init__(self, window):
        super().__init__()
        self.window = window

    @pyqtSlot()
    def toggle(self):
        self.window.show_launcher()


class KomutaWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Komuta")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(760, 500)
        self.db = database()
        self._recategorize_automatic_clips()
        self.mode = "all"
        self.category = ""
        self.editing_clip: Result | None = None
        self.catalog: list[Result] = []
        self.results: list[Result] = []
        self.file_search_query: str | None = None
        self.file_search_matches: list[Result] = []
        self.search_filter = "all"
        self.search_results: list[Result] = []
        self.file_search_worker: FileSearchWorker | None = None
        self.file_search_workers: list[FileSearchWorker] = []
        self.loaded_result_count = 0
        self.loading_result_batch = False
        self.last_clip = None
        self._build_ui()
        self._style()
        self._build_tray()
        self.nav_all.setChecked(True)
        self.local_server = QLocalServer(self)
        self.local_server.newConnection.connect(self.handle_local_request)
        QLocalServer.removeServer("komuta-launcher")
        self.local_server.listen("komuta-launcher")
        self._register_dbus_service()
        self.clipboard = QApplication.clipboard()
        self.clipboard.dataChanged.connect(self.capture_clipboard)
        self.worker = CatalogWorker()
        self.worker.ready.connect(self.on_catalog)
        self.worker.finished.connect(self.on_catalog_finished)
        self.worker.start()
        self.file_search_timer = QTimer(self)
        self.file_search_timer.setSingleShot(True)
        self.file_search_timer.setInterval(450)
        self.file_search_timer.timeout.connect(self.start_file_search)
        self.status.setText("● Hazır")
        self.refresh()
        self.search.setFocus()
        self.capture_clipboard()
        # Regular Qt shortcuts work while the launcher is open, including on Wayland.
        self.toggle_action = QAction(self)
        self.toggle_action.setShortcut(QKeySequence("Alt+Space"))
        self.toggle_action.triggered.connect(self.toggle)
        self.addAction(self.toggle_action)
        self._bind_shortcut("Ctrl+1", lambda: self.set_mode("all"))
        self._bind_shortcut("Ctrl+2", lambda: self.set_mode("apps"))
        self._bind_shortcut("Ctrl+3", lambda: self.set_mode("files"))
        self._bind_shortcut("Ctrl+K", self.focus_search)
        self._bind_shortcut("Alt+Right", lambda: self.cycle_category(1))
        self._bind_shortcut("Alt+Left", lambda: self.cycle_category(-1))
        self._bind_shortcut("Ctrl+Shift+P", self.toggle_selected_pin)
        self._bind_shortcut("Ctrl+Shift+M", self.move_selected_category)
        self._bind_shortcut("Ctrl+Shift+N", self.add_category)
        self._bind_shortcut("Ctrl+Shift+Delete", self.clear_history)
        self._bind_shortcut("Delete", self.delete_selected_clip)

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("surface")
        self.setCentralWidget(root)
        shadow = QGraphicsDropShadowEffect(root)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 7)
        shadow.setColor(QColor(0, 0, 0, 145))
        root.setGraphicsEffect(shadow)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 16)
        outer.setSpacing(12)

        brand = QHBoxLayout()
        title = QLabel("KOMUTA")
        title.setObjectName("brandName")
        subtitle = QLabel("/ hızlı arama  ·  pano geçmişi")
        subtitle.setObjectName("dimText")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        brand.addStretch()
        self.status = QLabel("● LISTENING")
        self.status.setObjectName("status")
        brand.addWidget(self.status)
        outer.addLayout(brand)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 4, 0, 4)
        search_row.setSpacing(10)
        self.prompt = QLabel("❯")
        self.prompt.setObjectName("prompt")
        search_row.addWidget(self.prompt)
        self.search = QLineEdit()
        self.search.setObjectName("searchInput")
        self.search.setPlaceholderText("panoda, uygulamalarda veya dosyalarda ara")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.search.returnPressed.connect(lambda: self.activate_item(self.list.currentItem()))
        search_row.addWidget(self.search, 1)
        outer.addLayout(search_row)

        separator = QFrame()
        separator.setObjectName("terminalRule")
        separator.setFixedHeight(1)
        outer.addWidget(separator)

        nav_row = QHBoxLayout()
        nav_row.setSpacing(5)
        nav_row.addWidget(self._nav_button("PANO", "all"))
        nav_row.addWidget(self._nav_button("UYGULAMA", "apps"))
        nav_row.addWidget(self._nav_button("DOSYA", "files"))
        nav_row.addStretch()
        nav_row.addSpacing(12)
        category_label = QLabel("KATEGORİLER:")
        category_label.setObjectName("dimText")
        nav_row.addWidget(category_label)
        self.category_layout = nav_row
        nav_row.addStretch()
        add_cat = QPushButton("+")
        add_cat.setObjectName("addCategory")
        add_cat.setToolTip("Kategori ekle")
        add_cat.clicked.connect(self.add_category)
        nav_row.addWidget(add_cat)
        outer.addLayout(nav_row)

        list_header = QHBoxLayout()
        self.heading = QLabel("# pano geçmişi")
        self.heading.setObjectName("heading")
        self.result_label = QLabel("SON KULLANILANLAR")
        self.result_label.setObjectName("sectionLabel")
        self.result_count = QLabel("")
        self.result_count.setObjectName("countLabel")
        list_header.addWidget(self.heading)
        list_header.addSpacing(14)
        list_header.addWidget(self.result_label)
        list_header.addStretch()
        list_header.addWidget(self.result_count)
        outer.addLayout(list_header)

        self.filter_row = QWidget()
        filter_layout = QHBoxLayout(self.filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(4)
        filter_label = QLabel("TÜR:")
        filter_label.setObjectName("dimText")
        filter_layout.addWidget(filter_label)
        self.filter_buttons: dict[str, QPushButton] = {}
        for key, label in (("all", "TÜMÜ"), ("files", "DOSYA"), ("images", "GÖRSEL"), ("directories", "KLASÖR"), ("apps", "UYGULAMA")):
            button = QPushButton(label)
            button.setObjectName("categoryButton")
            button.setCheckable(True)
            button.setChecked(key == self.search_filter)
            button.clicked.connect(lambda _=False, filter_key=key: self.set_search_filter(filter_key))
            filter_layout.addWidget(button)
            self.filter_buttons[key] = button
        filter_layout.addStretch()
        self.filter_row.hide()
        outer.addWidget(self.filter_row)

        self.list = QListWidget()
        self.list.setObjectName("resultList")
        self.list.setSpacing(1)
        self.list.itemActivated.connect(self.activate_item)
        self.list.currentItemChanged.connect(self.show_file_preview)
        self.list.itemClicked.connect(self.show_file_preview)
        self.list.verticalScrollBar().valueChanged.connect(self.load_more_results_if_needed)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.context_menu)
        results_body = QWidget()
        results_layout = QHBoxLayout(results_body)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(12)
        results_layout.addWidget(self.list, 1)

        self.preview_panel = QFrame()
        self.preview_panel.setObjectName("previewPanel")
        self.preview_panel.setFixedWidth(270)
        preview_layout = QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(12, 12, 12, 12)
        preview_layout.setSpacing(8)
        self.preview_title = QLabel("DOSYA ÖNİZLEMESİ")
        self.preview_title.setObjectName("heading")
        self.preview_title.setWordWrap(True)
        self.preview_meta = QLabel("")
        self.preview_meta.setObjectName("dimText")
        self.preview_meta.setWordWrap(True)
        self.preview_image = QLabel()
        self.preview_image.setObjectName("previewImage")
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setFixedSize(244, 190)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setObjectName("previewText")
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumBlockCount(1000)
        self.preview_text.hide()
        self.preview_image.hide()
        self.preview_info = QLabel("")
        self.preview_info.setObjectName("dimText")
        self.preview_info.setWordWrap(True)
        preview_layout.addWidget(self.preview_title)
        preview_layout.addWidget(self.preview_meta)
        preview_layout.addWidget(self.preview_image, 0, Qt.AlignmentFlag.AlignHCenter)
        preview_layout.addWidget(self.preview_text, 1)
        preview_layout.addWidget(self.preview_info, 1)
        self.preview_panel.hide()
        results_layout.addWidget(self.preview_panel)
        outer.addWidget(results_body, 1)

        self.edit_panel = QWidget()
        edit_layout = QVBoxLayout(self.edit_panel)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        edit_layout.setSpacing(8)
        self.edit_input = QPlainTextEdit()
        self.edit_input.setObjectName("clipEditor")
        self.edit_input.setPlaceholderText("Pano metni")
        edit_layout.addWidget(self.edit_input, 1)
        edit_actions = QHBoxLayout()
        self.edit_status = QLabel("")
        self.edit_status.setObjectName("dimText")
        edit_actions.addWidget(self.edit_status, 1)
        cancel_edit = QPushButton("Vazgeç")
        cancel_edit.setObjectName("clearButton")
        cancel_edit.clicked.connect(self.cancel_edit_clip)
        save_edit = QPushButton("Kaydet")
        save_edit.setObjectName("navButton")
        save_edit.clicked.connect(self.save_edit_clip)
        edit_actions.addWidget(cancel_edit)
        edit_actions.addWidget(save_edit)
        edit_layout.addLayout(edit_actions)
        self.edit_panel.hide()
        outer.addWidget(self.edit_panel, 1)

        footer = QHBoxLayout()
        footer_text = QLabel("↑↓ gezin   ↵ seç/kopyala   esc kapat")
        footer_text.setObjectName("footerText")
        footer.addWidget(footer_text)
        footer.addStretch()
        clear = QPushButton("Pano geçmişini temizle")
        clear.setObjectName("clearButton")
        clear.clicked.connect(self.clear_history)
        footer.addWidget(clear)
        outer.addLayout(footer)
        self.rebuild_categories()

    def _nav_button(self, label: str, mode: str) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName("navButton")
        button.setCheckable(True)
        button.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
        setattr(self, f"nav_{mode}", button)
        return button

    def _bind_shortcut(self, sequence: str, callback):
        action = QAction(self)
        action.setShortcut(QKeySequence(sequence))
        action.triggered.connect(callback)
        self.addAction(action)

    def _style(self):
        self.setStyleSheet("""
            QMainWindow { background: transparent; border: 0; }
            #surface { background: rgba(15, 20, 28, 168); border: 1px solid rgba(205, 222, 236, 72); border-radius: 15px; }
            QWidget { color: #dfe7ee; font-family: 'JetBrains Mono', 'Noto Sans Mono', monospace; font-size: 12px; }
            #brandName { color: #dce7f0; font-size: 13px; font-weight: 800; letter-spacing: 2px; }
            #dimText, #countLabel, #footerText { color: #99a7b4; font-size: 10px; }
            #status { color: #bdcbd7; font-size: 10px; }
            #prompt { color: #d4e1ec; font-size: 18px; font-weight: bold; }
            #searchInput { border: 0; border-bottom: 1px solid rgba(202, 218, 232, 54); background: transparent; min-height: 38px; font-size: 14px; selection-background-color: rgba(127, 161, 187, 100); }
            #searchInput:focus { border-bottom: 1px solid #c1d4e3; }
            #clipEditor { background: rgba(15, 20, 28, 90); color: #dfe7ee; border: 1px solid rgba(205, 222, 236, 72); selection-background-color: rgba(127, 161, 187, 100); }
            #navButton, #categoryButton { border: 0; border-radius: 2px; padding: 5px 8px; color: #a1adb8; background: transparent; font-size: 10px; }
            #navButton:hover, #categoryButton:hover { color: #eef4f8; background: rgba(220, 233, 244, 22); }
            #navButton:checked, #categoryButton:checked { color: #e2edf5; background: rgba(188, 210, 228, 30); }
            #addCategory { border: 0; color: #c5d3df; background: transparent; font-size: 15px; }
            #addCategory:hover { color: #f0f5f9; }
            #heading { color: #dce7f0; font-size: 11px; font-weight: bold; }
            #sectionLabel { color: #9ba9b5; font-size: 9px; letter-spacing: 1px; }
            #terminalRule { background: rgba(205, 222, 236, 52); }
            #resultList { border: 0; background: transparent; outline: 0; }
            #resultList::item { border: 0; border-radius: 2px; background: transparent; padding: 0; }
            #resultList::item:hover { background: rgba(220, 233, 244, 20); }
            #resultList::item:selected { background: rgba(188, 210, 228, 30); }
            #previewPanel { background: rgba(220, 232, 242, 12); border: 1px solid rgba(205, 222, 236, 48); border-radius: 5px; }
            #previewImage { background: rgba(8, 12, 18, 70); border-radius: 3px; }
            #previewText { background: transparent; color: #dfe7ee; border: 0; font-size: 10px; }
            #resultTitle { color: #e0e8ef; font-size: 12px; }
            #resultSubtitle { color: #a0adb9; font-size: 10px; }
            #kindBadge { color: #c2d1de; font-size: 9px; }
            #emptyTitle { color: #d1dce5; font-size: 13px; }
            #emptyBody { color: #98a6b2; font-size: 10px; }
            #clearButton { border: 0; color: #98a6b2; background: transparent; font-size: 10px; }
            #clearButton:hover { color: #e5d6d6; }
            QMenu { background: rgba(22, 28, 37, 248); border: 1px solid #53616d; padding: 4px; }
            QMenu::item { padding: 6px 14px; }
            QMenu::item:selected { color: #edf4f8; background: #35434f; }
            QScrollBar:vertical { width: 6px; background: transparent; margin: 0; }
            QScrollBar::handle:vertical { background: #65737f; min-height: 25px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(QIcon.fromTheme("edit-paste"), self)
        menu = QMenu()
        open_action = menu.addAction("Komuta'yı aç")
        open_action.triggered.connect(self.show_launcher)
        menu.addSeparator()
        quit_action = menu.addAction("Çıkış")
        quit_action.triggered.connect(QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_launcher() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.setToolTip("Komuta · pano ve hızlı arama")
        self.tray.show()

    def _register_dbus_service(self):
        self.dbus_connection = QDBusConnection.sessionBus()
        self.dbus_toggle = ToggleService(self)
        object_registered = self.dbus_connection.registerObject("/Komuta", self.dbus_toggle,
                                                                 QDBusConnection.RegisterOption.ExportAllSlots)
        if object_registered:
            self.dbus_connection.registerService("org.komuta.Launcher")

    def rebuild_categories(self):
        for button in getattr(self, "category_buttons", []):
            self.category_layout.removeWidget(button)
            button.deleteLater()
        self.category_buttons = []
        for row in self.db.execute("SELECT name, regex FROM categories ORDER BY position, name"):
            name = row["name"]
            regex = row["regex"]
            button = QPushButton(name)
            button.setObjectName("categoryButton")
            button.setCheckable(True)
            button.setChecked(self.category == name)
            tooltip = "Seçmek için tıkla · düzenlemek için sağ tık"
            if regex:
                tooltip += f"\nRegex: {regex}"
            button.setToolTip(tooltip)
            button.clicked.connect(lambda _=False, n=name: self.set_category(n))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda position, b=button, n=name: self.category_context_menu(n, b.mapToGlobal(position))
            )
            self.category_layout.insertWidget(6 + len(self.category_buttons), button)
            self.category_buttons.append(button)

    def category_context_menu(self, name: str, position):
        menu = QMenu(self)
        regex = menu.addAction("Regex kuralı…")
        rename = menu.addAction("Yeniden adlandır")
        delete = menu.addAction("Kategoriyi sil…")
        action = menu.exec(position)
        if action == regex:
            self.edit_category_regex(name)
        elif action == rename:
            self.rename_category(name)
        elif action == delete:
            self.delete_category(name)

    def edit_category_regex(self, name: str):
        from PyQt6.QtWidgets import QInputDialog
        current = self.db.execute("SELECT regex FROM categories WHERE name=?", (name,)).fetchone()
        if current is None:
            return
        pattern, accepted = QInputDialog.getText(
            self, f"{name} · Regex kuralı",
            r"Regex (ör. ^INV-\d+; boş bırakmak kuralı kaldırır):", text=current["regex"] or "",
        )
        pattern = pattern.strip()
        if not accepted:
            return
        if pattern:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as error:
                QMessageBox.information(self, "Geçersiz regex", str(error))
                return
        with self.db:
            self.db.execute("UPDATE categories SET regex=? WHERE name=?", (pattern or None, name))
        self._recategorize_automatic_clips()
        self.rebuild_categories()
        self.refresh()

    def rename_category(self, old_name: str):
        from PyQt6.QtWidgets import QInputDialog
        new_name, accepted = QInputDialog.getText(self, "Kategoriyi yeniden adlandır", "Yeni ad:", text=old_name)
        new_name = new_name.strip()
        if not accepted or not new_name or new_name == old_name:
            return
        try:
            with self.db:
                self.db.execute("UPDATE categories SET name=? WHERE name=?", (new_name, old_name))
                self.db.execute("UPDATE clips SET category=?, category_auto=0 WHERE category=?", (new_name, old_name))
        except sqlite3.IntegrityError:
            QMessageBox.information(self, "Kategori mevcut", "Bu isimde bir kategori zaten var.")
            return
        if self.category == old_name:
            self.category = new_name
            self.heading.setText(f"# pano / {new_name}")
        self.rebuild_categories()
        self.refresh()

    def delete_category(self, name: str):
        categories = [row["name"] for row in self.db.execute(
            "SELECT name FROM categories ORDER BY position, name")]
        if len(categories) < 2:
            QMessageBox.information(self, "Kategori silinemiyor", "En az bir kategori kalmalı.")
            return
        fallback = "Genel" if name != "Genel" and "Genel" in categories else next(item for item in categories if item != name)
        count = self.db.execute("SELECT COUNT(*) FROM clips WHERE category=?", (name,)).fetchone()[0]
        answer = QMessageBox.question(
            self, "Kategoriyi sil",
            f"‘{name}’ kategorisindeki {count} kayıt ‘{fallback}’ kategorisine taşınsın mı?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        with self.db:
            self.db.execute("UPDATE clips SET category=?, category_auto=0 WHERE category=?", (fallback, name))
            self.db.execute("DELETE FROM categories WHERE name=?", (name,))
        if self.category == name:
            self.category = ""
            self.heading.setText("# pano geçmişi")
        self.rebuild_categories()
        self.refresh()

    def set_mode(self, mode: str):
        if self.editing_clip:
            self.cancel_edit_clip()
        self.mode, self.category = mode, ""
        self.set_search_filter("all")
        for name in ("all", "apps", "files"):
            getattr(self, f"nav_{name}").setChecked(name == mode)
        self.rebuild_categories()
        titles = {"all": "pano geçmişi", "apps": "uygulama araması", "files": "dosya araması"}
        self.heading.setText(f"# {titles[mode]}")
        self.prompt.setText("❯")
        self.search.setPlaceholderText("panoda, uygulamalarda veya dosyalarda ara")
        self.refresh()
        self.search.setFocus()

    def set_category(self, name: str):
        if self.editing_clip:
            self.cancel_edit_clip()
        self.mode, self.category = "clipboard", name
        self.set_search_filter("all")
        for key in ("all", "apps", "files"):
            getattr(self, f"nav_{key}").setChecked(key == "all")
        self.rebuild_categories()
        self.heading.setText(f"# pano / {name}")
        self.refresh()

    def on_catalog(self, results: list[Result]):
        self.catalog = results
        if self.search.text().strip():
            self.refresh()

    def on_catalog_finished(self):
        if self.file_search_worker is None:
            self.status.setText("● Hazır")

    def start_file_search(self):
        query = self.file_search_query
        if not query or self.mode not in ("all", "files"):
            return
        worker = FileSearchWorker(query)
        worker.matchesFound.connect(self.on_file_search_matches)
        worker.finished.connect(self.on_file_search_finished)
        self.file_search_worker = worker
        self.file_search_workers.append(worker)
        self.status.setText("● Dosyalar aranıyor")
        worker.start(QThread.Priority.LowPriority)

    def on_file_search_matches(self, worker: FileSearchWorker, query: str, matches: list[Result]):
        if worker is not self.file_search_worker or query != self.file_search_query or self.mode not in ("all", "files"):
            return
        previous_count = len(self.results)
        scrollbar = self.list.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 5
        self.file_search_matches.extend(matches)
        visible_matches = [result for result in matches if self._matches_search_filter(result)]
        if not visible_matches:
            return
        self.search_results.extend(visible_matches)
        self.results.extend(visible_matches)
        self.result_count.setText(f"{len(self.results)} sonuç")
        if previous_count == 0:
            self.list.clear()
            self.loaded_result_count = 0
            self._append_result_batch()
            self.list.setCurrentRow(0)
        elif self.loaded_result_count < 150 or was_at_bottom:
            self._append_result_batch()

    def on_file_search_finished(self):
        worker = self.sender()
        if not isinstance(worker, FileSearchWorker):
            return
        if worker in self.file_search_workers:
            self.file_search_workers.remove(worker)
        if self.file_search_worker is worker:
            self.file_search_worker = None
            if worker.query == self.file_search_query:
                self.status.setText("● Hazır")
        worker.deleteLater()

    def set_search_filter(self, key: str):
        self.search_filter = key
        for filter_key, button in self.filter_buttons.items():
            button.setChecked(filter_key == key)
        self.refresh()

    @staticmethod
    def _is_image_path(path: str) -> bool:
        return Path(path).suffix.casefold() in {
            ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".svg",
        }

    def _matches_search_filter(self, result: Result) -> bool:
        if self.search_filter == "all":
            return True
        if self.search_filter == "apps":
            return result.kind == "app"
        if self.search_filter == "directories":
            return result.kind == "directory"
        if self.search_filter == "images":
            if result.kind == "file":
                return self._is_image_path(result.value)
            if result.kind == "clip":
                if result.clip_kind == "image":
                    return True
                if result.clip_kind == "files":
                    try:
                        paths = json.loads(result.value)
                    except (TypeError, json.JSONDecodeError):
                        return False
                    return bool(paths) and all(self._is_image_path(path) for path in paths)
            return False
        if self.search_filter == "files":
            if result.kind == "file":
                return not self._is_image_path(result.value)
            if result.kind == "clip" and result.clip_kind == "files":
                try:
                    paths = json.loads(result.value)
                except (TypeError, json.JSONDecodeError):
                    return True
                return not (paths and all(self._is_image_path(path) for path in paths))
        return False

    def cancel_file_search(self):
        if self.file_search_timer.isActive():
            self.file_search_timer.stop()
        if self.file_search_worker and self.file_search_worker.isRunning():
            self.file_search_worker.requestInterruption()
        self.file_search_worker = None
        if self.status.text().startswith("● Dosyalar aranıyor"):
            self.status.setText("● Hazır")

    def capture_clipboard(self):
        mime = self.clipboard.mimeData()
        if mime is None:
            return

        kind, content, payload, mime_type, preview, thumbnail, dimensions = "text", "", None, "", "", None, ""
        urls = mime.urls() if mime.hasUrls() else []
        local_urls = [url for url in urls if url.isLocalFile()]
        if local_urls and len(local_urls) == len(urls):
            paths = [url.toLocalFile() for url in local_urls]
            if len(paths) > MAX_FILES:
                self._notify_clip_skipped("Pano kaydında en fazla 100 dosya tutulabilir.")
                return
            content = json.dumps(paths, ensure_ascii=False)
            if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
                self._notify_clip_skipped("Dosya yolları 200 KB sınırını aşıyor; kayıt eklenmedi.")
                return
            kind = "files"
            preview = " · ".join(Path(path).name for path in paths[:3])
            if len(paths) > 3:
                preview += f" · +{len(paths) - 3}"
            preview = f"{len(paths)} dosya · {preview}"
        elif mime.hasImage():
            image = QImage(mime.imageData())
            if image.isNull() or image.sizeInBytes() > MAX_IMAGE_BYTES * 4:
                self._notify_clip_skipped("Görsel çok büyük; pano geçmişine eklenmedi.")
                return
            encoded = QByteArray()
            buffer = QBuffer(encoded)
            if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
                return
            payload = bytes(encoded)
            buffer.close()
            if len(payload) > MAX_IMAGE_BYTES:
                self._notify_clip_skipped("Görsel 8 MB sınırını aşıyor; pano geçmişine eklenmedi.")
                return
            digest = hashlib.sha256(payload).hexdigest()
            content = f"image:{digest}"
            kind, mime_type = "image", "image/png"
            preview = f"{image.width()} × {image.height()} · PNG · {len(payload) / 1024:.0f} KB"
            dimensions = f"{image.width()} × {image.height()}"
            thumbnail_image = image.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            thumbnail_data = QByteArray()
            thumbnail_buffer = QBuffer(thumbnail_data)
            if thumbnail_buffer.open(QIODevice.OpenModeFlag.WriteOnly):
                thumbnail_image.save(thumbnail_buffer, "PNG")
                thumbnail_buffer.close()
                thumbnail = bytes(thumbnail_data)
        elif mime.hasText():
            content = mime.text().strip()
            if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
                self._notify_clip_skipped("Metin 200 KB sınırını aşıyor; pano geçmişine eklenmedi.")
                return
        dedupe_key = (kind, content)
        if not content or dedupe_key == self.last_clip:
            return
        self.last_clip = dedupe_key
        category = self._category_for_clip(kind, content)
        try:
            self.db.execute("INSERT OR IGNORE INTO clips(content, category, created, kind, payload, mime, thumbnail, dimensions) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                            (content, category, time.time(), kind, payload, mime_type, thumbnail, dimensions or None))
            self.db.execute("UPDATE clips SET created=?, category=CASE WHEN category_auto=1 THEN ? ELSE category END, payload=COALESCE(?, payload), mime=COALESCE(?, mime), thumbnail=COALESCE(?, thumbnail), dimensions=COALESCE(?, dimensions) WHERE content=?",
                            (time.time(), category, payload, mime_type or None, thumbnail, dimensions or None, content))
            self._prune_history()
            stored = self.db.execute("SELECT 1 FROM clips WHERE content=?", (content,)).fetchone()
            self.db.commit()
            if not stored:
                self._notify_clip_skipped("Görsel geçmişi 100 MB sınırında; sabitlenmiş öğeleri silerek yer aç.")
                return
            self.refresh()
        except sqlite3.Error:
            pass

    @staticmethod
    def _suggest_category(kind: str, content: str) -> str:
        if kind == "image":
            return "Görseller"
        if kind == "files":
            try:
                paths = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                paths = []
            image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
            if paths and all(Path(path).suffix.lower() in image_extensions for path in paths):
                return "Görseller"
            return "Genel"
        if URL_ONLY.fullmatch(content.strip()):
            return "Bağlantılar"
        return "Genel"

    def _category_for_clip(self, kind: str, content: str) -> str:
        if kind != "image":
            for row in self.db.execute(
                "SELECT name, regex FROM categories WHERE regex IS NOT NULL AND regex != '' ORDER BY position, name"
            ):
                try:
                    if re.search(row["regex"], content, re.IGNORECASE):
                        return row["name"]
                except re.error:
                    continue
        return self._suggest_category(kind, content)

    def _recategorize_automatic_clips(self):
        rows = self.db.execute(
            "SELECT id, content, kind, category FROM clips WHERE category_auto=1"
        ).fetchall()
        for row in rows:
            category = self._category_for_clip(row["kind"], row["content"])
            if category != row["category"]:
                self.db.execute("UPDATE clips SET category=? WHERE id=?", (category, row["id"]))
        self.db.commit()

    def _prune_history(self):
        self.db.execute("DELETE FROM clips WHERE pinned=0 AND id NOT IN (SELECT id FROM clips WHERE pinned=0 ORDER BY created DESC LIMIT ?)", (MAX_HISTORY,))
        while (self.db.execute("SELECT COALESCE(SUM(length(payload)), 0) FROM clips").fetchone()[0] or 0) > MAX_IMAGE_STORAGE:
            oldest = self.db.execute("SELECT id FROM clips WHERE pinned=0 AND payload IS NOT NULL ORDER BY created ASC LIMIT 1").fetchone()
            if oldest is None:
                break
            self.db.execute("DELETE FROM clips WHERE id=?", (oldest["id"],))

    def _notify_clip_skipped(self, message: str):
        self.status.setText("●  Boyut sınırı")
        self.tray.showMessage("Komuta · Pano geçmişi", message, QSystemTrayIcon.MessageIcon.Information, 3500)
        QTimer.singleShot(4500, lambda: self.status.setText("●  Hazır"))

    def refresh(self, *_):
        query = self.search.text().strip().casefold()
        should_search_files = bool(query) and self.mode in ("all", "files")
        if should_search_files and query != self.file_search_query:
            self.cancel_file_search()
            self.file_search_query = query
            self.file_search_matches = []
            self.file_search_timer.start()
        elif not should_search_files and self.file_search_query is not None:
            self.cancel_file_search()
            self.file_search_query = None
            self.file_search_matches = []
        clips = []
        if self.mode in ("all", "clipboard"):
            sql = "SELECT id, content, category, created, pinned, kind, mime, thumbnail, dimensions FROM clips"
            args: tuple = ()
            if self.category:
                sql += " WHERE category=?"
                args = (self.category,)
            sql += " ORDER BY pinned DESC, created DESC LIMIT ?"
            args += (MAX_HISTORY + MAX_PINNED,)
            for row in self.db.execute(sql, args):
                clip_kind = row["kind"] or "text"
                searchable = row["content"] + " " + row["category"] + " " + clip_kind + " " + (row["mime"] or "")
                if clip_kind == "image":
                    searchable += " panodaki görsel resim"
                if query and query not in searchable.casefold():
                    continue
                if clip_kind == "image":
                    title = "Panodaki görsel"
                    subtitle = row["dimensions"] or "Görsel"
                elif clip_kind == "files":
                    try:
                        paths = json.loads(row["content"])
                    except (TypeError, json.JSONDecodeError):
                        paths = []
                    title = f"{len(paths)} dosya · {Path(paths[0]).name}" if paths else "Dosya listesi"
                    subtitle = " · ".join(paths[:3])
                else:
                    text = row["content"].replace("\n", " ").strip()
                    title = text[:150] or "Boş metin"
                    subtitle = text[150:300] if len(text) > 150 else row["category"]
                clips.append(Result("clip", title, subtitle, row["content"], category=row["category"],
                                    pinned=bool(row["pinned"]), clip_id=row["id"], clip_kind=clip_kind,
                                    mime=row["mime"] or "", thumbnail=bytes(row["thumbnail"]) if row["thumbnail"] else None))
        if query and self.mode in ("all", "apps"):
            catalog = [r for r in self.catalog if r.kind == "app" and query in (r.title + " " + r.subtitle).casefold()]
        else:
            catalog = []
        files = self.file_search_matches if should_search_files else []
        self.search_results = clips + catalog + files
        filter_visible = bool(query) and self.mode in ("all", "files")
        self.filter_row.setVisible(filter_visible)
        self.filter_buttons["apps"].setVisible(self.mode == "all")
        self.results = [result for result in self.search_results if self._matches_search_filter(result)]
        self.list.clear()
        self.result_count.setText(f"{len(self.results)} sonuç")
        self.result_label.setText("ARAMA SONUÇLARI" if query else ("SON KULLANILANLAR" if self.mode in ("all", "clipboard") else "EŞLEŞENLER"))
        self.loaded_result_count = 0
        self.loading_result_batch = False
        if self.results:
            self._append_result_batch()
            self.list.setCurrentRow(0)
        else:
            self.list.setSpacing(0)
            empty = QListWidgetItem()
            empty.setSizeHint(QSize(0, 165))
            self.list.addItem(empty)
            self.list.setItemWidget(empty, self._empty_widget(query))

    def _append_result_batch(self, batch_size: int = 150):
        if self.loading_result_batch:
            return
        self.loading_result_batch = True
        end = min(self.loaded_result_count + batch_size, len(self.results))
        for result in self.results[self.loaded_result_count:end]:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 48))
            self.list.addItem(item)
            self.list.setItemWidget(item, self._result_widget(result))
        self.loaded_result_count = end
        self.list.setSpacing(5)
        self.loading_result_batch = False

    def load_more_results_if_needed(self, value: int):
        scrollbar = self.list.verticalScrollBar()
        if value >= scrollbar.maximum() - 5 and self.loaded_result_count < len(self.results):
            self._append_result_batch()

    def show_file_preview(self, item: QListWidgetItem | None, *_):
        row = self.list.row(item) if item else -1
        if row < 0 or row >= len(self.results) or self.results[row].kind not in ("file", "directory"):
            self.preview_panel.hide()
            return
        result = self.results[row]
        path = Path(result.value)
        self.preview_title.setText(result.title)
        self.preview_text.clear()
        self.preview_text.hide()
        self.preview_image.clear()
        self.preview_image.hide()
        self.preview_info.clear()
        self.preview_info.show()
        try:
            if result.kind == "directory":
                self.preview_meta.setText(f"Klasör · {path}")
                children = []
                with os.scandir(path) as entries:
                    for child in entries:
                        if len(children) >= 31:
                            break
                        children.append(child.name + ("/" if child.is_dir(follow_symlinks=False) else ""))
                self.preview_text.setPlainText("\n".join(sorted(children[:30], key=str.casefold)))
                self.preview_text.show()
                self.preview_info.setText("İlk 30 öğe" if len(children) > 30 else f"{len(children)} öğe")
                self.preview_panel.show()
                return
            size_bytes = path.stat().st_size
            size_text = f"{size_bytes / 1024:.1f} KB" if size_bytes < 1024 * 1024 else f"{size_bytes / (1024 * 1024):.1f} MB"
            self.preview_meta.setText(f"{path.parent}\n{size_text}")
            suffix = path.suffix.casefold()
            if suffix == ".svg":
                renderer = QSvgRenderer(str(path))
                if renderer.isValid():
                    dimensions = renderer.defaultSize()
                    if not dimensions.isValid() or dimensions.isEmpty():
                        dimensions = self.preview_image.size()
                    dimensions = dimensions.scaled(
                        self.preview_image.size(), Qt.AspectRatioMode.KeepAspectRatio
                    )
                    image = QImage(dimensions, QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(Qt.GlobalColor.transparent)
                    from PyQt6.QtGui import QPainter
                    painter = QPainter(image)
                    renderer.render(painter)
                    painter.end()
                    self.preview_image.setPixmap(QPixmap.fromImage(image))
                    self.preview_image.show()
                    self.preview_info.setText(f"SVG · {renderer.defaultSize().width()} × {renderer.defaultSize().height()} px")
                else:
                    self.preview_info.setText("SVG önizlemesi açılamadı.")
            elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
                reader = QImageReader(str(path))
                reader.setAutoTransform(True)
                dimensions = reader.size()
                if dimensions.isValid():
                    reader.setScaledSize(dimensions.scaled(
                        self.preview_image.size(), Qt.AspectRatioMode.KeepAspectRatio
                    ))
                image = reader.read()
                if not image.isNull():
                    self.preview_image.setPixmap(QPixmap.fromImage(image))
                    self.preview_image.show()
                    self.preview_info.setText(f"{dimensions.width()} × {dimensions.height()} px")
                else:
                    self.preview_info.setText("Görsel önizlemesi açılamadı.")
            elif suffix in {".txt", ".md", ".py", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv", ".log", ".html", ".css", ".js", ".ts", ".sh", ".xml", ".sql", ".rs", ".go", ".c", ".h", ".cpp", ".java", ".kt", ".rb", ".php"}:
                with path.open("rb") as source:
                    data = source.read(64 * 1024 + 1)
                truncated = len(data) > 64 * 1024
                content = data[:64 * 1024].decode("utf-8", errors="replace")
                self.preview_text.setPlainText(content)
                self.preview_text.show()
                self.preview_info.setText("Önizleme ilk 64 KB ile sınırlı." if truncated else "Metin önizlemesi")
            else:
                self.preview_info.setText("Bu dosya türü için hızlı önizleme yok.")
        except OSError as error:
            self.preview_meta.setText(str(path))
            self.preview_info.setText(f"Dosya okunamadı: {error}")
        self.preview_panel.show()

    def _result_widget(self, result: Result) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(9)
        icon = QLabel()
        icon.setFixedSize(32, 32)
        if result.icon and not result.icon.isNull():
            icon.setPixmap(result.icon.pixmap(26, 26))
        elif result.kind == "clip" and result.clip_kind == "image" and result.thumbnail:
            pixmap = QPixmap()
            pixmap.loadFromData(result.thumbnail, "PNG")
            icon.setPixmap(pixmap.scaled(34, 34, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            glyph = {"clip": {"text": "TXT", "image": "IMG", "files": "DIR"}.get(result.clip_kind, "TXT"), "app": "APP", "file": "FILE", "directory": "DIR"}.get(result.kind, ">")
            icon.setText(glyph)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon.setStyleSheet("color:#c2d1de;font-family:'JetBrains Mono','Noto Sans Mono',monospace;font-size:9px;")
        layout.addWidget(icon)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        name = QLabel(("★  " if result.pinned else "") + result.title)
        name.setObjectName("resultTitle")
        name.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        sub = QLabel(result.subtitle or result.value[:70])
        sub.setObjectName("resultSubtitle")
        sub.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        texts.addWidget(name)
        texts.addWidget(sub)
        layout.addLayout(texts, 1)
        badge = QLabel({"clip": result.category, "app": "UYGULAMA", "file": "DOSYA", "directory": "KLASÖR"}[result.kind])
        badge.setObjectName("kindBadge")
        layout.addWidget(badge)
        return row

    def _empty_widget(self, query: str) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addStretch()
        if self.mode in ("apps", "files") and not query:
            empty_title = "Uygulama aramak için yaz" if self.mode == "apps" else "Dosya aramak için yaz"
        elif self.mode in ("all", "clipboard") and not query:
            empty_title = "Henüz pano kaydı yok"
        else:
            empty_title = "Sonuç bulunamadı" if query else "Aramaya başla"
        title = QLabel(empty_title)
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self.mode in ("apps", "files") and not query:
            body_text = "Eşleşmeler yalnızca arama yapınca listelenir."
        elif self.mode in ("all", "clipboard") and not query:
            body_text = "Kopyaladığın içerikler burada birikir; uygulama ve dosyaları arayabilirsin."
        else:
            body_text = "Pano, uygulama adı veya dosya adı yaz."
        body = QLabel(body_text)
        body.setObjectName("emptyBody")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addStretch()
        return container

    def activate_item(self, item: QListWidgetItem):
        row = self.list.row(item)
        if row < 0 or row >= len(self.results):
            return
        result = self.results[row]
        if result.kind == "clip":
            self.copy_clip(result)
            self.hide()
        elif result.kind in ("file", "directory"):
            subprocess.Popen(["xdg-open", result.value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.hide()
        elif result.kind == "app":
            command = result.value.replace("%u", "").replace("%U", "").replace("%f", "").replace("%F", "").replace("%i", "").replace("%c", "").replace("%k", "")
            try:
                subprocess.Popen(shlex.split(command), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except (OSError, ValueError):
                pass
            self.hide()

    def context_menu(self, position):
        item = self.list.itemAt(position)
        index = self.list.row(item) if item else -1
        if index < 0 or index >= len(self.results) or self.results[index].kind != "clip":
            return
        result = self.results[index]
        menu = QMenu(self)
        pin = menu.addAction("★  Sabitle" if not result.pinned else "Sabitlemeyi kaldır")
        pin.triggered.connect(lambda: self.set_pinned(result))
        category_menu = menu.addMenu("Kategoriye taşı")
        for row in self.db.execute("SELECT name FROM categories ORDER BY position, name"):
            action = category_menu.addAction(row["name"])
            action.triggered.connect(lambda _=False, name=row["name"]: self.move_category(result, name))
        if result.clip_kind == "text":
            menu.addSeparator()
            edit = menu.addAction("Metni düzenle…")
            edit.triggered.connect(lambda: self.edit_clip_text(result))
        menu.addSeparator()
        copy = menu.addAction("Kopyala")
        copy.triggered.connect(lambda: self.copy_clip(result))
        delete = menu.addAction("Sil")
        delete.triggered.connect(lambda: self.delete_clip(result))
        menu.exec(self.list.mapToGlobal(position))

    def edit_clip_text(self, result: Result):
        self.editing_clip = result
        self.heading.setText("# pano kaydını düzenle")
        self.result_label.hide()
        self.result_count.hide()
        self.list.hide()
        self.edit_input.setPlainText(result.value)
        self.edit_status.clear()
        self.edit_panel.show()
        self.edit_input.setFocus()
        self.edit_input.moveCursor(QTextCursor.MoveOperation.End)

    def cancel_edit_clip(self):
        self.editing_clip = None
        self.edit_panel.hide()
        self.list.show()
        self.result_label.show()
        self.result_count.show()
        self._restore_heading()
        self.refresh()

    def save_edit_clip(self):
        result = self.editing_clip
        if not result:
            return
        text = self.edit_input.toPlainText()
        if not text.strip():
            self.edit_status.setText("Boş metin kaydedilemez.")
            return
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            self.edit_status.setText("Metin 200 KB sınırını aşıyor.")
            return
        try:
            with self.db:
                self.db.execute(
                    "UPDATE clips SET content=?, created=?, category_auto=0 WHERE id=?",
                    (text, time.time(), result.clip_id),
                )
        except sqlite3.IntegrityError:
            self.edit_status.setText("Bu metin pano geçmişinde zaten var.")
            return
        self.editing_clip = None
        self.edit_panel.hide()
        self.list.show()
        self.result_label.show()
        self.result_count.show()
        self._restore_heading()
        self.refresh()

    def _restore_heading(self):
        if self.mode == "clipboard" and self.category:
            self.heading.setText(f"# pano / {self.category}")
        else:
            titles = {"all": "pano geçmişi", "apps": "uygulama araması", "files": "dosya araması"}
            self.heading.setText(f"# {titles.get(self.mode, 'pano geçmişi')}")

    def set_pinned(self, result: Result):
        if not result.pinned:
            count = self.db.execute("SELECT COUNT(*) FROM clips WHERE pinned=1").fetchone()[0]
            if count >= MAX_PINNED:
                self._notify_clip_skipped("En fazla 50 pano kaydı sabitlenebilir.")
                return
        self.db.execute("UPDATE clips SET pinned=? WHERE id=?", (0 if result.pinned else 1, result.clip_id))
        self.db.commit()
        self.refresh()

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def cycle_category(self, direction: int):
        names = [""] + [row["name"] for row in self.db.execute("SELECT name FROM categories ORDER BY position, name")]
        current = self.category if self.mode == "clipboard" else ""
        index = names.index(current) if current in names else 0
        selected = names[(index + direction) % len(names)]
        if selected:
            self.set_category(selected)
        else:
            self.set_mode("all")

    def selected_clip(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.results) and self.results[row].kind == "clip":
            return self.results[row]
        return None

    def toggle_selected_pin(self):
        result = self.selected_clip()
        if result:
            self.set_pinned(result)

    def delete_selected_clip(self):
        result = self.selected_clip()
        if result:
            self.delete_clip(result)

    def move_selected_category(self):
        result = self.selected_clip()
        if not result:
            return
        from PyQt6.QtWidgets import QInputDialog
        categories = [row["name"] for row in self.db.execute("SELECT name FROM categories ORDER BY position, name")]
        if not categories:
            return
        current = categories.index(result.category) if result.category in categories else 0
        category, accepted = QInputDialog.getItem(self, "Kategoriyi değiştir", "Kategori:", categories, current, False)
        if accepted:
            self.move_category(result, category)

    def move_category(self, result: Result, category: str):
        self.db.execute("UPDATE clips SET category=?, category_auto=0 WHERE id=?", (category, result.clip_id))
        self.db.commit()
        self.refresh()

    def copy_clip(self, result: Result):
        if result.clip_kind == "image":
            row = self.db.execute("SELECT payload FROM clips WHERE id=?", (result.clip_id,)).fetchone()
            if row and row["payload"]:
                self.clipboard.setImage(QImage.fromData(bytes(row["payload"])))
        elif result.clip_kind == "files":
            mime = QMimeData()
            try:
                paths = json.loads(result.value)
            except (TypeError, json.JSONDecodeError):
                paths = []
            mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
            self.clipboard.setMimeData(mime)
        else:
            self.clipboard.setText(result.value)
        self.db.execute("UPDATE clips SET created=? WHERE id=?", (time.time(), result.clip_id))
        self.db.commit()
        self.hide()

    def delete_clip(self, result: Result):
        self.db.execute("DELETE FROM clips WHERE id=?", (result.clip_id,))
        self.db.commit()
        self.refresh()

    def add_category(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Yeni kategori", "Kategori adı:")
        name = name.strip()
        if ok and name:
            try:
                position = self.db.execute("SELECT COALESCE(MAX(position), 0)+1 FROM categories").fetchone()[0]
                self.db.execute("INSERT INTO categories(name, position) VALUES(?, ?)", (name, position))
                self.db.commit()
                self.rebuild_categories()
            except sqlite3.IntegrityError:
                QMessageBox.information(self, "Kategori mevcut", "Bu isimde bir kategori zaten var.")

    def clear_history(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Pano geçmişini temizle")
        dialog.setText("Hangi kayıtları silmek istiyorsun?")
        clear_all = dialog.addButton("Tümünü sil", QMessageBox.ButtonRole.DestructiveRole)
        clear_unpinned = dialog.addButton("Sabitlenmemişleri sil", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        if dialog.clickedButton() == clear_all:
            self.db.execute("DELETE FROM clips")
        elif dialog.clickedButton() == clear_unpinned:
            self.db.execute("DELETE FROM clips WHERE pinned=0")
        else:
            return
        self.db.commit()
        self.refresh()

    def show_launcher(self):
        if self.isVisible():
            self.hide()
            return
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    def toggle(self):
        self.show_launcher()

    def handle_local_request(self):
        connection = self.local_server.nextPendingConnection()
        if connection:
            connection.waitForReadyRead(150)
            command = bytes(connection.readAll()).decode("utf-8", errors="ignore").strip()
            if command == "toggle":
                self.toggle()
            elif command == "show":
                self.show()
                self.raise_()
                self.activateWindow()
                self.search.setFocus()
            connection.disconnectFromServer()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            if self.editing_clip:
                self.cancel_edit_clip()
            else:
                self.hide()
        elif event.key() == Qt.Key.Key_Down and not self.list.hasFocus():
            self.list.setFocus()
            if self.list.currentRow() < 0:
                self.list.setCurrentRow(0)
        elif event.key() == Qt.Key.Key_Up and self.list.hasFocus() and self.list.currentRow() == 0:
            self.search.setFocus()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(area.center().x() - self.width() // 2, area.top() + max(75, area.height() // 8))
        QTimer.singleShot(0, self.search.setFocus)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("komuta")
    app.setApplicationDisplayName("Komuta")
    app.setQuitOnLastWindowClosed(False)
    client = QLocalSocket()
    client.connectToServer("komuta-launcher")
    if client.waitForConnected(250):
        if "--background" not in sys.argv:
            command = b"toggle" if "--toggle" in sys.argv else b"show"
            client.write(command)
            client.waitForBytesWritten(250)
        client.disconnectFromServer()
        return 0
    window = KomutaWindow()
    if "--background" not in sys.argv:
        window.show()
        window.raise_()
        window.activateWindow()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
