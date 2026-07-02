# USB Recovery Pro V6.9 Final Test Ready
# Tek dosya: USB_Recovery_Pro_V4_6_5_Final_Layout_Polish.py
# Amaç: USB içindeki görünen dosyaları güvenli şekilde bilgisayara kopyalamak,
# raporlamak ve temel analiz yapmak.
# Not: Bu sürüm dosya kurtarma motoru olarak kopyalama odaklıdır; diske yazma/onarım yapmaz.

import os
import sys
import time
import json
import queue
import shutil
import threading
import platform
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from tkinter import Tk, StringVar, BooleanVar, IntVar, filedialog, messagebox
from tkinter import ttk

try:
    import ctypes
except Exception:
    ctypes = None

APP_NAME = "USB Recovery Pro"
APP_VERSION = "V6.9 Final Test Ready"
DEFAULT_SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information", "RECOVERY", "Windows", "Program Files",
    "Program Files (x86)", "ProgramData", ".Spotlight-V100", ".Trashes", "FOUND.000"
}

CATEGORY_RULES = {
    "Fotoğraflar": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff"},
    "Videolar": {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp"},
    "Belgeler": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".csv"},
    "Arşivler": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Sesler": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
    "Diğer": set(),
}

@dataclass
class FileItem:
    src: str
    rel: str
    size: int
    ext: str
    category: str

@dataclass
class JobStats:
    started_at: str = ""
    finished_at: str = ""
    source: str = ""
    destination: str = ""
    scanned_files: int = 0
    selected_files: int = 0
    copied_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    total_bytes: int = 0
    copied_bytes: int = 0
    status: str = "Hazır"


def human_size(num: int) -> str:
    try:
        num = float(num)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if num < 1024:
                return f"{num:.1f} {unit}"
            num /= 1024
        return f"{num:.1f} PB"
    except Exception:
        return "0 B"


def safe_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    cleaned = ''.join('_' if ch in bad else ch for ch in name).strip()
    return cleaned or "unnamed"


def category_for(path: Path) -> str:
    ext = path.suffix.lower()
    for cat, exts in CATEGORY_RULES.items():
        if cat != "Diğer" and ext in exts:
            return cat
    return "Diğer"


def open_in_file_manager(path: str):
    try:
        if platform.system() == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        messagebox.showwarning("Açılamadı", f"Klasör açılamadı:\n{exc}")


def get_windows_drive_info(letter: str):
    if not ctypes:
        return None
    try:
        root = f"{letter}:\\"
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
        # 2 removable, 3 fixed, 4 remote, 5 cdrom
        name_buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_ulong(0)
        max_component = ctypes.c_ulong(0)
        flags = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetVolumeInformationW(
            root, name_buf, 260, ctypes.byref(serial), ctypes.byref(max_component),
            ctypes.byref(flags), fs_buf, 260
        )
        total = ctypes.c_ulonglong(0)
        free = ctypes.c_ulonglong(0)
        available = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(root, ctypes.byref(available), ctypes.byref(total), ctypes.byref(free))
        return {
            "root": root,
            "type": drive_type,
            "label": name_buf.value or "USB / Disk",
            "fs": fs_buf.value or "?",
            "total": int(total.value),
            "free": int(free.value),
        }
    except Exception:
        return None


def list_drives():
    """
    V4.3 düzeltmesi:
    Windows'ta yalnızca çıkarılabilir sürücüler listelenir.
    C:\\, dahili HDD/SSD ve sistem diskleri artık kaynak USB listesine eklenmez.
    """
    drives = []
    if platform.system() == "Windows" and ctypes:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                letter = chr(65 + i)
                info = get_windows_drive_info(letter)
                if not info:
                    continue

                # GetDriveTypeW:
                # 2 = Removable / USB
                # 3 = Fixed / dahili disk
                # Sadece USB bellekleri göster.
                if info["type"] != 2:
                    continue

                drives.append({
                    **info,
                    "display": f"{info['root']}  {info['label']}  • USB • {human_size(info['total'])}"
                })
    else:
        # Linux/macOS için basit fallback
        for base in [Path("/media"), Path("/mnt"), Path("/Volumes")]:
            if base.exists():
                for p in base.iterdir():
                    if p.is_dir():
                        try:
                            usage = shutil.disk_usage(str(p))
                            drives.append({
                                "root": str(p), "type": 2, "label": p.name, "fs": "?",
                                "total": usage.total, "free": usage.free,
                                "display": f"{p}  • USB • {human_size(usage.total)}"
                            })
                        except Exception:
                            pass
    return drives


