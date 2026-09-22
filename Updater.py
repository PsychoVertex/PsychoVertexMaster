import ast
import json
import queue
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty

from . import Preferences


API_URL = "https://api.github.com/repos/PsychoVertex/PsychoVertexMaster/releases/latest"
ADDON_DIR = Path(__file__).resolve().parent
PRESERVED_ROOTS = {".git", "backups", "denoisers", "__pycache__"}
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024

_registered = False
_checking = False
_results = queue.Queue()


def _version_tuple(value):
    text = str(value).strip().lstrip("vV")
    parts = text.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"Unsupported release version: {value}")
    return tuple(int(part) for part in parts)


def _current_version():
    package = __import__(__package__, fromlist=["bl_info"])
    return tuple(package.bl_info["version"])


def _version_text(version):
    return ".".join(str(part) for part in version)


def _request_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "PsychoVertexMaster-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _latest_release():
    data = _request_json(API_URL)
    version = _version_tuple(data.get("tag_name", ""))
    download_url = data.get("zipball_url", "")
    if not download_url:
        raise ValueError("The latest GitHub release has no source archive")
    return {
        "version": version,
        "version_text": _version_text(version),
        "download_url": download_url,
        "release_url": data.get("html_url", ""),
    }


def _with_window(window, callback):
    windows = bpy.context.window_manager.windows
    try:
        window_pointer = window.as_pointer() if window is not None else 0
        window_is_open = any(item.as_pointer() == window_pointer for item in windows)
    except (AttributeError, ReferenceError):
        window_is_open = False
    if not window_is_open or not hasattr(bpy.context, "temp_override"):
        return callback()
    with bpy.context.temp_override(window=window):
        return callback()


def _show_message(title, message, icon="INFO", window=None):
    def draw(self, _context):
        self.layout.label(text=message, icon=icon)
    return _with_window(
        window,
        lambda: bpy.context.window_manager.popup_menu(draw, title=title, icon=icon),
    )


def _deliver_result(result, manual, window):
    global _checking
    _checking = False
    if not _registered:
        return None
    if isinstance(result, Exception):
        if manual:
            _show_message("Update Check Failed", str(result), "ERROR", window)
        return None
    if result["version"] <= _current_version():
        if manual:
            _show_message(
                "PsychoVertexMaster",
                f"Version {_version_text(_current_version())} is up to date.",
                window=window,
            )
        return None
    prefs = Preferences.get()
    if not manual and prefs:
        if not prefs.auto_update_alerts or prefs.ignored_update_version == result["version_text"]:
            return None
    if not bpy.context.window_manager.windows:
        return False
    _with_window(
        window,
        lambda: bpy.ops.pvm.update_available(
            "INVOKE_DEFAULT",
            version=result["version_text"],
            download_url=result["download_url"],
            release_url=result["release_url"],
        ),
    )
    return True


def _poll_results():
    if not _registered:
        return None
    try:
        result, manual, window = _results.get_nowait()
    except queue.Empty:
        return 0.5
    if _deliver_result(result, manual, window) is False:
        _results.put((result, manual, window))
    return 0.5


def check_for_updates(manual=False, window=None):
    global _checking
    if _checking:
        if manual:
            _show_message(
                "PsychoVertexMaster", "An update check is already running.", window=window)
        return
    _checking = True

    def worker():
        try:
            result = _latest_release()
        except Exception as exc:
            result = exc
        _results.put((result, manual, window))

    threading.Thread(target=worker, name="PVM update check", daemon=True).start()


def _startup_check():
    if not _registered:
        return None
    prefs = Preferences.get()
    if prefs and prefs.auto_update_alerts:
        check_for_updates(manual=False, window=bpy.context.window)
    return None


