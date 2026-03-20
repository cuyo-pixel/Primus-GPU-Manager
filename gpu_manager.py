#!/usr/bin/env python3
"""
GPU Manager — per-app GPU profile manager for Linux with Optimus/PRIME.
Scans .desktop files and lets the user assign Intel (iGPU) or NVIDIA (dGPU)
to each application by prepending the appropriate launch wrapper.

Requirements:
  - Python 3.10+
  - GTK 4 + Libadwaita  (apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1)
  - primusrun OR prime-run in PATH (provided by nvidia-primus-vk-wrapper or nvidia-prime)
"""

import gi
import os
import sys
import subprocess
import configparser
import shutil
from pathlib import Path

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, GdkPixbuf, Gdk

# ─── Constants ────────────────────────────────────────────────────────────────

APP_ID = "io.github.primus-gpu-manager"
APP_NAME = "Primus GPU Manager"
APP_VERSION = "1.0.0"

# Directories to scan for .desktop files (user-local takes priority).
# In Flatpak, system dirs are mounted under /run/host by the sandbox.
def _get_desktop_dirs() -> list[Path]:
    """Return the list of directories to scan, accounting for Flatpak and AppImage.

    Priority order:
      1. User-local (~/.local/share/applications) — always first
      2. Flatpak user exports
      3. System XDG_DATA_DIRS entries (covers AppImage PATH expansion)
      4. Flatpak system exports
    """
    dirs: list[Path] = []

    # 1. User-local always wins
    dirs.append(Path.home() / ".local/share/applications")

    # 2. User Flatpak exports
    dirs.append(Path.home() / ".local/share/flatpak/exports/share/applications")

    if os.path.exists("/.flatpak-info"):
        # Inside Flatpak sandbox: host is mounted at /run/host
        dirs += [
            Path("/run/host/usr/share/applications"),
            Path("/run/host/usr/local/share/applications"),
            Path("/run/host/var/lib/flatpak/exports/share/applications"),
        ]
    else:
        # Normal install or AppImage: read XDG_DATA_DIRS (AppRun sets it)
        xdg_dirs = os.environ.get(
            "XDG_DATA_DIRS", "/usr/local/share:/usr/share"
        ).split(":")
        for d in xdg_dirs:
            p = Path(d) / "applications"
            if p not in dirs:
                dirs.append(p)
        # System Flatpak exports
        dirs.append(Path("/var/lib/flatpak/exports/share/applications"))

    return dirs

# User-local directory where we write modified .desktop files
USER_APP_DIR = Path.home() / ".local/share/applications"

# Supported PRIME/Optimus wrappers, checked in order of preference
NVIDIA_WRAPPERS = ["primusrun", "prime-run"]

# Environment variable approach as fallback (no wrapper needed)
NVIDIA_ENV_PREFIX = "__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia"

# Marker comment we inject so we can detect our own edits
OUR_MARKER = "# gpu-manager: nvidia"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_flatpak() -> bool:
    """Return True if we are running inside a Flatpak sandbox."""
    return os.path.exists("/.flatpak-info")


def find_nvidia_wrapper() -> str | None:
    """Return the first available NVIDIA launch wrapper, or None.

    - Outside sandbox: searches the normal PATH plus common system dirs.
    - Inside Flatpak: checks /run/host/usr/bin where the host OS is mounted,
      and verifies the wrapper is reachable via flatpak-spawn --host.
    - Inside AppImage: ensures common system paths are always in PATH.
    """
    if is_flatpak():
        # Flatpak mounts the host filesystem under /run/host
        host_dirs = ["/run/host/usr/local/bin", "/run/host/usr/bin", "/run/host/bin"]
        for wrapper in NVIDIA_WRAPPERS:
            for d in host_dirs:
                if os.path.isfile(os.path.join(d, wrapper)):
                    return wrapper
        return None

    # AppImage / normal install: ensure common paths are always searched
    extra_paths = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/games"]
    for p in extra_paths:
        if p not in os.environ.get("PATH", ""):
            os.environ["PATH"] = f"{p}:{os.environ.get('PATH', '')}"

    for wrapper in NVIDIA_WRAPPERS:
        if shutil.which(wrapper):
            return wrapper
    return None


