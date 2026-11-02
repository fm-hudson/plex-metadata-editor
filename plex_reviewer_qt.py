#!/usr/bin/python3
import sys
import os
import json
import time
import copy
import threading
import traceback
import requests
import xml.etree.ElementTree as ET

APP_VERSION = "v1.0.0"
PROCESS_TITLE = "plex metadata ui editor"

try:
    import setproctitle
    setproctitle.setproctitle(PROCESS_TITLE)
    if hasattr(setproctitle, "setthreadtitle"):
        setproctitle.setthreadtitle("plex-meta-ui")
except ImportError:
    pass

try:
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QGridLayout, QFormLayout, QLabel, QLineEdit, QPlainTextEdit,
        QPushButton, QCheckBox, QDialog, QMessageBox,
        QProgressBar, QScrollArea, QGroupBox, QSizePolicy, QFrame,
        QFileDialog
    )
    from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
    from PyQt5.QtGui import QPixmap, QColor, QPalette
except ImportError:
    print("PyQt5 is not installed. Please install it first.")
    sys.exit(1)

# Add current directory (and ~/myScripts fallback) to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.expanduser('~/myScripts'))
try:
    import media_tag_io as io
    import plex_metadata_lib as lib
    from plex_mkv_metadata_batch import discover_matching_items, SECTION_ITEM_TYPES
except ImportError as e:
    print(f"Error importing required modules: {e}")
    sys.exit(1)


class Plex:
    def __init__(self, url, token):
        self.url = (url or "").rstrip("/")
        self.token = token or ""

    def get(self, path, params=None):
        params = dict(params or {})
        params["X-Plex-Token"] = self.token
        r = requests.get(self.url + path, params=params, timeout=60)
        r.raise_for_status()
        return r

    def xml(self, path, params=None):
        return ET.fromstring(self.get(path, params).content)

    def item(self, rating_key):
        root = self.xml(f"/library/metadata/{rating_key}")
        video = root.find("Video")
        if video is None:
            raise RuntimeError(f"No Video element for ratingKey {rating_key}")
        return video


def format_size(b):
    if b >= 1073741824:
        return f"{(b / 1073741824):.2f} GB"
    if b >= 1048576:
        return f"{(b / 1048576):.0f} MB"
    return f"{max(1, int(b / 1024))} KB"


# ----------------------------------------
# Worker Threads
# ----------------------------------------

class DiscoveryThread(QThread):
    finished_signal = pyqtSignal(list, int)
    error_signal = pyqtSignal(str)

    def __init__(self, url, token, directory):
        super().__init__()
        self.url = url
        self.token = token
        self.directory = directory

    def run(self):
        try:
            matches = discover_matching_items(self.url, self.token, self.directory)
            matches.sort(key=lambda x: x[1])
            filtered = []
            for rk, fpath in matches:
                sent = io.read_sentinel(fpath)
                if sent not in io.SKIP_VALUES:
                    filtered.append((rk, fpath))
            self.finished_signal.emit(filtered, len(filtered))
        except Exception as e:
            self.error_signal.emit(str(e))


class LoadItemThread(QThread):
    finished_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def __init__(self, url, token, rk, path):
        super().__init__()
        self.url = url
        self.token = token
        self.rk = rk
        self.path = path
        self.poster_bytes = None

    def run(self):
        try:
            plex = Plex(self.url, self.token)
            plex_item = plex.item(self.rk)
            plex_tags = io.parse_plex(plex_item)

            poster_bytes = None
            if plex_item.get("thumb"):
                poster_url = f"{self.url}{plex_item.get('thumb')}?X-Plex-Token={self.token}"
                resp = requests.get(poster_url, timeout=30)
                if resp.status_code == 200:
                    poster_bytes = resp.content
            self.poster_bytes = poster_bytes

            file_tags = io.read_file_fields(self.path, "/tmp/plex_rev")
            size = os.path.getsize(self.path)

            self.finished_signal.emit({
                "ratingKey": self.rk,
                "path": self.path,
                "size": size,
                "plex_tags": plex_tags,
                "file_tags": file_tags,
                "poster_bytes": poster_bytes
            })
        except Exception as e:
            traceback.print_exc()
            self.error_signal.emit(str(e))