class RecoveryWorker(threading.Thread):
    def __init__(self, app, source: Path, dest: Path, selected_categories: set[str], keep_structure: bool, overwrite: bool):
        super().__init__(daemon=True)
        self.app = app
        self.source = source
        self.dest = dest
        self.selected_categories = selected_categories
        self.keep_structure = keep_structure
        self.overwrite = overwrite
        self.stop_event = threading.Event()
        self.stats = JobStats(started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source=str(source), destination=str(dest))
        self.files: list[FileItem] = []

    def stop(self):
        self.stop_event.set()

    def emit(self, kind, payload):
        self.app.events.put((kind, payload))

    def scan(self):
        self.emit("log", "Ön analiz başladı. USB içeriği taranıyor...")
        scanned = 0
        selected = 0
        total_bytes = 0
        for root, dirs, files in os.walk(self.source):
            if self.stop_event.is_set():
                break
            dirs[:] = [d for d in dirs if d not in DEFAULT_SKIP_DIRS]
            root_path = Path(root)
            for filename in files:
                if self.stop_event.is_set():
                    break
                try:
                    src = root_path / filename
                    if not src.exists() or not src.is_file():
                        continue
                    rel = src.relative_to(self.source)
                    size = src.stat().st_size
                    cat = category_for(src)
                    scanned += 1
                    if cat in self.selected_categories:
                        selected += 1
                        total_bytes += size
                        self.files.append(FileItem(str(src), str(rel), size, src.suffix.lower(), cat))
                    if scanned % 100 == 0:
                        self.emit("scan_progress", {"scanned": scanned, "selected": selected, "bytes": total_bytes})
                except PermissionError:
                    self.stats.skipped_files += 1
                except Exception:
                    self.stats.skipped_files += 1
        self.stats.scanned_files = scanned
        self.stats.selected_files = selected
        self.stats.total_bytes = total_bytes
        self.emit("scan_progress", {"scanned": scanned, "selected": selected, "bytes": total_bytes})
        self.emit("log", f"Ön analiz bitti: {selected} dosya seçildi, toplam {human_size(total_bytes)}.")

    def unique_destination(self, target: Path) -> Path:
        if self.overwrite or not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        parent = target.parent
        n = 1
        while True:
            candidate = parent / f"{stem} ({n}){suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    def copy_one(self, item: FileItem):
        src = Path(item.src)
        if self.keep_structure:
            target = self.dest / safe_name(APP_NAME.replace(" ", "_")) / item.rel
        else:
            target = self.dest / safe_name(APP_NAME.replace(" ", "_")) / item.category / src.name
        target = self.unique_destination(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        return target

    def write_report(self):
        report_dir = self.dest / safe_name(APP_NAME.replace(" ", "_")) / "_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        txt_path = report_dir / f"recovery_report_{stamp}.txt"
        json_path = report_dir / f"recovery_report_{stamp}.json"
        lines = [
            f"{APP_NAME} {APP_VERSION}",
            "=" * 52,
            f"Başlangıç: {self.stats.started_at}",
            f"Bitiş: {self.stats.finished_at}",
            f"Kaynak: {self.stats.source}",
            f"Hedef: {self.stats.destination}",
            "",
            f"Taranan dosya: {self.stats.scanned_files}",
            f"Seçilen dosya: {self.stats.selected_files}",
            f"Kopyalanan dosya: {self.stats.copied_files}",
            f"Atlanan dosya: {self.stats.skipped_files}",
            f"Hatalı dosya: {self.stats.failed_files}",
            f"Toplam boyut: {human_size(self.stats.total_bytes)}",
            f"Kopyalanan boyut: {human_size(self.stats.copied_bytes)}",
            f"Durum: {self.stats.status}",
            "",
            "Not: Bu araç USB içindeki dosyaları hedef klasöre güvenli şekilde kopyalar; kaynak diske onarım/yazma yapmaz.",
        ]
        txt_path.write_text("\n".join(lines), encoding="utf-8")
        json_path.write_text(json.dumps(asdict(self.stats), ensure_ascii=False, indent=2), encoding="utf-8")
        return txt_path

    def run(self):
        try:
            self.stats.status = "Analiz"
            self.scan()
            if self.stop_event.is_set():
                self.stats.status = "Durduruldu"
                self.emit("done", self.stats)
                return
            if not self.files:
                self.stats.status = "Dosya bulunamadı"
                self.emit("log", "Seçili kategoriye uygun dosya bulunamadı.")
                self.emit("done", self.stats)
                return
            self.stats.status = "Kopyalama"
            self.emit("log", "Kopyalama başladı. Kaynak USB üzerinde değişiklik yapılmıyor.")
            copied_bytes = 0
            total = max(1, self.stats.total_bytes)
            for index, item in enumerate(self.files, 1):
                if self.stop_event.is_set():
                    self.stats.status = "Durduruldu"
                    self.emit("log", "İşlem kullanıcı tarafından durduruldu.")
                    break
                try:
                    self.copy_one(item)
                    self.stats.copied_files += 1
                    copied_bytes += item.size
                    self.stats.copied_bytes = copied_bytes
                except PermissionError:
                    self.stats.skipped_files += 1
                    self.emit("log", f"Yetki yok, atlandı: {item.rel}")
                except Exception as exc:
                    self.stats.failed_files += 1
                    self.emit("log", f"Hata: {item.rel} → {exc}")
                pct = int((copied_bytes / total) * 100)
                if index % 5 == 0 or index == len(self.files):
                    self.emit("copy_progress", {"pct": pct, "current": index, "total": len(self.files), "bytes": copied_bytes})
            if self.stats.status != "Durduruldu":
                self.stats.status = "Tamamlandı"
            self.stats.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            report = self.write_report()
            self.emit("log", f"Rapor oluşturuldu: {report}")
            self.emit("done", self.stats)
        except Exception as exc:
            self.stats.status = "Kritik hata"
            self.stats.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.emit("log", f"Kritik hata: {exc}")
            self.emit("done", self.stats)


class App(Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        # V6.9 Final Test Ready - GUI/Layout only:
        # 1366x768 ekranlarda taşma yapmaması için pencere boyutu ekrana göre ayarlanır.
        self.configure(bg="#070a12")
        self.apply_screen_fit_geometry()
        self.after(50, self.center_window)
        self.events = queue.Queue()
        self.worker: RecoveryWorker | None = None
        self.drives = []
        self.metric_value_labels = {}
        self.last_drive_signature = ""
        self.is_refreshing_usb = False

        self.source_var = StringVar()
        self.dest_var = StringVar(value=str(Path.home() / "Desktop"))
        self.status_var = StringVar(value="Hazır")
        self.drive_info_var = StringVar(value="USB bulunamadı")
        self.scan_var = StringVar(value="Taranan: 0 • Seçilen: 0 • Boyut: 0 B")
        self.copy_var = StringVar(value="Kopyalama bekliyor")
        self.footer_var = StringVar(value="Ready • CPU: -- • RAM: -- • Süre: 00:00")
        self.header_title_var = StringVar(value="◆ USB Recovery Pro")
        self.header_sub_var = StringVar(value=f"{APP_VERSION} • Professional Edition • Safe Copy Engine")
        self.started_ts = None
        self.keep_structure_var = BooleanVar(value=True)
        self.overwrite_var = BooleanVar(value=False)
        self.progress_var = IntVar(value=0)
        self.progress_text_var = StringVar(value="İlerleme: 0%")
        self.cat_vars = {name: BooleanVar(value=True) for name in CATEGORY_RULES.keys()}

        self.setup_style()
        self.build_ui()
        self.status_var.trace_add("write", lambda *_: self.update_status_color())
        self.update_status_color()
        self.refresh_drives(log_result=True)
        self.after(120, self.consume_events)
        self.after(1000, self.update_footer)
        self.after(1800, self.auto_usb_watch)

    def apply_screen_fit_geometry(self):
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            w = min(1280, max(1120, sw - 50))
            h = min(780, max(720, sh - 55))
            self.geometry(f"{w}x{h}")
            self.minsize(1080, 700)
        except Exception:
            self.geometry("1240x730")
            self.minsize(1080, 700)

    def center_window(self):
        try:
            self.update_idletasks()
            w = self.winfo_width()
            h = self.winfo_height()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        self.COL_BG = "#050914"
        self.COL_PANEL = "#0b1220"
        self.COL_CARD = "#101a2c"
        self.COL_CARD2 = "#111d33"
        self.COL_LINE = "#1f3b5a"
        self.COL_TEXT = "#eef7ff"
        self.COL_MUTED = "#93a9c7"
        self.COL_CYAN = "#38d9ff"
        self.COL_GREEN = "#4ade80"
        self.COL_RED = "#fb7185"
        self.COL_WARN = "#facc15"

        style.configure("TFrame", background=self.COL_BG)
        style.configure("Root.TFrame", background=self.COL_BG)
        style.configure("Sidebar.TFrame", background=self.COL_PANEL)
        style.configure("Card.TFrame", background=self.COL_CARD, relief="flat")
        style.configure("SoftCard.TFrame", background=self.COL_CARD2, relief="flat")
        style.configure("Inner.TFrame", background=self.COL_CARD)
        style.configure("Hero.TFrame", background="#0e1b31")
        style.configure("Footer.TFrame", background=self.COL_PANEL)

        style.configure("TLabel", background=self.COL_BG, foreground=self.COL_TEXT, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=self.COL_BG, foreground="#ffffff", font=("Segoe UI", 21, "bold"))
        style.configure("Sub.TLabel", background=self.COL_BG, foreground=self.COL_CYAN, font=("Segoe UI", 9))
        style.configure("Card.TLabel", background=self.COL_CARD, foreground=self.COL_TEXT, font=("Segoe UI", 9))
        style.configure("SoftCard.TLabel", background=self.COL_CARD2, foreground=self.COL_TEXT, font=("Segoe UI", 9))
        style.configure("Hero.TLabel", background="#0e1b31", foreground=self.COL_TEXT, font=("Segoe UI", 9))
        style.configure("Muted.TLabel", background=self.COL_CARD, foreground=self.COL_MUTED, font=("Segoe UI", 8))
        style.configure("SidebarMuted.TLabel", background=self.COL_PANEL, foreground=self.COL_MUTED, font=("Segoe UI", 8))
        style.configure("Section.TLabel", background=self.COL_CARD, foreground="#ffffff", font=("Segoe UI", 11, "bold"))
        style.configure("MetricTitle.TLabel", background=self.COL_CARD, foreground=self.COL_MUTED, font=("Segoe UI", 8))
        style.configure("Metric.TLabel", background=self.COL_CARD, foreground="#ffffff", font=("Segoe UI", 19, "bold"))
        style.configure("MetricOk.TLabel", background=self.COL_CARD, foreground=self.COL_GREEN, font=("Segoe UI", 19, "bold"))
        style.configure("MetricBad.TLabel", background=self.COL_CARD, foreground=self.COL_RED, font=("Segoe UI", 19, "bold"))
        style.configure("MetricWarn.TLabel", background=self.COL_CARD, foreground=self.COL_WARN, font=("Segoe UI", 19, "bold"))
        style.configure("MetricBlue.TLabel", background=self.COL_CARD, foreground=self.COL_CYAN, font=("Segoe UI", 19, "bold"))

        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(12, 10), background="#172846", foreground="#eef7ff", borderwidth=1)
        style.map("TButton", background=[("active", "#223c65")])
        style.configure("Accent.TButton", background="#145cff", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#2a73ff"), ("disabled", "#253044")])
        style.configure("Danger.TButton", background="#8a1e35", foreground="#ffffff")
        style.map("Danger.TButton", background=[("active", "#ad2946")])
        style.configure("Ghost.TButton", background="#0f1e34", foreground="#dce8ff")
        style.map("Ghost.TButton", background=[("active", "#1a3152")])

        style.configure("TCheckbutton", background=self.COL_CARD, foreground=self.COL_TEXT, font=("Segoe UI", 8))
        style.map("TCheckbutton", background=[("active", self.COL_CARD)])
        style.configure("Side.TCheckbutton", background=self.COL_CARD, foreground=self.COL_TEXT, font=("Segoe UI", 8))
        style.map("Side.TCheckbutton", background=[("active", self.COL_CARD)])
        style.configure("Horizontal.TProgressbar", troughcolor="#07101e", background=self.COL_CYAN, bordercolor=self.COL_CARD, lightcolor=self.COL_CYAN, darkcolor=self.COL_CYAN, thickness=80)
        style.configure("Success.Horizontal.TProgressbar", troughcolor="#07101e", background=self.COL_GREEN, bordercolor=self.COL_CARD, lightcolor=self.COL_GREEN, darkcolor=self.COL_GREEN, thickness=80)
        style.configure("TCombobox", fieldbackground="#07101e", background="#07101e", foreground="#ffffff")

    def card(self, parent, **grid):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=12)
        frame.grid(**grid)
        return frame

    def side_card(self, parent, **grid):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=12)
        frame.grid(**grid)
        return frame

    def pill_label(self, parent, text, col=0):
        lbl = ttk.Label(parent, text=text, style="Hero.TLabel", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=col, sticky="w", padx=(0, 18))
        return lbl

    def build_ui(self):
        root = ttk.Frame(self, style="Root.TFrame", padding=(18, 14))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=0, minsize=268)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root, style="Root.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, textvariable=self.header_title_var, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.header_sub_var, style="Sub.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Button(header, text="⟳ USB Yenile", command=self.refresh_drives).grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0), ipadx=8)

        sidebar = ttk.Frame(root, style="Sidebar.TFrame", padding=(0, 0))
        sidebar.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        sidebar.columnconfigure(0, weight=1)

        source_card = self.side_card(sidebar, row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(source_card, text="01  Kaynak USB", style="Section.TLabel").pack(anchor="w")
        self.drive_combo = ttk.Combobox(source_card, textvariable=self.source_var, state="readonly", width=30)
        self.drive_combo.pack(fill="x", pady=(10, 7))
        self.drive_combo.bind("<<ComboboxSelected>>", lambda e: self.update_drive_info())
        ttk.Label(source_card, textvariable=self.drive_info_var, style="Muted.TLabel", wraplength=226, justify="left").pack(anchor="w")

        dest_card = self.side_card(sidebar, row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(dest_card, text="02  Hedef Klasör", style="Section.TLabel").pack(anchor="w")
        ttk.Label(dest_card, textvariable=self.dest_var, style="Muted.TLabel", wraplength=226, justify="left").pack(anchor="w", pady=(8, 8))
        ttk.Button(dest_card, text="Hedef Seç", command=self.choose_dest, style="Ghost.TButton").pack(fill="x")

        cat_card = self.side_card(sidebar, row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(cat_card, text="03  Kategoriler", style="Section.TLabel").pack(anchor="w")
        cat_grid = ttk.Frame(cat_card, style="Inner.TFrame")
        cat_grid.pack(fill="x", pady=(8, 0))
        for i, cat in enumerate(CATEGORY_RULES.keys()):
            ttk.Checkbutton(cat_grid, text=cat, variable=self.cat_vars[cat], style="Side.TCheckbutton").grid(row=i//2, column=i%2, sticky="w", padx=(0, 12), pady=3)

        option_card = self.side_card(sidebar, row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(option_card, text="04  Güvenli Ayarlar", style="Section.TLabel").pack(anchor="w")
        ttk.Checkbutton(option_card, text="Klasör yapısını koru", variable=self.keep_structure_var, style="Side.TCheckbutton").pack(anchor="w", pady=(8, 3))
        ttk.Checkbutton(option_card, text="Aynı isim varsa üzerine yaz", variable=self.overwrite_var, style="Side.TCheckbutton").pack(anchor="w", pady=3)

        safety_card = self.side_card(sidebar, row=4, column=0, sticky="ew")
        ttk.Label(safety_card, text="Güvenlik", style="Section.TLabel").pack(anchor="w")
        ttk.Label(safety_card, text="Kaynak USB’ye yazma yapılmaz. Dosyalar sadece hedef klasöre kopyalanır.", style="Muted.TLabel", wraplength=226, justify="left").pack(anchor="w", pady=(7, 0))

        main = ttk.Frame(root, style="Root.TFrame")
        main.grid(row=1, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        metrics = ttk.Frame(main, style="Root.TFrame")
        metrics.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for i in range(3):
            metrics.columnconfigure(i, weight=1, uniform="metric")
        self.metric_status = self.metric_card(metrics, "● DURUM", self.status_var, 0)
        self.metric_scan = self.metric_card(metrics, "◆ ANALİZ", self.scan_var, 1)
        self.metric_copy = self.metric_card(metrics, "▶ KOPYALAMA", self.copy_var, 2)

        hero = ttk.Frame(main, style="Hero.TFrame", padding=(16, 12))
        hero.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        hero.columnconfigure(0, weight=1)
        ttk.Label(hero, text="⚡ Kurtarma Merkezi", style="Hero.TLabel", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(hero, text="USB analiz edilir, seçili kategoriler hedef klasöre güvenli şekilde kopyalanır.", style="Hero.TLabel", foreground="#a9bddb").grid(row=1, column=0, sticky="w", pady=(3, 8))
        self.progress = ttk.Progressbar(hero, variable=self.progress_var, maximum=100)
        self.progress.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(hero, textvariable=self.progress_text_var, style="Hero.TLabel", foreground=self.COL_CYAN, font=("Segoe UI", 9, "bold")).grid(row=3, column=0, sticky="e", pady=(0, 6))
        pill_row = ttk.Frame(hero, style="Hero.TFrame")
        pill_row.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        pill_row.columnconfigure(0, weight=1)
        pill_row.columnconfigure(1, weight=1)
        pill_row.columnconfigure(2, weight=1)
        ttk.Label(pill_row, text="✓ Kaynağa yazmaz", style="Hero.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(pill_row, text="✓ Rapor oluşturur", style="Hero.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(pill_row, text="✓ Güvenli kopyalama", style="Hero.TLabel").grid(row=0, column=2, sticky="w")
        btn_row = ttk.Frame(hero, style="Hero.TFrame")
        btn_row.grid(row=5, column=0, sticky="ew")
        for i in range(4):
            btn_row.columnconfigure(i, weight=1)
        self.start_btn = ttk.Button(btn_row, text="▶ Başlat", style="Accent.TButton", command=self.start_job)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8), ipady=5)
        self.start_btn.state(["disabled"])
        self.stop_btn = ttk.Button(btn_row, text="■ Durdur", style="Danger.TButton", command=self.stop_job)
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(0, 8), ipady=5)
        ttk.Button(btn_row, text="📂 Hedefi Aç", command=lambda: open_in_file_manager(self.dest_var.get()), style="Ghost.TButton").grid(row=0, column=2, sticky="ew", padx=(0, 8), ipady=5)
        ttk.Button(btn_row, text="Log Temizle", command=self.clear_log, style="Ghost.TButton").grid(row=0, column=3, sticky="ew", ipady=5)

        status_strip = ttk.Frame(main, style="Root.TFrame")
        status_strip.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        for i in range(3):
            status_strip.columnconfigure(i, weight=1, uniform="strip")
        for col, text in enumerate(["🛡 Safe Copy Mode", "📂 Visible File Recovery", "📄 Report Enabled"]):
            box = ttk.Frame(status_strip, style="SoftCard.TFrame", padding=(12, 11))
            box.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0))
            ttk.Label(box, text=text, style="SoftCard.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w")
            ttk.Label(box, text="Aktif", style="SoftCard.TLabel", foreground="#4ade80").pack(anchor="w")

        log_card = ttk.Frame(main, style="Card.TFrame", padding=12)
        log_card.grid(row=3, column=0, sticky="nsew")
        log_card.rowconfigure(1, weight=1)
        log_card.columnconfigure(0, weight=1)
        log_header = ttk.Frame(log_card, style="Inner.TFrame")
        log_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        log_header.columnconfigure(0, weight=1)
        ttk.Label(log_header, text="▣ Canlı İşlem Logu", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(log_header, text="durum mesajları", style="Muted.TLabel").grid(row=0, column=1, sticky="e")
        self.log_text = __import__("tkinter").Text(log_card, height=34, bg="#030812", fg="#d7f6ff", insertbackground="#ffffff", relief="flat", font=("Consolas", 9), wrap="word", padx=12, pady=9)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(log_card, command=self.log_text.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.tag_configure("ok", foreground="#4ade80")
        self.log_text.tag_configure("warn", foreground="#facc15")
        self.log_text.tag_configure("err", foreground="#fb7185")
        self.log_text.tag_configure("info", foreground="#7dd3fc")

        footer = ttk.Frame(main, style="Footer.TFrame", padding=(12, 8))
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.footer_var, style="SidebarMuted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(footer, text="Safe Copy • Kaynağa yazmaz", style="SidebarMuted.TLabel").grid(row=0, column=1, sticky="e")

        self.bind("<Configure>", self.on_resize_layout)
        self.log("Sistem hazır. USB tak, hedef klasörü belirle ve başlat.")

    def on_resize_layout(self, event=None):
        try:
            main_w = max(720, self.winfo_width() - 360)
            metric_wrap = max(210, min(360, main_w // 3 - 30))
            for lbl in self.metric_value_labels.values():
                lbl.configure(wraplength=metric_wrap)
        except Exception:
            pass

    def metric_card(self, parent, title, var, col):
        c = ttk.Frame(parent, style="Card.TFrame", padding=(18, 16))
        c.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0))
        c.configure(height=176)
        c.grid_propagate(False)
        c.columnconfigure(0, weight=1)
        ttk.Label(c, text=title, style="MetricTitle.TLabel").grid(row=0, column=0, sticky="w")
        value_label = ttk.Label(c, textvariable=var, style="Metric.TLabel", wraplength=260, justify="left")
        value_label.grid(row=1, column=0, sticky="w", pady=(14, 0))
        self.metric_value_labels[title.title() if title.isupper() else title] = value_label
        # Eski update_status_color "Durum" anahtarını aradığı için ayrıca ekle.
        if "DURUM" in title.upper():
            self.metric_value_labels["Durum"] = value_label
        elif "ANALİZ" in title.upper():
            self.metric_value_labels["Analiz"] = value_label
        elif "KOPYALAMA" in title.upper():
            self.metric_value_labels["Kopyalama"] = value_label
        return c

    def update_status_color(self):
        label = self.metric_value_labels.get("Durum")
        if not label:
            return
        status = self.status_var.get().lower()
        if any(x in status for x in ["hazır", "tamam"]):
            label.configure(style="MetricOk.TLabel")
        elif any(x in status for x in ["usb yok", "hata", "bulunamadı"]):
            label.configure(style="MetricBad.TLabel")
        elif any(x in status for x in ["durdur", "taranıyor"]):
            label.configure(style="MetricWarn.TLabel")
        else:
            label.configure(style="MetricBlue.TLabel")

    def set_status(self, value: str):
        self.status_var.set(value)
        self.update_status_color()

    def log(self, text: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        lower = text.lower()
        tag = "info"
        if any(x in lower for x in ["tamam", "bitti", "oluşturuldu", "bulundu", "hazır"]):
            tag = "ok"
        if any(x in lower for x in ["uyarı", "atlandı", "yetki", "durdur"]):
            tag = "warn"
        if any(x in lower for x in ["hata", "kritik", "bulunamadı", "açılamadı"]):
            tag = "err"
        self.log_text.insert("end", f"[{stamp}] ", "info")
        self.log_text.insert("end", f"{text}\n", tag)
        self.log_text.see("end")

    def clear_log(self):
        self.log_text.delete("1.0", "end")
        self.log("Log temizlendi.")

    def update_footer(self):
        try:
            elapsed = "00:00"
            if self.started_ts:
                sec = int(time.time() - self.started_ts)
                elapsed = f"{sec//60:02d}:{sec%60:02d}"
            cpu = "--"
            ram = "--"
            try:
                import psutil  # type: ignore
                cpu = f"{psutil.cpu_percent(interval=None):.0f}%"
                ram = f"{psutil.virtual_memory().percent:.0f}%"
            except Exception:
                pass
            self.footer_var.set(f"Ready • CPU: {cpu} • RAM: {ram} • Süre: {elapsed}")
        finally:
            self.after(1000, self.update_footer)

    def drive_signature(self):
        return "|".join(f"{d.get('root')}:{d.get('label')}:{d.get('total')}" for d in self.drives)

    def set_start_enabled(self, enabled: bool):
        if enabled and not (self.worker and self.worker.is_alive()):
            self.start_btn.state(["!disabled"])
        else:
            self.start_btn.state(["disabled"])

    def refresh_drives(self, log_result: bool = True, silent: bool = False):
        if self.is_refreshing_usb:
            return
        self.is_refreshing_usb = True
        try:
            if not silent:
                self.set_status("USB taranıyor")
                self.drive_info_var.set("USB sürücüleri kontrol ediliyor...")
                self.update_idletasks()

            previous_root = None
            old_drive = self.selected_drive()
            if old_drive:
                previous_root = old_drive.get("root")

            self.drives = list_drives()
            values = [d["display"] for d in self.drives]
            self.drive_combo["values"] = values

            selected_index = 0
            if previous_root:
                for i, d in enumerate(self.drives):
                    if d.get("root") == previous_root:
                        selected_index = i
                        break

            if values:
                self.drive_combo.current(selected_index)
                self.source_var.set(values[selected_index])
                self.update_drive_info()
                self.copy_var.set("Hazır • Kopyalama bekliyor")
                self.set_start_enabled(True)
                if log_result:
                    self.log(f"{len(values)} USB bulundu.")
            else:
                self.source_var.set("")
                self.drive_combo.set("")
                self.drive_info_var.set("USB bulunamadı. USB belleği takınca otomatik algılanır veya 'USB Yenile'ye bas.")
                self.set_status("USB yok")
                self.copy_var.set("Kopyalama bekliyor")
                self.set_start_enabled(False)
                if log_result:
                    self.log("USB bulunamadı.")

            self.last_drive_signature = self.drive_signature()
        finally:
            self.is_refreshing_usb = False

    def auto_usb_watch(self):
        try:
            if not (self.worker and self.worker.is_alive()):
                current = list_drives()
                sig = "|".join(f"{d.get('root')}:{d.get('label')}:{d.get('total')}" for d in current)
                if sig != self.last_drive_signature:
                    self.drives = current
                    self.last_drive_signature = sig
                    values = [d["display"] for d in self.drives]
                    self.drive_combo["values"] = values
                    if values:
                        self.drive_combo.current(0)
                        self.source_var.set(values[0])
                        self.update_drive_info()
                        self.set_start_enabled(True)
                        self.log(f"USB değişikliği algılandı: {len(values)} USB bulundu.")
                    else:
                        self.source_var.set("")
                        self.drive_combo.set("")
                        self.drive_info_var.set("USB çıkarıldı veya bulunamadı.")
                        self.set_status("USB yok")
                        self.set_start_enabled(False)
                        self.log("USB çıkarıldı veya bulunamadı.")
        except Exception as exc:
            self.log(f"USB izleme uyarısı: {exc}")
        finally:
            self.after(1800, self.auto_usb_watch)


    def selected_drive(self):
        idx = self.drive_combo.current()
        if idx < 0 or idx >= len(self.drives):
            return None
        return self.drives[idx]


    def format_eta(self, seconds):
        try:
            seconds = int(max(0, seconds))
            return f"{seconds//60:02d}:{seconds%60:02d}"
        except Exception:
            return "--:--"

    def update_usb_live_card(self, d):
        try:
            used = max(0, d["total"] - d["free"])
            self.status_var.set(
                f"{d['label']}\n{d['fs']} • {human_size(d['total'])}\nBoş: {human_size(d['free'])}"
            )
            self.drive_info_var.set(
                f"Yol: {d['root']}\nEtiket: {d['label']}\nDosya sistemi: {d['fs']}\nToplam: {human_size(d['total'])}\nBoş: {human_size(d['free'])}\nDolu: {human_size(used)}"
            )
        except Exception:
            pass

    def update_drive_info(self):
        d = self.selected_drive()
        if not d:
            self.drive_info_var.set("USB bulunamadı. USB belleği takınca otomatik algılanır veya 'USB Yenile'ye bas.")
            self.set_start_enabled(False)
            return
        self.update_usb_live_card(d)
        self.copy_var.set("Hazır • Kopyalama bekliyor")
        self.set_start_enabled(True)


    def choose_dest(self):
        folder = filedialog.askdirectory(title="Kurtarılan dosyalar nereye kopyalansın?")
        if folder:
            self.dest_var.set(folder)
            self.log(f"Hedef klasör seçildi: {folder}")

    def selected_categories(self):
        return {cat for cat, var in self.cat_vars.items() if var.get()}

    def validate_job(self):
        drive = self.selected_drive()
        if not drive:
            messagebox.showwarning("USB bulunamadı", "Önce USB belleği tak ve USB Yenile butonuna bas.")
            return None
        source = Path(drive["root"])
        dest = Path(self.dest_var.get())
        if not source.exists():
            messagebox.showwarning("Kaynak yok", "Seçili kaynak bulunamadı.")
            return None
        if not dest.exists():
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                messagebox.showwarning("Hedef oluşturulamadı", str(exc))
                return None
        if source.resolve() == dest.resolve():
            messagebox.showwarning("Hatalı hedef", "Hedef klasör kaynak USB ile aynı olamaz.")
            return None
        cats = self.selected_categories()
        if not cats:
            messagebox.showwarning("Kategori yok", "En az bir kategori seç.")
            return None
        return source, dest, cats

    def start_job(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("İşlem sürüyor", "Zaten çalışan bir işlem var.")
            return
        valid = self.validate_job()
        if not valid:
            return
        source, dest, cats = valid
        self.progress_var.set(0)
        self.progress_text_var.set("İlerleme: 0%")
        try:
            self.progress.configure(style="Horizontal.TProgressbar")
        except Exception:
            pass
        self.started_ts = time.time()
        self.set_status("Başladı")
        self.title(f"{APP_NAME} {APP_VERSION} - İşlem Devam Ediyor")
        self.header_title_var.set("⏳ Kurtarma İşlemi Devam Ediyor")
        self.header_sub_var.set("USB analiz ediliyor ve güvenli kopyalama için hazırlanıyor...")
        self.scan_var.set("Taranan: 0 • Seçilen: 0 • Boyut: 0 B")
        self.copy_var.set("Kopyalama bekliyor")
        self.set_start_enabled(False)
        self.worker = RecoveryWorker(self, source, dest, cats, self.keep_structure_var.get(), self.overwrite_var.get())
        self.worker.start()
        self.log("İşlem başlatıldı.")

    def stop_job(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self.set_status("Durduruluyor")
            self.log("Durdurma isteği gönderildi.")
        else:
            self.log("Çalışan işlem yok.")

    def consume_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "scan_progress":
                    self.set_status("Analiz")
                    self.scan_var.set(f"{payload['selected']} dosya\nTaranan: {payload['scanned']}\nBoyut: {human_size(payload['bytes'])}")
                elif kind == "copy_progress":
                    self.set_status("Kopyalama")
                    self.progress_var.set(payload["pct"])
                    self.progress_text_var.set(f"İlerleme: {payload['pct']}%")
                    elapsed = max(0.1, time.time() - self.started_ts) if self.started_ts else 0.1
                    speed = payload['bytes'] / elapsed
                    total_bytes = self.worker.stats.total_bytes if self.worker else payload['bytes']
                    remaining = max(0, total_bytes - payload['bytes'])
                    eta = remaining / speed if speed > 0 else 0
                    self.copy_var.set(f"%{payload['pct']}\n{human_size(speed)}/sn\nKalan: {self.format_eta(eta)}")
                elif kind == "done":
                    stats: JobStats = payload
                    if stats.status == "Tamamlandı":
                        self.title("✔ USB Recovery Completed")
                        self.header_title_var.set("✔ USB Recovery Completed")
                        self.header_sub_var.set(f"{stats.copied_files} dosya • {human_size(stats.copied_bytes)} • Rapor oluşturuldu")
                        try:
                            self.progress.configure(style="Success.Horizontal.TProgressbar")
                        except Exception:
                            pass
                    elif stats.status == "Durduruldu":
                        self.title(f"{APP_NAME} {APP_VERSION} - Durduruldu")
                        self.header_title_var.set("■ İşlem Durduruldu")
                        self.header_sub_var.set("Kopyalama kullanıcı tarafından durduruldu")
                    else:
                        self.title(f"{APP_NAME} {APP_VERSION}")
                        self.header_title_var.set("◆ USB Recovery Pro")
                        self.header_sub_var.set(f"{APP_VERSION} • Professional Edition • Safe Copy Engine")
                    self.set_status("✔ Tamamlandı" if stats.status == "Tamamlandı" else stats.status)
                    self.progress_var.set(100 if stats.status == "Tamamlandı" else self.progress_var.get())
                    self.progress_text_var.set("İlerleme: 100%" if stats.status == "Tamamlandı" else f"İlerleme: {self.progress_var.get()}%")
                    self.copy_var.set(f"{stats.copied_files} kopyalandı • {stats.failed_files} hata")
                    self.started_ts = None
                    self.set_start_enabled(bool(self.drives))
                    self.log(f"Bitti: {stats.status} | Kopyalanan: {stats.copied_files} | Hata: {stats.failed_files}")
                    if stats.status == "Tamamlandı":
                        elapsed = "--:--"
                        try:
                            if stats.started_at and stats.finished_at:
                                a = datetime.strptime(stats.started_at, "%Y-%m-%d %H:%M:%S")
                                b = datetime.strptime(stats.finished_at, "%Y-%m-%d %H:%M:%S")
                                elapsed = self.format_eta((b-a).total_seconds())
                        except Exception:
                            pass
                        self.copy_var.set(f"✔ Tamamlandı\n{stats.copied_files} dosya\nSüre: {elapsed}")
                        self.log("✔ KURTARMA BAŞARIYLA TAMAMLANDI")
                        self.log(f"Özet: {stats.copied_files} dosya • {human_size(stats.copied_bytes)} • Hata: {stats.failed_files}")
                    if stats.status == "Tamamlandı":
                        messagebox.showinfo("Tamamlandı", f"Kopyalama tamamlandı.\nKopyalanan: {stats.copied_files}\nBoyut: {human_size(stats.copied_bytes)}")
        except queue.Empty:
            pass
        self.after(120, self.consume_events)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