def build_exec_prefix(wrapper: str | None) -> str:
    """Return the correct exec prefix for launching apps with NVIDIA.

    Inside Flatpak we must escape the sandbox via flatpak-spawn --host
    so the wrapper actually runs on the host where the GPU drivers live.
    """
    if is_flatpak() and wrapper:
        return f"flatpak-spawn --host {wrapper}"
    if wrapper:
        return wrapper
    return NVIDIA_ENV_PREFIX


def get_app_icon(icon_name: str, size: int = 32) -> Gtk.Image:
    """Resolve an icon name or path to a Gtk.Image widget."""
    image = Gtk.Image()
    image.set_pixel_size(size)

    if not icon_name:
        image.set_from_icon_name("application-x-executable")
        return image

    # Absolute path to an image file
    if os.path.isabs(icon_name) and os.path.exists(icon_name):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(icon_name, size, size)
            image.set_from_pixbuf(pixbuf)
            return image
        except Exception:
            pass

    # Named icon from the theme
    image.set_from_icon_name(icon_name)
    return image


def load_desktop_files() -> list[dict]:
    """
    Scan desktop directories for .desktop files.
    Returns a deduplicated list of app dicts, user-local files taking priority.
    Also includes Flatpak and AppImage exported .desktop entries.
    """
    seen: dict[str, dict] = {}  # basename → app dict

    for directory in _get_desktop_dirs():
        if not directory.exists():
            continue
        for desktop_file in sorted(directory.glob("*.desktop")):
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(desktop_file, encoding="utf-8")
            except Exception:
                continue

            if "Desktop Entry" not in parser:
                continue

            entry = parser["Desktop Entry"]

            # Skip non-application entries and hidden ones
            entry_type = entry.get("Type", "")
            if entry_type != "Application":
                continue
            if entry.get("NoDisplay", "false").lower() == "true":
                continue
            if entry.get("Hidden", "false").lower() == "true":
                continue

            name = entry.get("Name", desktop_file.stem)
            exec_line = entry.get("Exec", "")
            icon = entry.get("Icon", "")
            comment = entry.get("Comment", "")

            # Detect if we've already set this app to use NVIDIA
            gpu_mode = "nvidia" if OUR_MARKER in desktop_file.read_text(encoding="utf-8") else "intel"

            app = {
                "name": name,
                "exec": exec_line,
                "icon": icon,
                "comment": comment,
                "desktop_file": desktop_file,
                "basename": desktop_file.name,
                "gpu_mode": gpu_mode,
            }

            # User-local files always win
            if desktop_file.parent == USER_APP_DIR:
                seen[desktop_file.name] = app
            elif desktop_file.name not in seen:
                seen[desktop_file.name] = app

    apps = sorted(seen.values(), key=lambda a: a["name"].lower())
    return apps