class QtProgressHooks:
    def __init__(self, thread):
        self.thread = thread
        self.state = None
        self.lock = threading.Lock()

    def begin(self, name):
        now = time.time()
        with self.lock:
            self.state = {
                "name": name,
                "started": now,
                "step_started": now,
                "current": None,
                "steps": [
                    {"name": s, "state": "pending"}
                    for s in ["Safety copy", "Check original", "Write tags", "Verify", "Restore original", "Clean up"]
                ],
                "pct": None,
                "bytes_done": 0,
                "bytes_total": 0,
                "rate": 0,
                "eta": None
            }
        self._emit()

    def step(self, name):
        now = time.time()
        with self.lock:
            if not self.state:
                return
            p = self.state
            for s in p["steps"]:
                if s["state"] == "active":
                    s["state"] = "done"
            match = [s for s in p["steps"] if s["name"] == name]
            if match:
                match[0]["state"] = "active"
            else:
                pos = next((k for k, s in enumerate(p["steps"]) if s["state"] == "pending"), len(p["steps"]))
                p["steps"].insert(pos, {"name": name, "state": "active"})
            p.update(current=name, step_started=now, pct=None, bytes_done=0, bytes_total=0, rate=0, eta=None)
        self._emit()

    def fail(self):
        with self.lock:
            if not self.state:
                return
            for s in self.state["steps"]:
                if s["state"] == "active":
                    s["state"] = "failed"
        self._emit()

    def skip_pending(self, names):
        with self.lock:
            if not self.state:
                return
            for s in self.state["steps"]:
                if s["state"] == "pending" and s["name"] in names:
                    s["state"] = "skipped"
        self._emit()

    def pct(self, pct):
        with self.lock:
            if self.state:
                self.state["pct"] = float(pct)
        self._emit()

    def bytes(self, done, total):
        now = time.time()
        with self.lock:
            if self.state:
                elapsed = max(0.001, now - self.state.get("step_started", now))
                rate = done / elapsed
                eta = ((total - done) / rate) if (rate > 0 and total > done) else 0
                self.state["bytes_done"] = done
                self.state["bytes_total"] = total
                self.state["pct"] = (done / total * 100.0) if total else 0.0
                self.state["rate"] = rate
                self.state["eta"] = eta
        self._emit()

    def _emit(self):
        with self.lock:
            if self.state:
                snap = copy.deepcopy(self.state)
                snap["elapsed"] = time.time() - snap.get("started", time.time())
                snap["step_elapsed"] = time.time() - snap.get("step_started", time.time())
                self.thread.state_signal.emit(snap)


class SaveItemThread(QThread):
    state_signal = pyqtSignal(dict)
    finished_signal = pyqtSignal(bool, str, dict)

    def __init__(self, settings, rk, path, merged_tags, poster_bytes):
        super().__init__()
        self.settings = settings
        self.rk = rk
        self.path = path
        self.merged_tags = merged_tags
        self.poster_bytes = poster_bytes

    def run(self):
        cover_path = None
        hooks = QtProgressHooks(self)
        hooks.begin(os.path.basename(self.path))
        try:
            if self.poster_bytes:
                cover_path = f"/tmp/plex_rev_cover_{self.rk}.jpg"
                with open(cover_path, "wb") as f_out:
                    f_out.write(self.poster_bytes)

            cleaned_tags = io.clean_fields(self.merged_tags)
            res = io.safe_write(
                path=self.path,
                f=cleaned_tags,
                cover=cover_path,
                sentinel_value=io.SENTINEL_VALUE_BATCH,
                tmp_prefix="/tmp/plex_rev",
                backup_dir=self.settings.get("backup_dir", ""),
                progress=hooks
            )

            if cover_path and os.path.exists(cover_path):
                try:
                    os.remove(cover_path)
                except Exception:
                    pass

            if res.get("ok"):
                # Mark all non-skipped steps as done on finish
                with hooks.lock:
                    for s in hooks.state["steps"]:
                        if s["state"] == "active":
                            s["state"] = "done"
                        elif s["state"] == "pending" and s["name"] == "Restore original":
                            s["state"] = "skipped"
                hooks._emit()
                self.finished_signal.emit(True, "", res)
            else:
                err_msg = res.get("error") or res.get("restored_text") or "Verification failed and backup was restored."
                self.finished_signal.emit(False, err_msg, res)
        except Exception as e:
            traceback.print_exc()
            hooks.fail()
            if cover_path and os.path.exists(cover_path):
                try:
                    os.remove(cover_path)
                except Exception:
                    pass
            self.finished_signal.emit(False, str(e), {})


