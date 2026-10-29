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
from PyQt6.QtGui import QAction, QKeySequence, QIcon, QPixmap, QImage
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
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
    db.execute("CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY, position INTEGER NOT NULL)")
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

        search_roots = [HOME / name for name in (
            "Desktop", "Masaüstü", "Documents", "Belgeler", "Downloads", "İndirilenler",
            "Pictures", "Resimler", "Videos", "Videolar", "Projects", "Projeler",
        )]
        found = 0
        for root in search_roots:
            if not root.is_dir():
                continue
            for current, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".cache", ".local"}]
                for filename in files:
                    path = Path(current) / filename
                    try:
                        entries.append(Result("file", filename, str(path.parent), str(path)))
                        found += 1
                    except OSError:
                        continue
                    if found >= 8000:
                        break
                if found >= 8000:
                    break
        self.ready.emit(entries)

    @staticmethod
    def _field(raw: str, field: str) -> str:
        match = re.search(rf"^{re.escape(field)}=(.*)$", raw, re.MULTILINE)
        return match.group(1).replace("%U", "").replace("%u", "").replace("%F", "").replace("%f", "").strip() if match else ""


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
        self.resize(880, 590)
        self.db = database()
        for row in self.db.execute("SELECT id, content, kind, category FROM clips WHERE category_auto=1").fetchall():
            suggested = self._suggest_category(row["kind"], row["content"])
            if suggested != row["category"]:
                self.db.execute("UPDATE clips SET category=? WHERE id=?", (suggested, row["id"]))
        self.db.commit()
        self.mode = "all"
        self.category = ""
        self.catalog: list[Result] = []
        self.results: list[Result] = []
        self.last_clip = None
        self._build_ui()
        self._style()
        self._build_tray()
        self.local_server = QLocalServer(self)
        self.local_server.newConnection.connect(self.handle_local_request)
        QLocalServer.removeServer("komuta-launcher")
        self.local_server.listen("komuta-launcher")
        self._register_dbus_service()
        self.clipboard = QApplication.clipboard()
        self.clipboard.dataChanged.connect(self.capture_clipboard)
        self.worker = CatalogWorker()
        self.worker.ready.connect(self.on_catalog)
        self.worker.start()
        self.refresh()
        self.search.setFocus()
        self.capture_clipboard()
        # Regular Qt shortcuts work while the launcher is open, including on Wayland.
        self.toggle_action = QAction(self)
        self.toggle_action.setShortcut(QKeySequence("Alt+Space"))
        self.toggle_action.triggered.connect(self.toggle)
        self.addAction(self.toggle_action)

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 20, 14, 16)
        side.setSpacing(7)
        brand = QHBoxLayout()
        mark = QLabel("K")
        mark.setObjectName("brandMark")
        mark.setFixedSize(36, 36)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("komuta")
        title.setObjectName("brandName")
        brand.addWidget(mark)
        brand.addSpacing(9)
        brand.addWidget(title)
        brand.addStretch()
        side.addLayout(brand)
        side.addSpacing(24)
        side.addWidget(self._nav_button("⌕", "Her şey", "all"))
        side.addWidget(self._nav_button("▤", "Pano geçmişi", "clipboard"))
        side.addWidget(self._nav_button("▣", "Uygulamalar", "apps"))
        side.addWidget(self._nav_button("▱", "Dosyalar", "files"))
        side.addSpacing(21)
        category_header = QHBoxLayout()
        cat_label = QLabel("KATEGORİLER")
        cat_label.setObjectName("sectionLabel")
        category_header.addWidget(cat_label)
        category_header.addStretch()
        add_cat = QPushButton("+")
        add_cat.setObjectName("addCategory")
        add_cat.setToolTip("Kategori ekle")
        add_cat.clicked.connect(self.add_category)
        category_header.addWidget(add_cat)
        side.addLayout(category_header)
        self.category_list = QVBoxLayout()
        self.category_list.setSpacing(4)
        side.addLayout(self.category_list)
        side.addStretch()
        hint = QLabel("Alt  +  Boşluk")
        hint.setObjectName("shortcutHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(hint)
        outer.addWidget(sidebar)

        main = QWidget()
        main.setObjectName("mainPanel")
        content = QVBoxLayout(main)
        content.setContentsMargins(25, 22, 25, 18)
        content.setSpacing(14)
        top = QHBoxLayout()
        heading = QVBoxLayout()
        self.heading = QLabel("Her şey")
        self.heading.setObjectName("heading")
        self.subheading = QLabel("Ara, bul ve kaldığın yerden devam et")
        self.subheading.setObjectName("subheading")
        heading.addWidget(self.heading)
        heading.addWidget(self.subheading)
        top.addLayout(heading)
        top.addStretch()
        self.status = QLabel("●  Hazır")
        self.status.setObjectName("status")
        top.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        content.addLayout(top)

        search_wrap = QFrame()
        search_wrap.setObjectName("searchWrap")
        search_row = QHBoxLayout(search_wrap)
        search_row.setContentsMargins(14, 0, 12, 0)
        search_row.setSpacing(10)
        search_icon = QLabel("⌕")
        search_icon.setObjectName("searchIcon")
        search_row.addWidget(search_icon)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Pano, uygulama veya dosya ara…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.search.returnPressed.connect(lambda: self.activate_item(self.list.currentItem()))
        search_row.addWidget(self.search)
        content.addWidget(search_wrap)

        list_header = QHBoxLayout()
        self.result_label = QLabel("SON KULLANILANLAR")
        self.result_label.setObjectName("sectionLabel")
        self.result_count = QLabel("")
        self.result_count.setObjectName("countLabel")
        list_header.addWidget(self.result_label)
        list_header.addStretch()
        list_header.addWidget(self.result_count)
        content.addLayout(list_header)

        self.list = QListWidget()
        self.list.setObjectName("resultList")
        self.list.setSpacing(5)
        self.list.itemActivated.connect(self.activate_item)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.context_menu)
        content.addWidget(self.list, 1)

        footer = QHBoxLayout()
        footer_text = QLabel("↑ ↓ seç     ↵ aç / kopyala     Sağ tık: kategori")
        footer_text.setObjectName("footerText")
        footer.addWidget(footer_text)
        footer.addStretch()
        clear = QPushButton("Pano geçmişini temizle")
        clear.setObjectName("clearButton")
        clear.clicked.connect(self.clear_history)
        footer.addWidget(clear)
        content.addLayout(footer)
        outer.addWidget(main, 1)
        self.rebuild_categories()

    def _nav_button(self, icon: str, label: str, mode: str) -> QPushButton:
        button = QPushButton(f"{icon}     {label}")
        button.setObjectName("navButton")
        button.setCheckable(True)
        button.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
        setattr(self, f"nav_{mode}", button)
        return button

    def _style(self):
        self.setStyleSheet("""
            QMainWindow { background: #11131a; border: 1px solid #303440; }
            QWidget { color: #e9eaf0; font-family: 'Inter', 'Noto Sans', sans-serif; font-size: 13px; }
            #sidebar { background: #171922; border-right: 1px solid #292c37; }
            #brandMark { background: #8d7cff; color: #11131a; border-radius: 11px; font-size: 21px; font-weight: 800; }
            #brandName { font-size: 20px; font-weight: 750; letter-spacing: .3px; }
            #navButton, #categoryButton { text-align: left; border: 0; border-radius: 8px; padding: 10px 11px; color: #a7a9b7; background: transparent; }
            #navButton:hover, #categoryButton:hover { background: #222530; color: #f3f3f8; }
            #navButton:checked, #categoryButton:checked { background: #28283b; color: #b5aaff; font-weight: 650; }
            #sectionLabel { color: #777b8d; font-size: 10px; font-weight: 750; letter-spacing: 1.25px; }
            #addCategory { border: 0; border-radius: 6px; color: #9a9dad; background: transparent; font-size: 18px; padding: 0 7px; }
            #addCategory:hover { color: white; background: #292c38; }
            #shortcutHint { color: #777b8d; background: #20222d; border-radius: 7px; padding: 9px; font-size: 11px; }
            #mainPanel { background: #11131a; }
            #heading { font-size: 23px; font-weight: 720; }
            #subheading { color: #898d9e; font-size: 12px; margin-top: 1px; }
            #status { color: #82d8a8; font-size: 11px; padding: 6px 0; }
            #searchWrap { background: #1c1f29; border: 1px solid #303440; border-radius: 11px; min-height: 48px; }
            #searchWrap:focus-within { border: 1px solid #8d7cff; }
            #searchIcon { color: #9c8cff; font-size: 25px; }
            QLineEdit { border: 0; background: transparent; font-size: 14px; selection-background-color: #6858cf; }
            #resultList { border: 0; background: transparent; outline: 0; }
            #resultList::item { border: 1px solid #292c37; border-radius: 9px; background: #191b24; padding: 0; }
            #resultList::item:hover { background: #21232e; border-color: #3c3e4d; }
            #resultList::item:selected { background: #29273b; border-color: #7667db; }
            #resultTitle { font-size: 13px; font-weight: 620; color: #eeeff5; }
            #resultSubtitle { color: #85899a; font-size: 11px; }
            #kindBadge { color: #b2a6ff; background: #29263d; border-radius: 5px; padding: 4px 7px; font-size: 10px; }
            #emptyTitle { color: #d9dbe5; font-weight: 650; font-size: 15px; }
            #emptyBody { color: #868a99; font-size: 12px; }
            #countLabel, #footerText { color: #737788; font-size: 11px; }
            #clearButton { border: 0; color: #85899a; background: transparent; font-size: 11px; }
            #clearButton:hover { color: #ef8e9c; }
            QMenu { background: #20222b; border: 1px solid #3b3e4b; padding: 5px; }
            QMenu::item { padding: 7px 18px; border-radius: 5px; }
            QMenu::item:selected { background: #343147; }
            QScrollBar:vertical { width: 7px; background: transparent; margin: 2px; }
            QScrollBar::handle:vertical { background: #393c4a; border-radius: 3px; min-height: 25px; }
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
        while self.category_list.count():
            item = self.category_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for row in self.db.execute("SELECT name FROM categories ORDER BY position, name"):
            name = row["name"]
            button = QPushButton(f"◦     {name}")
            button.setObjectName("categoryButton")
            button.setCheckable(True)
            button.setChecked(self.category == name)
            button.clicked.connect(lambda _=False, n=name: self.set_category(n))
            self.category_list.addWidget(button)

    def set_mode(self, mode: str):
        self.mode, self.category = mode, ""
        for name in ("all", "clipboard", "apps", "files"):
            getattr(self, f"nav_{name}").setChecked(name == mode)
        self.rebuild_categories()
        titles = {"all": "Her şey", "clipboard": "Pano geçmişi", "apps": "Uygulamalar", "files": "Dosyalar"}
        self.heading.setText(titles[mode])
        self.refresh()
        self.search.setFocus()

    def set_category(self, name: str):
        self.mode, self.category = "clipboard", name
        for key in ("all", "clipboard", "apps", "files"):
            getattr(self, f"nav_{key}").setChecked(key == "clipboard")
        self.rebuild_categories()
        self.heading.setText(name)
        self.refresh()

    def on_catalog(self, results: list[Result]):
        self.catalog = results
        self.refresh()

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
        category = self._suggest_category(kind, content)
        try:
            self.db.execute("INSERT OR IGNORE INTO clips(content, category, created, kind, payload, mime, thumbnail, dimensions) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                            (content, category, time.time(), kind, payload, mime_type, thumbnail, dimensions or None))
            self.db.execute("UPDATE clips SET created=?, payload=COALESCE(?, payload), mime=COALESCE(?, mime), thumbnail=COALESCE(?, thumbnail), dimensions=COALESCE(?, dimensions) WHERE content=?",
                            (time.time(), payload, mime_type or None, thumbnail, dimensions or None, content))
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
        catalog = []
        if self.mode in ("all", "apps", "files"):
            kinds = {"apps": {"app"}, "files": {"file"}, "all": {"app", "file"}}[self.mode]
            catalog = [r for r in self.catalog if r.kind in kinds and (not query or query in (r.title + " " + r.subtitle).casefold())]
        self.results = clips + catalog
        self.list.clear()
        self.result_count.setText(f"{len(self.results)} sonuç")
        self.result_label.setText("ARAMA SONUÇLARI" if query else ("SON KULLANILANLAR" if self.mode in ("all", "clipboard") else "EŞLEŞENLER"))
        for result in self.results[:150]:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 62))
            self.list.addItem(item)
            self.list.setItemWidget(item, self._result_widget(result))
        if not self.results:
            self.list.setSpacing(0)
            empty = QListWidgetItem()
            empty.setSizeHint(QSize(0, 165))
            self.list.addItem(empty)
            self.list.setItemWidget(empty, self._empty_widget(query))
        else:
            self.list.setSpacing(5)
            self.list.setCurrentRow(0)

    def _result_widget(self, result: Result) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(13, 7, 13, 7)
        layout.setSpacing(11)
        icon = QLabel()
        icon.setFixedSize(32, 32)
        if result.icon and not result.icon.isNull():
            icon.setPixmap(result.icon.pixmap(26, 26))
        elif result.kind == "clip" and result.clip_kind == "image" and result.thumbnail:
            pixmap = QPixmap()
            pixmap.loadFromData(result.thumbnail, "PNG")
            icon.setPixmap(pixmap.scaled(34, 34, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            glyph = {"clip": "▤" if result.clip_kind == "text" else ("▧" if result.clip_kind == "image" else "▱"), "app": "▣", "file": "▱"}.get(result.kind, "•")
            icon.setText(glyph)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon.setStyleSheet("background:#292b38;border-radius:8px;color:#a99bff;font-size:16px;")
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
        badge = QLabel({"clip": result.category, "app": "UYGULAMA", "file": "DOSYA"}[result.kind])
        badge.setObjectName("kindBadge")
        layout.addWidget(badge)
        return row

    def _empty_widget(self, query: str) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addStretch()
        title = QLabel("Henüz pano kaydı yok" if self.mode == "clipboard" else ("Sonuç bulunamadı" if query else "Aramaya başla"))
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = QLabel("Kopyaladığın metinler burada otomatik birikir." if self.mode == "clipboard" else "Pano içeriği, uygulama adı veya dosya adı yaz.")
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
        elif result.kind == "file":
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
        menu.addSeparator()
        copy = menu.addAction("Kopyala")
        copy.triggered.connect(lambda: self.copy_clip(result))
        delete = menu.addAction("Sil")
        delete.triggered.connect(lambda: self.delete_clip(result))
        menu.exec(self.list.mapToGlobal(position))

    def set_pinned(self, result: Result):
        if not result.pinned:
            count = self.db.execute("SELECT COUNT(*) FROM clips WHERE pinned=1").fetchone()[0]
            if count >= MAX_PINNED:
                self._notify_clip_skipped("En fazla 50 pano kaydı sabitlenebilir.")
                return
        self.db.execute("UPDATE clips SET pinned=? WHERE id=?", (0 if result.pinned else 1, result.clip_id))
        self.db.commit()
        self.refresh()

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
        answer = QMessageBox.question(self, "Pano geçmişini temizle", "Sabitlediklerin dışındaki tüm kayıtlar silinsin mi?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.db.execute("DELETE FROM clips WHERE pinned=0")
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
            self.hide()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.activate_item(self.list.currentItem())
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