def set_app_gpu(app: dict, mode: str, wrapper: str | None) -> bool:
    """
    Rewrite (or create) the user-local .desktop file for `app`,
    setting the Exec line to use `wrapper` (nvidia) or stripping it (intel).

    Returns True on success, False on error.
    """
    USER_APP_DIR.mkdir(parents=True, exist_ok=True)
    dest = USER_APP_DIR / app["basename"]

    # Read the original file (could be system-wide)
    src = app["desktop_file"]
    try:
        content = src.read_text(encoding="utf-8")
    except Exception:
        return False

    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str  # preserve key casing
    try:
        parser.read_string(content)
    except Exception:
        return False

    if "Desktop Entry" not in parser:
        return False

    original_exec = parser["Desktop Entry"].get("Exec", "")

    # Strip any previous gpu-manager prefix from Exec
    clean_exec = original_exec
    for w in NVIDIA_WRAPPERS:
        if clean_exec.startswith(w + " "):
            clean_exec = clean_exec[len(w) + 1:]
    if clean_exec.startswith(NVIDIA_ENV_PREFIX + " "):
        clean_exec = clean_exec[len(NVIDIA_ENV_PREFIX) + 1:]

    if mode == "nvidia":
        prefix = build_exec_prefix(wrapper)
        new_exec = f"{prefix} {clean_exec}"
    else:
        new_exec = clean_exec

    parser["Desktop Entry"]["Exec"] = new_exec

    # Build output, injecting/removing our marker comment before [Desktop Entry]
    lines = []
    for line in content.splitlines():
        if line.strip() == OUR_MARKER:
            continue  # remove old marker
        lines.append(line)

    output = "\n".join(lines)

    # Re-serialise only the [Desktop Entry] section's Exec key change
    # (configparser would mangle comments, so we do a targeted line replace)
    new_lines = []
    in_desktop_entry = False
    exec_replaced = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped == "[Desktop Entry]":
            in_desktop_entry = True
            if mode == "nvidia":
                new_lines.append(OUR_MARKER)
            new_lines.append(line)
            continue
        if stripped.startswith("[") and stripped != "[Desktop Entry]":
            in_desktop_entry = False
        if in_desktop_entry and stripped.startswith("Exec=") and not exec_replaced:
            new_lines.append(f"Exec={new_exec}")
            exec_replaced = True
            continue
        new_lines.append(line)

    try:
        dest.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        # Make executable
        dest.chmod(dest.stat().st_mode | 0o111)
    except Exception:
        return False

    # Update the desktop database so the launcher picks up changes
    subprocess.run(
        ["update-desktop-database", str(USER_APP_DIR)],
        capture_output=True,
    )

    return True


# ─── App Row Widget ───────────────────────────────────────────────────────────

class AppRow(Adw.ActionRow):
    """A single application row with a GPU toggle switch."""

    def __init__(self, app: dict, wrapper: str | None, on_change_cb):
        super().__init__()

        self._app = app
        self._wrapper = wrapper
        self._on_change_cb = on_change_cb
        self._updating = False

        # Title and subtitle
        self.set_title(app["name"])
        if app["comment"]:
            self.set_subtitle(app["comment"])
        else:
            # Show a shortened exec as subtitle
            exec_short = app["exec"].split()[0] if app["exec"] else ""
            self.set_subtitle(exec_short)

        # Icon
        icon_widget = get_app_icon(app["icon"], 32)
        self.add_prefix(icon_widget)

        # GPU label
        self._gpu_label = Gtk.Label()
        self._gpu_label.set_valign(Gtk.Align.CENTER)
        self._gpu_label.add_css_class("caption")
        self._gpu_label.set_margin_end(8)

        # Toggle switch
        self._switch = Gtk.Switch()
        self._switch.set_valign(Gtk.Align.CENTER)
        self._switch.connect("notify::active", self._on_switch_toggled)

        self.add_suffix(self._gpu_label)
        self.add_suffix(self._switch)

        # Set initial state
        self._set_display(app["gpu_mode"])

    def _set_display(self, mode: str):
        """Update label and switch appearance to reflect current mode."""
        self._updating = True
        if mode == "nvidia":
            self._gpu_label.set_text("NVIDIA")
            self._gpu_label.add_css_class("accent")
            self._gpu_label.remove_css_class("dim-label")
            self._switch.set_active(True)
        else:
            self._gpu_label.set_text("Integrated")
            self._gpu_label.remove_css_class("accent")
            self._gpu_label.add_css_class("dim-label")
            self._switch.set_active(False)
        self._updating = False

    def _on_switch_toggled(self, switch, _param):
        if self._updating:
            return
        mode = "nvidia" if switch.get_active() else "intel"
        success = set_app_gpu(self._app, mode, self._wrapper)
        if success:
            self._app["gpu_mode"] = mode
            self._set_display(mode)
            self._on_change_cb(self._app["name"], mode, success=True)
        else:
            # Revert the switch on failure
            self._set_display(self._app["gpu_mode"])
            self._on_change_cb(self._app["name"], mode, success=False)