# ----------------------------------------
# UI Components
# ----------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, parent, current_settings):
        super().__init__(parent)
        self.setWindowTitle(f"Settings & Scan ({APP_VERSION})")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title_lbl = QLabel(f"Settings & Scan <span style='color:#888; font-size:13px;'>({APP_VERSION})</span>")
        title_lbl.setTextFormat(Qt.RichText)
        title_lbl.setStyleSheet("font-size: 18px; font-weight: bold; color: #e5a00d;")
        layout.addWidget(title_lbl)

        # Directory selection with browse button
        dir_layout = QHBoxLayout()
        self.dir_input = QLineEdit(current_settings.get("directory", ""))
        self.dir_input.setPlaceholderText("/path/to/media/movies")
        self.dir_browse_btn = QPushButton("Browse...")
        self.dir_browse_btn.clicked.connect(self.browse_dir)
        dir_layout.addWidget(self.dir_input, 1)
        dir_layout.addWidget(self.dir_browse_btn)

        self.url_input = QLineEdit(current_settings.get("plex_url", "http://127.0.0.1:32400"))
        self.token_input = QLineEdit(current_settings.get("plex_token", ""))
        self.token_input.setEchoMode(QLineEdit.Password)

        # Backup directory selection with browse button
        backup_layout = QHBoxLayout()
        self.backup_input = QLineEdit(current_settings.get("backup_dir", ""))
        self.backup_input.setPlaceholderText("Optional (blank = temporary safety copy next to file)")
        self.backup_browse_btn = QPushButton("Browse...")
        self.backup_browse_btn.clicked.connect(self.browse_backup)
        backup_layout.addWidget(self.backup_input, 1)
        backup_layout.addWidget(self.backup_browse_btn)

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("Folder to Scan:", dir_layout)
        form.addRow("Plex URL:", self.url_input)
        form.addRow("Plex Token:", self.token_input)
        form.addRow("Backup Dir:", backup_layout)
        layout.addLayout(form)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color: #e5a00d;")
        layout.addWidget(self.status_lbl)

        btn_layout = QHBoxLayout()
        self.scan_btn = QPushButton("Start Scan")
        self.scan_btn.setStyleSheet("background-color: #e5a00d; color: #111; font-weight: bold; padding: 8px 16px;")
        self.scan_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.scan_btn, 1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet("padding: 8px 16px;")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        layout.addLayout(btn_layout)

    def browse_dir(self):
        start_dir = self.dir_input.text().strip() or "/mnt"
        if not os.path.isdir(start_dir):
            start_dir = "/mnt" if os.path.isdir("/mnt") else os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Scan", start_dir)
        if folder:
            self.dir_input.setText(folder)

    def browse_backup(self):
        start_dir = self.backup_input.text().strip() or "/mnt"
        if not os.path.isdir(start_dir):
            start_dir = "/mnt" if os.path.isdir("/mnt") else os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Select Backup Directory", start_dir)
        if folder:
            self.backup_input.setText(folder)

    def get_settings(self):
        return {
            "directory": self.dir_input.text().strip(),
            "plex_url": self.url_input.text().strip(),
            "plex_token": self.token_input.text().strip(),
            "backup_dir": self.backup_input.text().strip()
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Plex Metadata Reviewer {APP_VERSION}")
        self.resize(1180, 860)
        self.setStyleSheet("""
            QMainWindow { background-color: #1e1f22; color: #ddd; font-family: system-ui, sans-serif; font-size: 14px; }
            QWidget { background-color: #1e1f22; color: #ddd; }
            QGroupBox { border: 1px solid #3a3d44; border-radius: 6px; margin-top: 12px; padding-top: 10px; font-weight: bold; }
            QGroupBox::title { color: #e5a00d; subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QLineEdit, QPlainTextEdit { background-color: #15161a; border: 1px solid #444; border-radius: 4px; color: #ddd; padding: 5px; }
            QPushButton { background-color: #3a3d44; color: #eee; border: 1px solid #555; border-radius: 4px; padding: 8px 16px; font-weight: bold; }
            QPushButton:hover { background-color: #4a4e57; }
            QPushButton:disabled { background-color: #2a2c30; color: #666; border-color: #3a3d44; }
        """)

        # State
        self.settings = {
            "directory": "",
            "plex_url": "http://127.0.0.1:32400",
            "plex_token": "",
            "backup_dir": ""
        }
        self.settings_path = os.path.expanduser('~/scripts_meta_data/plex_metadata_editor_settings.json')
        self.load_settings()
        self.queue = []
        self.history = []
        self.current_item = None
        self.current_poster_bytes = None
        self.merged_tags = {}

        self.auto_timer = QTimer(self)
        self.auto_timer.setInterval(100)
        self.auto_timer.timeout.connect(self.auto_tick)
        self.auto_ms = 0

        # UI Setup
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Header
        header_frame = QFrame()
        header_frame.setStyleSheet("QFrame { background-color: #2b2d31; border-bottom: 1px solid #111; }")
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(14)

        left_header_col = QVBoxLayout()
        left_header_col.setSpacing(4)
        self.settings_btn = QPushButton("⚙️ Settings")
        self.settings_btn.setFixedWidth(115)
        self.settings_btn.clicked.connect(self.open_settings)
        self.version_lbl = QLabel(APP_VERSION)
        self.version_lbl.setAlignment(Qt.AlignCenter)
        self.version_lbl.setStyleSheet(
            "color: #e5a00d; background-color: #15161a; border: 1px solid #3a3d44; "
            "border-radius: 4px; font-size: 11px; font-weight: bold; padding: 1px 6px;"
        )
        left_header_col.addWidget(self.settings_btn)
        left_header_col.addWidget(self.version_lbl)
        header_layout.addLayout(left_header_col, 0)

        queue_info_layout = QVBoxLayout()
        queue_info_layout.setSpacing(4)
        self.queue_lbl = QLabel("Queue: 0 items")
        self.queue_lbl.setStyleSheet("font-weight: bold; font-size: 16px; background-color: transparent; border: none;")
        self.file_lbl = QLabel("")
        self.file_lbl.setWordWrap(True)
        self.file_lbl.setStyleSheet("color: #aaa; font-family: monospace; font-size: 13px; background-color: transparent; border: none;")
        queue_info_layout.addWidget(self.queue_lbl)
        queue_info_layout.addWidget(self.file_lbl)
        header_layout.addLayout(queue_info_layout, 1)

        # Auto-Mode box
        auto_frame = QFrame()
        auto_frame.setStyleSheet("QFrame { background-color: #15161a; border: 1px solid #444; border-radius: 4px; }")
        auto_layout = QHBoxLayout(auto_frame)
        auto_layout.setContentsMargins(10, 6, 10, 6)
        self.auto_cb = QCheckBox("Auto-Mode")
        self.auto_cb.setToolTip("Automatically approve and proceed after 3 seconds")
        self.auto_cb.setStyleSheet("background-color: transparent; border: none; font-weight: bold;")
        self.auto_cb.stateChanged.connect(self.toggle_auto)
        auto_layout.addWidget(self.auto_cb)
        header_layout.addWidget(auto_frame, 0, Qt.AlignTop)

        # Fixed-size Action Buttons + Auto-Mode Countdown Bar
        controls_layout = QVBoxLayout()
        controls_layout.setSpacing(4)
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        self.prev_btn = QPushButton("Prev")
        self.prev_btn.setFixedWidth(75)
        self.prev_btn.setStyleSheet("""
            QPushButton { background-color: #5c626b; color: #fff; border: 1px solid #4a4e57; }
            QPushButton:hover { background-color: #6c727b; }
            QPushButton:disabled { background-color: #2a2c30; color: #666; border-color: #3a3d44; }
        """)
        self.prev_btn.clicked.connect(self.do_prev)

        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setFixedWidth(75)
        self.skip_btn.setStyleSheet("""
            QPushButton { background-color: #5c626b; color: #fff; border: 1px solid #4a4e57; }
            QPushButton:hover { background-color: #6c727b; }
            QPushButton:disabled { background-color: #2a2c30; color: #666; border-color: #3a3d44; }
        """)
        self.skip_btn.clicked.connect(self.do_skip)

        self.save_btn = QPushButton("OK & Next")
        self.save_btn.setFixedWidth(115)
        self.save_btn.setStyleSheet("""
            QPushButton { background-color: #e5a00d; color: #111; border: 1px solid #e5a00d; font-weight: bold; }
            QPushButton:hover { background-color: #f5b01d; }
            QPushButton:disabled { background-color: #5a4618; color: #888; border-color: #5a4618; }
        """)
        self.save_btn.clicked.connect(self.do_save)

        btn_layout.addWidget(self.prev_btn)
        btn_layout.addWidget(self.skip_btn)
        btn_layout.addWidget(self.save_btn)
        controls_layout.addLayout(btn_layout)

        self.auto_progress_bar = QProgressBar()
        self.auto_progress_bar.setFixedHeight(4)
        self.auto_progress_bar.setTextVisible(False)
        self.auto_progress_bar.setStyleSheet(
            "QProgressBar { background-color: #222; border: none; border-radius: 2px; } "
            "QProgressBar::chunk { background-color: #e5a00d; border-radius: 2px; }"
        )
        self.auto_progress_bar.hide()
        controls_layout.addWidget(self.auto_progress_bar)

        header_layout.addLayout(controls_layout, 0)
        main_layout.addWidget(header_frame)

        # Status & Save Progress Panel (matches web #status panel)
        self.status_frame = QFrame()
        self.status_frame.setStyleSheet(
            "QFrame#statusFrame { background-color: #25272c; border: 1px solid #3a3d44; border-radius: 6px; }"
        )
        self.status_frame.setObjectName("statusFrame")
        status_outer = QVBoxLayout()
        status_outer.setContentsMargins(20, 10, 20, 0)
        status_inner = QVBoxLayout(self.status_frame)
        status_inner.setContentsMargins(14, 10, 14, 10)
        status_inner.setSpacing(6)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setTextFormat(Qt.RichText)
        self.status_lbl.setStyleSheet("background-color: transparent; border: none; font-size: 13px;")
        status_inner.addWidget(self.status_lbl)

        # Dedicated Save Step Progress Bar with % display
        self.save_progress_bar = QProgressBar()
        self.save_progress_bar.setFixedHeight(14)
        self.save_progress_bar.setMaximumWidth(560)
        self.save_progress_bar.setTextVisible(True)
        self.save_progress_bar.setFormat("%p%")
        self.save_progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #15161a;
                border: 1px solid #444;
                border-radius: 6px;
                text-align: center;
                color: #fff;
                font-size: 11px;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #e5a00d;
                border-radius: 5px;
            }
        """)
        self.save_progress_bar.hide()
        status_inner.addWidget(self.save_progress_bar)

        self.status_frame.hide()
        status_outer.addWidget(self.status_frame)
        main_layout.addLayout(status_outer)

        # Content Split
        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(20, 14, 20, 20)
        content_layout.setSpacing(20)

        # Poster
        self.poster_lbl = QLabel()
        self.poster_lbl.setFixedSize(320, 480)
        self.poster_lbl.setStyleSheet("background-color: #111; border: 1px solid #2b2d31; border-radius: 6px;")
        self.poster_lbl.setAlignment(Qt.AlignCenter)
        content_layout.addWidget(self.poster_lbl, 0, Qt.AlignTop)

        # Scroll Area for Form
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")
        self.form_widget = QWidget()
        self.form_layout = QVBoxLayout(self.form_widget)
        self.form_layout.setContentsMargins(0, 0, 8, 0)
        self.form_layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.form_widget)
        content_layout.addWidget(scroll, 1)

        main_layout.addLayout(content_layout, 1)

        QTimer.singleShot(100, self.open_settings)

    def load_settings(self):
        if os.path.exists(self.settings_path):
            try:
                with open(self.settings_path) as f:
                    s = json.load(f)
                    for k in ("directory", "plex_url", "plex_token", "backup_dir"):
                        if k in s:
                            self.settings[k] = s[k]
            except Exception as e:
                print(f"Failed to load settings: {e}")

    def save_settings(self, new_s):
        self.settings.update(new_s)
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            existing = {}
            if os.path.exists(self.settings_path):
                with open(self.settings_path, 'r') as f:
                    existing = json.load(f)
            existing.update(self.settings)
            with open(self.settings_path, 'w') as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            print(f"Failed to save settings: {e}")

    def show_status(self, msg, color="#ddd", show_bar=False):
        self.status_lbl.setText(f"<span style='color:{color}'>{msg}</span>")
        self.save_progress_bar.setVisible(show_bar)
        self.status_frame.show()

    def open_settings(self):
        self.stop_auto()
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec_() == QDialog.Accepted:
            self.save_settings(dlg.get_settings())
            self.start_discovery()

    def set_busy(self, busy):
        self.save_btn.setDisabled(busy)
        self.skip_btn.setDisabled(busy)
        self.prev_btn.setDisabled(busy)
        self.settings_btn.setDisabled(busy)

    def start_discovery(self):
        self.set_busy(True)
        self.show_status("Querying Plex and scanning folder... this may take a minute.", "#aaa")
        self.queue = []
        self.history = []
        self.current_item = None
        self.current_poster_bytes = None
        self.form_widget.hide()

        self.disc_thread = DiscoveryThread(
            self.settings.get("plex_url"),
            self.settings.get("plex_token"),
            self.settings.get("directory")
        )
        self.disc_thread.finished_signal.connect(self.on_discovery_finished)
        self.disc_thread.error_signal.connect(self.on_discovery_error)
        self.disc_thread.start()

    def on_discovery_finished(self, matches, count):
        self.queue = matches
        self.show_status(f"Found {count} items needing update.", "#5c5")
        self.load_next()

    def on_discovery_error(self, err):
        self.set_busy(False)
        self.show_status(f"Discovery Error: {err}", "#f55")

    def load_next(self):
        self.stop_auto()
        self.set_busy(True)
        if not self.queue:
            self.show_status("Queue Complete! 🎉", "#5c5")
            self.queue_lbl.setText("Queue: 0 items left")
            self.file_lbl.setText("")
            self.poster_lbl.clear()
            self.poster_lbl.setText("Done")
            self.form_widget.hide()
            self.prev_btn.setDisabled(False)
            self.settings_btn.setDisabled(False)
            return

        rk, path = self.queue.pop(0)
        self.current_item = (rk, path)
        self.queue_lbl.setText(f"Queue: {len(self.queue)} left")
        self.file_lbl.setText(f"{path} (Loading...)")

        self.load_thread = LoadItemThread(
            self.settings.get("plex_url"),
            self.settings.get("plex_token"),
            rk,
            path
        )
        self.load_thread.finished_signal.connect(self.on_load_finished)
        self.load_thread.error_signal.connect(self.on_load_error)
        self.load_thread.start()

    def on_load_error(self, err):
        self.set_busy(False)
        self.show_status(f"Load Error: {err}", "#f55")

    def on_load_finished(self, data):
        self.current_poster_bytes = data.get("poster_bytes")
        self.file_lbl.setText(f"{data['path']}   ({format_size(data['size'])})")

        if self.current_poster_bytes:
            pixmap = QPixmap()
            pixmap.loadFromData(self.current_poster_bytes)
            self.poster_lbl.setPixmap(pixmap.scaled(320, 480, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.poster_lbl.clear()
            self.poster_lbl.setText("No Poster")

        f = data['file_tags']
        p = data['plex_tags']
        tags = dict(f)
        mk = {}

        for k, v in p.items():
            if v is not None and v != '' and v != []:
                tags[k] = v
                fv = f.get(k)
                if fv is None or fv == '' or fv == []:
                    mk[k] = ('new', 'New from Plex')
                elif json.dumps(fv, sort_keys=True) != json.dumps(v, sort_keys=True):
                    mk[k] = ('chg', f'Was: {json.dumps(fv)}')

        for k, v in f.items():
            if v and (not p.get(k)) and tags.get(k) is None:
                tags[k] = v

        self.merged_tags = tags
        self.render_form(tags, mk)
        self.form_widget.show()

        self.set_busy(False)
        if self.auto_cb.isChecked():
            self.start_auto()

    def render_form(self, t, mk):
        # Clear old layout
        while self.form_layout.count():
            item = self.form_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        def create_group(title, fields):
            group = QGroupBox(title)
            layout = QGridLayout()
            layout.setHorizontalSpacing(14)
            layout.setVerticalSpacing(10)
            row, col = 0, 0
            for k, lbl, span in fields:
                v = t.get(k, "")
                if isinstance(v, list):
                    if k == 'roles':
                        v = ", ".join([
                            f"{x.get('actor', '')} ({x.get('character', '')})" if x.get('character') else x.get('actor', '')
                            for x in v
                        ])
                    elif k == 'extra_tags':
                        v = "\n".join([f"{x.get('name', '')}: {x.get('value', '')}" for x in v])
                    else:
                        v = ", ".join(str(x) for x in v)

                is_multiline = k in ('summary', 'extra_tags')
                widg = QPlainTextEdit(str(v or "")) if is_multiline else QLineEdit(str(v or ""))
                widg.setReadOnly(True)

                if is_multiline:
                    widg.setFixedHeight(68)

                if k in mk:
                    kind, tip = mk[k]
                    if kind == 'new':
                        border_col = "#00e676"
                        bg_col = "#142e1b"
                        badge_bg = "#2e7d32"
                        badge_txt = "NEW"
                    else:
                        border_col = "#ff9800"
                        bg_col = "#33220d"
                        badge_bg = "#cc6600"
                        badge_txt = "CHG"

                    cls_name = "QPlainTextEdit" if is_multiline else "QLineEdit"
                    widg.setStyleSheet(
                        f"{cls_name} {{ "
                        f"border: 2px solid {border_col}; "
                        f"background-color: {bg_col}; "
                        f"color: #ffffff; "
                        f"font-weight: bold; "
                        f"border-radius: 4px; "
                        f"padding: 5px; "
                        f"}}"
                    )
                    widg.setToolTip(tip)
                    lbl_html = (
                        f"<span style='color: {border_col}; font-weight: bold;'>{lbl}</span> "
                        f"<span style='background-color: {badge_bg}; color: #ffffff; font-size: 10px; "
                        f"font-weight: bold; padding: 1px 6px; border-radius: 4px;'>&nbsp;{badge_txt}&nbsp;</span>"
                    )
                else:
                    lbl_html = f"<span style='color: #aaa;'>{lbl}</span>"

                label_w = QLabel(lbl_html)
                label_w.setTextFormat(Qt.RichText)
                label_w.setStyleSheet("font-size: 12px; background-color: transparent; border: none;")
                if k in mk:
                    label_w.setToolTip(mk[k][1])

                container = QVBoxLayout()
                container.setSpacing(3)
                container.addWidget(label_w)
                container.addWidget(widg)
                container.setContentsMargins(0, 0, 0, 4)

                colspan = 2 if span else 1
                if colspan == 2 and col == 1:
                    col = 0
                    row += 1
                layout.addLayout(container, row, col, 1, colspan)

                col += colspan
                if col >= 2:
                    col = 0
                    row += 1
            group.setLayout(layout)
            return group

        self.form_layout.addWidget(create_group("Title & Summary", [
            ('title', 'Title', False),
            ('sort_title', 'Sort Title', False),
            ('original_title', 'Original Title', True),
            ('summary', 'Summary', True),
            ('tagline', 'Tagline', True)
        ]))

        self.form_layout.addWidget(create_group("Release & Studio", [
            ('release_date', 'Release Date', False),
            ('year', 'Year', False),
            ('studio', 'Studio', False),
            ('content_rating', 'Content Rating', False),
            ('imdb', 'IMDb ID', False),
            ('tmdb', 'TMDb ID', False),
            ('tvdb', 'TVDb ID', False)
        ]))

        self.form_layout.addWidget(create_group("Classification", [
            ('genres', 'Genres', True),
            ('collections', 'Collections', True),
            ('countries', 'Countries', True)
        ]))

        self.form_layout.addWidget(create_group("TV Show", [
            ('show_title', 'Show Title', False),
            ('sort_show', 'Sort Show', False),
            ('season', 'Season', False),
            ('episode', 'Episode', False),
            ('episode_id', 'Episode ID (e.g. S01E02)', True)
        ]))

        self.form_layout.addWidget(create_group("People", [
            ('directors', 'Directors', True),
            ('writers', 'Writers', True),
            ('producers', 'Producers', True),
            ('roles', 'Cast (Actor - Character)', True)
        ]))

        self.form_layout.addWidget(create_group("Other File Tags", [
            ('extra_tags', 'Global MKV Tags', True)
        ]))

    def toggle_auto(self):
        if self.auto_cb.isChecked() and self.current_item and self.save_btn.isEnabled():
            self.start_auto()
        else:
            self.stop_auto()

    def start_auto(self):
        self.auto_ms = 0
        self.auto_progress_bar.setMaximum(3000)
        self.auto_progress_bar.setValue(0)
        self.auto_progress_bar.show()
        self.auto_timer.start()

    def stop_auto(self):
        self.auto_timer.stop()
        self.auto_progress_bar.setValue(0)
        self.auto_progress_bar.hide()

    def auto_tick(self):
        self.auto_ms += 100
        self.auto_progress_bar.setValue(self.auto_ms)
        if self.auto_ms >= 3000:
            self.stop_auto()
            self.do_save()

    def do_prev(self):
        self.stop_auto()
        if not self.history:
            self.show_status("Already at the beginning of the queue", "#888")
            return
        if self.current_item:
            self.queue.insert(0, self.current_item)
        rk, path = self.history.pop()
        self.queue.insert(0, (rk, path))
        self.load_next()

    def do_skip(self):
        self.stop_auto()
        if self.current_item:
            self.history.append(self.current_item)
            self.show_status(f"Skipped {self.current_item[1]}", "#888")
        self.load_next()

    def do_save(self):
        self.stop_auto()
        if not self.current_item:
            return
        self.set_busy(True)
        rk, path = self.current_item

        self.save_progress_bar.setRange(0, 100)
        self.save_progress_bar.setValue(0)
        self.show_status(f"<b>Starting save for {os.path.basename(path)}...</b>", "#ddd", show_bar=True)

        self.save_thread = SaveItemThread(
            self.settings,
            rk,
            path,
            self.merged_tags,
            self.current_poster_bytes
        )
        self.save_thread.state_signal.connect(self.on_save_state)
        self.save_thread.finished_signal.connect(self.on_save_finished)
        self.save_thread.start()

    def on_save_state(self, state):
        if not state:
            return
        elapsed = int(state.get("elapsed", 0))
        mins, secs = divmod(elapsed, 60)
        html = (
            f"<b>Saving {state['name']}</b> "
            f"<span style='color:#888;'>· {mins}:{secs:02d} elapsed — please wait</span><br>"
        )

        active_step = None
        for s in state['steps']:
            color = "#888"
            icon = "·"
            weight = "normal"
            if s['state'] == 'done':
                color = "#5c5"
                icon = "✔"
            elif s['state'] == 'active':
                color = "#eee"
                icon = "▶"
                weight = "bold"
                active_step = s['name']
            elif s['state'] == 'failed':
                color = "#f55"
                icon = "✘"
            elif s['state'] == 'skipped':
                color = "#666"
                icon = "–"

            detail = ""
            if s['state'] == 'active':
                if state['pct'] is not None:
                    pct_val = state['pct']
                    detail = f" <span style='color:#e5a00d; font-weight:bold;'>{pct_val:.0f}%</span>"
                    if state.get('bytes_total'):
                        rate_str = f" · {format_size(state.get('rate', 0))}/s" if state.get('rate') else ""
                        detail += (
                            f" <span style='color:#aaa; font-weight:normal;'>"
                            f"({format_size(state['bytes_done'])} of {format_size(state['bytes_total'])}{rate_str})"
                            f"</span>"
                        )
                else:
                    step_el = int(state.get("step_elapsed", 0))
                    detail = f" <span style='color:#aaa; font-weight:normal;'>(working… {step_el}s)</span>"

            html += (
                f"<div style='color:{color}; font-weight:{weight}; margin-top:2px;'>"
                f"<span style='display:inline-block; width:18px;'>{icon}</span> {s['name']}{detail}"
                f"</div>"
            )

        self.status_lbl.setText(html)
        if active_step:
            self.save_progress_bar.show()
            if state['pct'] is not None:
                self.save_progress_bar.setRange(0, 100)
                self.save_progress_bar.setValue(int(round(state['pct'])))
                self.save_progress_bar.setFormat(f"{active_step}: {state['pct']:.1f}%")
            else:
                # Indeterminate pulsing bar when step doesn't report byte %
                self.save_progress_bar.setRange(0, 0)
        else:
            self.save_progress_bar.hide()
        self.status_frame.show()

    def on_save_finished(self, ok, err, res):
        self.save_progress_bar.setRange(0, 100)
        self.save_progress_bar.hide()
        if ok:
            self.history.append(self.current_item)
            self.show_status(f"✔ Saved and verified: {self.current_item[1]}", "#5c5", show_bar=False)
            self.load_next()
        else:
            self.show_status(f"✘ Save Failed: {err}", "#f55", show_bar=False)
            self.set_busy(False)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setApplicationName("Plex Metadata UI Editor")
    app.setApplicationVersion(APP_VERSION)
    try:
        import setproctitle
        setproctitle.setproctitle(PROCESS_TITLE)
    except ImportError:
        pass

    # Set dark theme for Qt
    app.setStyle("Fusion")
    dark_palette = QPalette()
    dark_palette.setColor(QPalette.Window, QColor(30, 31, 34))
    dark_palette.setColor(QPalette.WindowText, Qt.white)
    dark_palette.setColor(QPalette.Base, QColor(21, 22, 26))
    dark_palette.setColor(QPalette.AlternateBase, QColor(43, 45, 49))
    dark_palette.setColor(QPalette.ToolTipBase, QColor(43, 45, 49))
    dark_palette.setColor(QPalette.ToolTipText, Qt.white)
    dark_palette.setColor(QPalette.Text, Qt.white)
    dark_palette.setColor(QPalette.Button, QColor(58, 61, 68))
    dark_palette.setColor(QPalette.ButtonText, Qt.white)
    dark_palette.setColor(QPalette.BrightText, Qt.red)
    dark_palette.setColor(QPalette.Link, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.Highlight, QColor(229, 160, 13))
    dark_palette.setColor(QPalette.HighlightedText, Qt.black)
    app.setPalette(dark_palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