def _download_archive(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": "PsychoVertexMaster-Updater"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_ARCHIVE_BYTES:
            raise ValueError("Update archive is larger than the allowed 200 MB")
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("Update archive is larger than the allowed 200 MB")
            output.write(chunk)


def _extract_archive(archive, destination):
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        if sum(member.file_size for member in package.infolist()) > MAX_ARCHIVE_BYTES * 4:
            raise ValueError("Expanded update archive is larger than the allowed 800 MB")
        for member in package.infolist():
            target = (root / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError("Update archive contains an unsafe path")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Update archive contains an unsupported symbolic link")
        package.extractall(destination)
    candidates = [path.parent for path in destination.glob("*/__init__.py")]
    if len(candidates) != 1:
        raise ValueError("Update archive does not contain one add-on root")
    return candidates[0]


def _archive_version(source_root):
    tree = ast.parse((source_root / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "bl_info" for target in node.targets):
                info = ast.literal_eval(node.value)
                return tuple(info["version"])
    raise ValueError("Update archive has no valid bl_info version")


def _source_files(source_root):
    for source in source_root.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        if relative.parts[0] in PRESERVED_ROOTS or source.suffix == ".pyc":
            continue
        yield source, relative


def _install_transactionally(source_root):
    with tempfile.TemporaryDirectory(prefix="pvm-rollback-") as rollback_dir:
        rollback_root = Path(rollback_dir)
        restored = []
        created = []
        temporary_files = []
        try:
            for source, relative in _source_files(source_root):
                destination = ADDON_DIR / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    backup = rollback_root / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup)
                    restored.append((backup, destination))
                else:
                    created.append(destination)
                temporary = destination.with_name(destination.name + ".pvm-update")
                temporary_files.append(temporary)
                shutil.copy2(source, temporary)
                temporary.replace(destination)
        except Exception:
            for destination in reversed(created):
                destination.unlink(missing_ok=True)
            for backup, destination in reversed(restored):
                shutil.copy2(backup, destination)
            raise
        finally:
            for temporary in temporary_files:
                temporary.unlink(missing_ok=True)


def install_update(download_url, version_text):
    expected = _version_tuple(version_text)
    with tempfile.TemporaryDirectory(prefix="pvm-update-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        archive = temporary_root / "update.zip"
        _download_archive(download_url, archive)
        source_root = _extract_archive(archive, temporary_root / "source")
        archive_version = _archive_version(source_root)
        if archive_version != expected:
            raise ValueError(
                f"Release tag {version_text} does not match archive version "
                f"{_version_text(archive_version)}"
            )
        if archive_version <= _current_version():
            raise ValueError("The downloaded release is not newer than this installation")
        _install_transactionally(source_root)


class PVM_OT_CheckForUpdates(bpy.types.Operator):
    bl_idname = "pvm.check_for_updates"
    bl_label = "Check for Updates"
    bl_description = "Check the latest stable PsychoVertexMaster GitHub release"

    def execute(self, context):
        check_for_updates(manual=True, window=context.window)
        self.report({"INFO"}, "Checking for updates in the background")
        return {"FINISHED"}


class PVM_OT_UpdateAvailable(bpy.types.Operator):
    bl_idname = "pvm.update_available"
    bl_label = "PsychoVertexMaster Update Available"

    version: StringProperty(options={"HIDDEN"})
    download_url: StringProperty(options={"HIDDEN"})
    release_url: StringProperty(options={"HIDDEN"})
    dont_show_again: BoolProperty(name="Don't show again for this update", default=False)

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.label(text=f"PsychoVertexMaster {self.version} is available.", icon="IMPORT")
        layout.label(text=f"Installed version: {_version_text(_current_version())}")
        layout.prop(self, "dont_show_again")
        row = layout.row(align=True)
        update = row.operator("pvm.install_update", text="Update", icon="IMPORT")
        update.version = self.version
        update.download_url = self.download_url
        cancel = row.operator("pvm.dismiss_update", text="Cancel", icon="X")
        cancel.version = self.version
        cancel.dont_show_again = self.dont_show_again

    def execute(self, context):
        return {"FINISHED"}


class PVM_OT_DismissUpdate(bpy.types.Operator):
    bl_idname = "pvm.dismiss_update"
    bl_label = "Cancel Update"

    version: StringProperty(options={"HIDDEN"})
    dont_show_again: BoolProperty(options={"HIDDEN"})

    def execute(self, context):
        prefs = Preferences.get()
        if prefs and self.dont_show_again:
            prefs.ignored_update_version = self.version
        return {"FINISHED"}


class PVM_OT_InstallUpdate(bpy.types.Operator):
    bl_idname = "pvm.install_update"
    bl_label = "Update PsychoVertexMaster"

    version: StringProperty(options={"HIDDEN"})
    download_url: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        try:
            install_update(self.download_url, self.version)
        except Exception as exc:
            self.report({"ERROR"}, f"Update failed: {exc}")
            return {"CANCELLED"}
        prefs = Preferences.get()
        if prefs:
            prefs.ignored_update_version = ""
        _show_message(
            "Update Installed",
            f"PsychoVertexMaster {self.version} was installed. Restart Blender to load it.",
            "CHECKMARK",
        )
        return {"FINISHED"}


classes = (
    PVM_OT_CheckForUpdates,
    PVM_OT_UpdateAvailable,
    PVM_OT_DismissUpdate,
    PVM_OT_InstallUpdate,
)


def register():
    global _registered
    registered = []
    try:
        while not _results.empty():
            _results.get_nowait()
        for cls in classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
        _registered = True
        bpy.app.timers.register(_poll_results, first_interval=0.5)
        bpy.app.timers.register(_startup_check, first_interval=5.0)
    except Exception:
        _registered = False
        if bpy.app.timers.is_registered(_startup_check):
            bpy.app.timers.unregister(_startup_check)
        if bpy.app.timers.is_registered(_poll_results):
            bpy.app.timers.unregister(_poll_results)
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except (RuntimeError, ValueError):
                pass
        raise


def unregister():
    global _registered, _checking
    _registered = False
    _checking = False
    if bpy.app.timers.is_registered(_startup_check):
        bpy.app.timers.unregister(_startup_check)
    if bpy.app.timers.is_registered(_poll_results):
        bpy.app.timers.unregister(_poll_results)
    while not _results.empty():
        try:
            _results.get_nowait()
        except queue.Empty:
            break
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