# ─── Main Window ─────────────────────────────────────────────────────────────

class GPUManagerWindow(Adw.ApplicationWindow):
    """Main application window."""

    def __init__(self, app: Adw.Application, wrapper: str | None):
        super().__init__(application=app)

        self._wrapper = wrapper
        self._all_apps: list[dict] = []

        self.set_title(APP_NAME)
        self.set_default_size(680, 620)

        # ── Outer layout ──
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # ── Header bar ──
        header = Adw.HeaderBar()
        header.set_centering_policy(Adw.CenteringPolicy.STRICT)
        toolbar_view.add_top_bar(header)

        # Search button
        self._search_btn = Gtk.ToggleButton()
        self._search_btn.set_icon_name("system-search-symbolic")
        self._search_btn.connect("toggled", self._on_search_toggled)
        header.pack_end(self._search_btn)

        # Refresh button
        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Reload applications")
        refresh_btn.connect("clicked", self._on_refresh)
        header.pack_start(refresh_btn)

        # ── Main content box ──
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar_view.set_content(main_box)

        # ── Search bar ──
        self._search_bar = Gtk.SearchBar()
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_bar.set_child(self._search_entry)
        self._search_bar.connect_entry(self._search_entry)
        main_box.append(self._search_bar)

        # ── Banner (wrapper info) ──
        if wrapper:
            banner_text = f"Using {wrapper} as NVIDIA launcher"
        else:
            banner_text = "No wrapper found — using environment variables as fallback"

        self._banner = Adw.Banner()
        self._banner.set_title(banner_text)
        self._banner.set_revealed(True)
        main_box.append(self._banner)

        # ── Scroll + list ──
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        main_box.append(scroll)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(760)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(16)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scroll.set_child(clamp)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(content_box)
        self._content_box = content_box  # keep ref for dynamic reload

        # Stats row
        self._stats_label = Gtk.Label()
        self._stats_label.add_css_class("caption")
        self._stats_label.add_css_class("dim-label")
        self._stats_label.set_halign(Gtk.Align.START)
        content_box.append(self._stats_label)

        # App list group — created fresh by _load_apps(), just reserve the slot
        self._list_group = Adw.PreferencesGroup()
        content_box.append(self._list_group)

        # Empty state
        self._empty_state = Adw.StatusPage()
        self._empty_state.set_icon_name("system-search-symbolic")
        self._empty_state.set_title("No results")
        self._empty_state.set_description("Try a different search term")
        self._empty_state.set_visible(False)
        content_box.append(self._empty_state)

        # ── Toast overlay for notifications ──
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(main_box)
        # Re-wrap: ToastOverlay must be the toolbar content
        toolbar_view.set_content(self._toast_overlay)
        self._toast_overlay.set_child(main_box)

        # ── Load apps ──
        self._load_apps()

    # ── Loading ────────────────────────────────────────────────────────────

    def _load_apps(self):
        """Load all .desktop apps and populate the list."""
        # Remove the old group and create a fresh one to avoid duplication.
        # Iterating Adw.PreferencesGroup children is unreliable because the
        # widget contains internal non-row children (header, description box).
        if hasattr(self, "_list_group") and self._list_group.get_parent():
            parent = self._list_group.get_parent()
            parent.remove(self._list_group)

        self._list_group = Adw.PreferencesGroup()
        self._list_group.set_title("Applications")
        self._list_group.set_description(
            "Toggle to run an app with the NVIDIA GPU. Integrated graphics is used by default."
        )
        # Re-insert before the empty-state widget
        self._content_box.insert_child_after(
            self._list_group, self._stats_label
        )

        self._all_apps = load_desktop_files()
        self._rows: list[AppRow] = []

        for app in self._all_apps:
            row = AppRow(app, self._wrapper, self._on_row_change)
            self._list_group.add(row)
            self._rows.append(row)

        self._update_stats()

    def _update_stats(self):
        nvidia_count = sum(1 for a in self._all_apps if a["gpu_mode"] == "nvidia")
        total = len(self._all_apps)
        self._stats_label.set_text(
            f"{total} apps found · {nvidia_count} running on NVIDIA"
        )

    # ── Search ─────────────────────────────────────────────────────────────

    def _on_search_toggled(self, btn):
        self._search_bar.set_search_mode(btn.get_active())
        if btn.get_active():
            self._search_entry.grab_focus()

    def _on_search_changed(self, entry):
        query = entry.get_text().lower().strip()
        visible_count = 0

        for row in self._rows:
            app = row._app
            matches = (
                query in app["name"].lower()
                or query in app["comment"].lower()
                or query in app["exec"].lower()
            )
            row.set_visible(matches)
            if matches:
                visible_count += 1

        self._empty_state.set_visible(visible_count == 0 and bool(query))
        self._list_group.set_visible(visible_count > 0)

    # ── Row change callback ─────────────────────────────────────────────────

    def _on_row_change(self, app_name: str, mode: str, success: bool):
        if success:
            label = "NVIDIA 🟢" if mode == "nvidia" else "Integrated 🔵"
            toast = Adw.Toast.new(f"{app_name} → {label}")
            toast.set_timeout(2)
        else:
            toast = Adw.Toast.new(f"❌ Failed to update {app_name}")
            toast.set_timeout(3)
        self._toast_overlay.add_toast(toast)
        self._update_stats()

    # ── Refresh ────────────────────────────────────────────────────────────

    def _on_refresh(self, _btn):
        self._load_apps()
        toast = Adw.Toast.new("Applications reloaded")
        toast.set_timeout(1)
        self._toast_overlay.add_toast(toast)


# ─── No-wrapper dialog ────────────────────────────────────────────────────────

def show_no_wrapper_dialog(app: Adw.Application):
    """Show an error dialog when no NVIDIA wrapper is found and exit."""
    win = Adw.ApplicationWindow(application=app)
    win.set_title(APP_NAME)
    win.set_default_size(440, 300)

    page = Adw.StatusPage()
    page.set_icon_name("dialog-warning-symbolic")
    page.set_title("NVIDIA wrapper not found")
    page.set_description(
        "GPU Manager requires <b>primusrun</b> or <b>prime-run</b> to launch apps "
        "on the NVIDIA GPU.\n\n"
        "Install one of the following and try again:\n"
        "<tt>sudo apt install nvidia-primus-vk-wrapper</tt>\n"
        "<tt>sudo apt install nvidia-prime</tt>"
    )

    btn = Gtk.Button(label="Close")
    btn.add_css_class("pill")
    btn.add_css_class("destructive-action")
    btn.connect("clicked", lambda _: app.quit())

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    box.set_halign(Gtk.Align.CENTER)
    box.set_valign(Gtk.Align.CENTER)
    box.append(page)
    box.append(btn)

    win.set_content(box)
    win.present()


# ─── Application ─────────────────────────────────────────────────────────────

class GPUManagerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self._wrapper = find_nvidia_wrapper()

    def do_activate(self):
        win = self.get_active_window()
        if win:
            win.present()
            return

        if self._wrapper is None:
            # Show warning but still allow use with env-var fallback
            print(
                "[gpu-manager] WARNING: No primusrun/prime-run found. "
                "Falling back to environment variables.",
                file=sys.stderr,
            )

        window = GPUManagerWindow(self, self._wrapper)
        window.present()


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = GPUManagerApp()
    sys.exit(app.run(sys.argv))
