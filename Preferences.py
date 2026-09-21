import json
import math
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Literal, cast

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences
from bpy_extras.io_utils import ExportHelper, ImportHelper


ModeTypes = Literal["PIE_MENUS", "NORMAL_MENUS"]
MAX_CONFIG_ITEMS = 64
ADDON_DIR = Path(__file__).resolve().parent
DENOISER_DIR = ADDON_DIR / "denoisers"
OIDN_DIR = DENOISER_DIR / "oidn-2.5.1.x64.windows"
OIDN_EXECUTABLE = OIDN_DIR / "bin" / "oidnDenoise.exe"
OPTIX_EXECUTABLE = DENOISER_DIR / "Denoiser.exe"
OIDN_URL = "https://github.com/RenderKit/oidn/releases/download/v2.5.1/oidn-2.5.1.x64.windows.zip"
OPTIX_URL = "https://github.com/DeclanRussell/NvidiaAIDenoiser/releases/download/3.0/Denoiser.exe"


def _model():
    from .HandyMenu import model
    return model


def _handy_menu():
    from . import HandyMenu
    return HandyMenu


def _default_json():
    return _model().dumps(_model().clone_default())


def _action_items(_self=None, _context=None):
    menu = _handy_menu()
    actions = [(key, value["label"], key) for key, value in menu.ACTION_CATALOG.items()]
    actions += [(key, value[0], key) for key, value in menu.PROPERTY_ACTIONS.items()]
    return sorted(actions, key=lambda item: item[1].lower())


_syncing_menu_ui = False
_icon_identifiers = None


def _available_icons():
    global _icon_identifiers
    if _icon_identifiers is not None:
        return _icon_identifiers
    try:
        icons = bpy.types.UILayout.bl_rna.functions["prop"].parameters["icon"].enum_items
        _icon_identifiers = [icon for icon in icons.keys() if icon != "NONE"]
    except Exception:
        _icon_identifiers = []
    return _icon_identifiers


def _inline_update(index, field):
    def update(prefs, _context):
        if _syncing_menu_ui:
            return
        config = get_menu_config(prefs)
        menu, _parents = _current_menu(prefs, config)
        if index >= len(menu["items"]):
            return
        item = menu["items"][index]
        value = getattr(prefs, f"menu_item_{field}_{index}")
        if field in {"label", "icon"}:
            if item["type"] != "separator":
                item[field] = value
                if field == "label" and item["type"] == "submenu":
                    item["menu"]["label"] = value or "Submenu"
        elif field == "action" and item["type"] == "plugin_action":
            item["action"] = value
        elif field == "command" and item["type"] == "custom_operator":
            try:
                operator_id, properties = _model().parse_operator_command(value)
            except _model().MenuConfigError:
                return
            item["operator_id"], item["properties"] = operator_id, properties
        elif field == "path" and item["type"] == "custom_property":
            try:
                item["property_path"] = _model().parse_property_path(value)
            except _model().MenuConfigError:
                return
        prefs.menu_config = _model().dumps(config)
    return update


class PV_Preferences(AddonPreferences):
    bl_idname = str(__package__)

    mode: EnumProperty(name="Mode", items=[
        ("PIE_MENUS", "Pie Menus", ""),
        ("NORMAL_MENUS", "Normal Menus", ""),
    ], default="PIE_MENUS")
    oidn_path: StringProperty(name="OIDN", subtype="FILE_PATH", default="")
    optix_path: StringProperty(name="OptiX", subtype="FILE_PATH", default="")
    dialog_settings: StringProperty(default="{}", options={"HIDDEN"})
    menu_config: StringProperty(default="", options={"HIDDEN"})
    menu_config_recovery: StringProperty(default="", options={"HIDDEN"})
    menu_root: EnumProperty(name="Context", items=[
        ("edit", "Edit Mode", ""), ("object", "Object Mode", ""),
        ("no_active", "No Active Object", ""),
    ], default="edit")
    menu_current_id: StringProperty(default="", options={"HIDDEN"})
    menu_selected_index: IntProperty(default=0, min=0, max=7, options={"HIDDEN"})

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "mode")
        denoise = layout.box()
        denoise.label(text="Lightmap Denoising", icon="IMAGE_DATA")
        denoise.prop(self, "oidn_path")
        denoise.prop(self, "optix_path")
        downloads = denoise.row(align=True)
        oidn = downloads.operator("pvm.download_denoiser", text="Download Open Image Denoiser", icon="IMPORT")
        oidn.denoiser = "OIDN"
        optix = downloads.operator("pvm.download_denoiser", text="Download OptiX Denoiser", icon="IMPORT")
        optix.denoiser = "OPTIX"
        _draw_menu_preferences(layout, self)


for _index in range(MAX_CONFIG_ITEMS):
    PV_Preferences.__annotations__[f"menu_item_label_{_index}"] = StringProperty(
        name="", description="Menu item name", update=_inline_update(_index, "label"))
    PV_Preferences.__annotations__[f"menu_item_icon_{_index}"] = StringProperty(
        name="", description="Blender icon identifier, for example UV or EXPORT",
        update=_inline_update(_index, "icon"))
    PV_Preferences.__annotations__[f"menu_item_action_{_index}"] = EnumProperty(
        name="", description="Built-in PsychoVertexMaster action", items=_action_items,
        update=_inline_update(_index, "action"))
    PV_Preferences.__annotations__[f"menu_item_command_{_index}"] = StringProperty(
        name="", description="Safe Blender operator command",
        update=_inline_update(_index, "command"))
    PV_Preferences.__annotations__[f"menu_item_path_{_index}"] = StringProperty(
        name="", description="Safe property path, for example context.object.display_type",
        update=_inline_update(_index, "path"))


def get():
    prefs = bpy.context.preferences.addons.get(str(__package__))
    return cast(PV_Preferences, prefs.preferences) if prefs else None


def get_oidn_path(prefs=None):
    prefs = prefs or get()
    return prefs.oidn_path.strip() if prefs and prefs.oidn_path.strip() else str(OIDN_EXECUTABLE)


def get_optix_path(prefs=None):
    prefs = prefs or get()
    return prefs.optix_path.strip() if prefs and prefs.optix_path.strip() else str(OPTIX_EXECUTABLE)


class PVM_OT_DownloadDenoiser(bpy.types.Operator):
    bl_idname = "pvm.download_denoiser"
    bl_label = "Download Denoiser"
    bl_description = "Download the denoiser into the PsychoVertexMaster add-on folder"

    denoiser: EnumProperty(items=[
        ("OIDN", "Open Image Denoiser", ""),
        ("OPTIX", "OptiX Denoiser", ""),
    ], options={"HIDDEN"})

    def execute(self, context):
        prefs = get()
        try:
            DENOISER_DIR.mkdir(parents=True, exist_ok=True)
            if self.denoiser == "OIDN":
                self._download_oidn()
                if prefs:
                    prefs.oidn_path = str(OIDN_EXECUTABLE)
                installed = OIDN_EXECUTABLE
            else:
                self._download(OPTIX_URL, OPTIX_EXECUTABLE)
                if prefs:
                    prefs.optix_path = str(OPTIX_EXECUTABLE)
                installed = OPTIX_EXECUTABLE
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            self.report({"ERROR"}, f"Denoiser download failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Installed {installed.name}")
        return {"FINISHED"}

    @staticmethod
    def _download(url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as response, tempfile.NamedTemporaryFile(
                dir=str(DENOISER_DIR), delete=False) as temporary:
            shutil.copyfileobj(response, temporary)
            temporary_path = Path(temporary.name)
        try:
            temporary_path.replace(destination)
        finally:
            temporary_path.unlink(missing_ok=True)

    @classmethod
    def _download_oidn(cls):
        with tempfile.TemporaryDirectory(dir=str(DENOISER_DIR)) as temporary_dir:
            archive = Path(temporary_dir) / "oidn.zip"
            cls._download(OIDN_URL, archive)
            extract_dir = Path(temporary_dir) / "extract"
            with zipfile.ZipFile(archive) as package:
                root = extract_dir.resolve()
                for member in package.infolist():
                    if root not in (root / member.filename).resolve().parents:
                        raise ValueError("OIDN archive contains an unsafe path")
                package.extractall(extract_dir)
            source = extract_dir / OIDN_DIR.name
            if not (source / "bin" / "oidnDenoise.exe").is_file():
                raise ValueError("OIDN archive does not contain oidnDenoise.exe")
            shutil.copytree(source, OIDN_DIR, dirs_exist_ok=True)


def get_mode() -> "ModeTypes | None":
    prefs = get()
    return prefs.mode if prefs else None


def get_menu_config(prefs=None):
    prefs = prefs or get()
    if not prefs or not prefs.menu_config:
        return _model().clone_default()
    try:
        return _model().loads(prefs.menu_config)
    except _model().MenuConfigError:
        return _model().clone_default()


def _store_config(prefs, config):
    prefs.menu_config = _model().dumps(config)
    _handy_menu().rebuild_dynamic_menus()


def _current_menu(prefs, config):
    root = config["roots"][prefs.menu_root]
    if not prefs.menu_current_id:
        prefs.menu_current_id = root["id"]
    menu, parents = _model().find_menu(config, prefs.menu_current_id)
    if not menu or (menu is not root and root not in parents):
        menu, parents = root, []
        prefs.menu_current_id = root["id"]
    prefs.menu_selected_index = min(prefs.menu_selected_index, max(0, len(menu["items"]) - 1))
    return menu, parents


def _command_text(item):
    properties = ", ".join(
        f"{key}={value!r}" for key, value in item.get("properties", {}).items())
    return f"bpy.ops.{item.get('operator_id', '')}({properties})"


def _sync_inline_fields(prefs, menu):
    global _syncing_menu_ui
    _syncing_menu_ui = True
    try:
        for index in range(MAX_CONFIG_ITEMS):
            item = menu["items"][index] if index < len(menu["items"]) else {}
            setattr(prefs, f"menu_item_label_{index}", item.get("label", ""))
            setattr(prefs, f"menu_item_icon_{index}", item.get("icon", ""))
            if item.get("type") == "plugin_action":
                setattr(prefs, f"menu_item_action_{index}", item.get("action", ""))
            setattr(prefs, f"menu_item_command_{index}",
                    _command_text(item) if item.get("type") == "custom_operator" else "")
            setattr(prefs, f"menu_item_path_{index}",
                    item.get("property_path", "") if item.get("type") == "custom_property" else "")
    finally:
        _syncing_menu_ui = False


def _safe_icon(identifier):
    if not identifier:
        return "NONE"
    try:
        icons = bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items
        return identifier if icons.get(identifier) else "QUESTION"
    except Exception:
        return "QUESTION"


def _draw_menu_preferences(layout, prefs):
    box = layout.box()
    header = box.row(align=True)
    header.label(text="Menu Configuration", icon="MENU_PANEL")
    header.operator("pvm.menu_import", text="", icon="IMPORT")
    header.operator("pvm.menu_export", text="", icon="EXPORT")
    header.operator("pvm.menu_reset", text="", icon="FILE_REFRESH")
    box.prop(prefs, "menu_root", expand=True)
    config = get_menu_config(prefs)
    menu, parents = _current_menu(prefs, config)
    _sync_inline_fields(prefs, menu)
    crumbs = box.row(align=True)
    chain = parents + [menu]
    for entry in chain:
        op = crumbs.operator("pvm.menu_navigate", text=entry["label"] or "Menu")
        op.menu_id = entry["id"]
    enabled_count = sum(item.get("enabled", True) for item in menu["items"])
    box.label(text=f"{enabled_count}/8 enabled slots · {len(menu['items'])} configured items")
    for index, item in enumerate(menu["items"]):
        row = box.row(align=True)
        if item["type"] == "submenu":
            nav = row.operator("pvm.menu_navigate", text="", icon="IMPORT")
            nav.menu_id = item["menu"]["id"]
        else:
            row.label(text="", icon="BLANK1")
        up = row.operator("pvm.menu_move", text="", icon="TRIA_UP"); up.index = index; up.direction = -1
        down = row.operator("pvm.menu_move", text="", icon="TRIA_DOWN"); down.index = index; down.direction = 1
        toggle = row.operator("pvm.menu_toggle", text="", icon="CHECKBOX_HLT" if item.get("enabled", True) else "CHECKBOX_DEHLT")
        toggle.index = index
        remove = row.operator("pvm.menu_remove", text="", icon="X")
        remove.index = index
        preview = row.row(align=True)
        preview.ui_units_x = 1.0
        preview.label(text="", icon=_safe_icon(item.get("icon", "")))
        if item["type"] != "separator":
            row.prop(prefs, f"menu_item_label_{index}", text="")
            row.prop(prefs, f"menu_item_icon_{index}", text="")
            picker = row.operator("pvm.menu_choose_icon", text="", icon="EYEDROPPER")
            picker.index = index
        if item["type"] == "plugin_action":
            row.prop(prefs, f"menu_item_action_{index}", text="")
        elif item["type"] == "custom_operator":
            row.prop(prefs, f"menu_item_command_{index}", text="")
        elif item["type"] == "custom_property":
            row.prop(prefs, f"menu_item_path_{index}", text="")
        elif item["type"] == "separator":
            row.label(text="Separator")
    box.operator_menu_enum("pvm.menu_add", "item_type", text="Add Item", icon="ADD")
    if prefs.menu_config_recovery:
        warning = box.box()
        warning.label(text="An invalid saved configuration was replaced with defaults.", icon="ERROR")
        warning.label(text="The original JSON remains in the hidden recovery preference.")


class PVM_OT_MenuNavigate(bpy.types.Operator):
    bl_idname = "pvm.menu_navigate"; bl_label = "Open Configured Menu"
    menu_id: StringProperty()
    def execute(self, context):
        prefs = get(); prefs.menu_current_id = self.menu_id; prefs.menu_selected_index = 0
        return {"FINISHED"}


class PVM_OT_MenuSelect(bpy.types.Operator):
    bl_idname = "pvm.menu_select"; bl_label = "Select Menu Item"
    index: IntProperty()
    def execute(self, context):
        get().menu_selected_index = self.index
        return {"FINISHED"}


def _mutate_selected(mutator):
    prefs = get(); config = get_menu_config(prefs); menu, _ = _current_menu(prefs, config)
    if not menu["items"]:
        return False
    index = min(prefs.menu_selected_index, len(menu["items"]) - 1)
    mutator(menu, index)
    _model().validate_config(config)
    _store_config(prefs, config)
    return True


class PVM_OT_MenuToggle(bpy.types.Operator):
    bl_idname = "pvm.menu_toggle"; bl_label = "Enable or Disable Item"
    index: IntProperty()
    def execute(self, context):
        prefs = get(); config = get_menu_config(prefs); menu, _ = _current_menu(prefs, config)
        if not 0 <= self.index < len(menu["items"]):
            return {"CANCELLED"}
        item = menu["items"][self.index]
        if not item.get("enabled", True):
            enabled_count = sum(entry.get("enabled", True) for entry in menu["items"])
            if enabled_count >= 8:
                self.report({"WARNING"}, "Menus are limited to 8 enabled slots; disable another item first")
                return {"CANCELLED"}
        item["enabled"] = not item.get("enabled", True)
        _store_config(prefs, config)
        return {"FINISHED"}


class PVM_OT_MenuMove(bpy.types.Operator):
    bl_idname = "pvm.menu_move"; bl_label = "Move Menu Item"
    index: IntProperty(); direction: IntProperty()
    def execute(self, context):
        prefs = get(); config = get_menu_config(prefs); menu, _ = _current_menu(prefs, config)
        target = self.index + self.direction
        if 0 <= self.index < len(menu["items"]) and 0 <= target < len(menu["items"]):
            menu["items"][self.index], menu["items"][target] = menu["items"][target], menu["items"][self.index]
            prefs.menu_selected_index = target; _store_config(prefs, config)
        return {"FINISHED"}


class PVM_OT_MenuAdd(bpy.types.Operator):
    bl_idname = "pvm.menu_add"; bl_label = "Add Menu Item"
    item_type: EnumProperty(items=[
        ("separator", "Separator", ""), ("submenu", "Submenu", ""),
        ("plugin_action", "Plugin Action", ""), ("custom_operator", "Custom Operator", ""),
        ("custom_property", "Custom Property", ""),
    ])
    def execute(self, context):
        prefs = get(); config = get_menu_config(prefs); menu, _ = _current_menu(prefs, config)
        if len(menu["items"]) >= MAX_CONFIG_ITEMS:
            self.report({"ERROR"}, f"A menu may contain at most {MAX_CONFIG_ITEMS} configured items")
            return {"CANCELLED"}
        handy = _handy_menu(); item = {"id": handy.new_id("item"), "type": self.item_type, "enabled": True}
        if sum(entry.get("enabled", True) for entry in menu["items"]) >= 8:
            item["enabled"] = False
        if self.item_type == "submenu":
            item.update(label="New Submenu", icon="NONE", menu={"id": handy.new_id("menu"), "label": "New Submenu", "items": []})
        elif self.item_type == "plugin_action":
            item["action"] = _action_items()[0][0]
        elif self.item_type == "custom_operator":
            item.update(label="Custom Operator", icon="NONE", operator_id="wm.save_as_mainfile", properties={})
        elif self.item_type == "custom_property":
            item.update(label="Custom Property", icon="NONE",
                        property_path="context.object.display_type")
        menu["items"].append(item); prefs.menu_selected_index = len(menu["items"]) - 1; _store_config(prefs, config)
        return {"FINISHED"}


class PVM_OT_MenuRemove(bpy.types.Operator):
    bl_idname = "pvm.menu_remove"; bl_label = "Remove Menu Item"
    index: IntProperty()
    def execute(self, context):
        prefs = get(); config = get_menu_config(prefs); menu, _ = _current_menu(prefs, config)
        if 0 <= self.index < len(menu["items"]):
            menu["items"].pop(self.index)
            _store_config(prefs, config)
        return {"FINISHED"}


class PVM_OT_MenuChooseIcon(bpy.types.Operator):
    bl_idname = "pvm.menu_choose_icon"
    bl_label = "Choose Blender Icon"
    bl_description = "Search and select a Blender icon"

    index: IntProperty()
    icon_columns: IntProperty(default=24, min=1, options={"HIDDEN", "SKIP_SAVE"})
    filter: StringProperty(
        name="", description="Filter icons by name",
        options={"TEXTEDIT_UPDATE", "SKIP_SAVE"})

    def invoke(self, context, event):
        self.filter = ""
        icon_count = max(1, len(_available_icons()))
        desired_columns = max(12, round(1.4 * math.sqrt(icon_count)))
        max_columns = max(12, (context.window.width - 48) // 20)
        self.icon_columns = min(desired_columns, max_columns)
        width = min(context.window.width - 32, self.icon_columns * 20 + 32)
        return context.window_manager.invoke_props_dialog(self, width=width)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "filter", text="", icon="VIEWZOOM",
                    placeholder="Search icons...")
        needle = self.filter.strip().upper()
        icons = [icon for icon in _available_icons() if not needle or needle in icon]
        if not icons:
            layout.label(text="No icons found", icon="INFO")
            return
        columns = min(getattr(self, "icon_columns", 24), len(icons))
        grid = layout.grid_flow(row_major=True, columns=max(1, columns), even_columns=True,
                                even_rows=True, align=True)
        grid.scale_x = 0.8
        grid.scale_y = 0.8
        for icon in icons:
            select = grid.operator("pvm.menu_set_icon", text="", icon=icon)
            select.index = self.index
            select.icon_name = icon

    def check(self, context):
        return True

    def execute(self, context):
        return {"FINISHED"}


class PVM_OT_MenuSetIcon(bpy.types.Operator):
    bl_idname = "pvm.menu_set_icon"
    bl_label = "Select Icon"
    bl_description = "Use this icon for the menu item"
    bl_options = {"INTERNAL"}

    index: IntProperty()
    icon_name: StringProperty()

    def execute(self, context):
        setattr(get(), f"menu_item_icon_{self.index}", self.icon_name)
        self.report({"INFO"}, self.icon_name)
        return {"FINISHED"}


class PVM_OT_MenuEdit(bpy.types.Operator):
    bl_idname = "pvm.menu_edit"; bl_label = "Edit Menu Item"
    label: StringProperty(name="Label")
    icon: StringProperty(name="Blender Icon", description="Icon enum, for example UV or EXPORT")
    action: EnumProperty(name="Plugin Action", items=_action_items)
    command: StringProperty(name="Operator Command", description="bpy.ops.category.operator(keyword=value)")

    def invoke(self, context, event):
        prefs = get(); config = get_menu_config(prefs); menu, _ = _current_menu(prefs, config)
        if not menu["items"]: return {"CANCELLED"}
        item = menu["items"][prefs.menu_selected_index]
        self.label = item.get("label", ""); self.icon = item.get("icon", "")
        if item["type"] == "plugin_action": self.action = item["action"]
        if item["type"] == "custom_operator":
            props = ", ".join(f"{key}={value!r}" for key, value in item.get("properties", {}).items())
            self.command = f"bpy.ops.{item['operator_id']}({props})"
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        prefs = get(); config = get_menu_config(prefs); menu, _ = _current_menu(prefs, config); item = menu["items"][prefs.menu_selected_index]
        if item["type"] != "separator":
            self.layout.prop(self, "label"); self.layout.prop(self, "icon")
        if item["type"] == "plugin_action": self.layout.prop(self, "action")
        elif item["type"] == "custom_operator":
            self.layout.prop(self, "command"); self.layout.label(text="Only literal keyword values are accepted.", icon="LOCKED")

    def execute(self, context):
        prefs = get(); config = get_menu_config(prefs); menu, _ = _current_menu(prefs, config); item = menu["items"][prefs.menu_selected_index]
        if item["type"] != "separator": item["label"], item["icon"] = self.label, self.icon
        if item["type"] == "submenu": item["menu"]["label"] = self.label or "Submenu"
        elif item["type"] == "plugin_action": item["action"] = self.action
        elif item["type"] == "custom_operator":
            try: operator_id, properties = _model().parse_operator_command(self.command)
            except _model().MenuConfigError as exc:
                self.report({"ERROR"}, str(exc)); return {"CANCELLED"}
            operator = _handy_menu()._operator_exists(operator_id)
            if operator is None:
                self.report({"ERROR"}, f"Operator {operator_id} is not installed"); return {"CANCELLED"}
            try:
                valid = {prop.identifier for prop in operator.get_rna_type().properties if prop.identifier != "rna_type"}
            except Exception: valid = set(properties)
            unknown = set(properties) - valid
            if unknown:
                self.report({"ERROR"}, "Unknown operator properties: " + ", ".join(sorted(unknown))); return {"CANCELLED"}
            item["operator_id"], item["properties"] = operator_id, properties
        _store_config(prefs, config); return {"FINISHED"}


class PVM_OT_MenuReset(bpy.types.Operator):
    bl_idname = "pvm.menu_reset"; bl_label = "Reset Menu Configuration"; bl_options = {"INTERNAL"}
    def invoke(self, context, event): return context.window_manager.invoke_confirm(self, event)
    def execute(self, context):
        prefs = get(); prefs.menu_current_id = ""; prefs.menu_selected_index = 0; prefs.menu_config_recovery = ""; _store_config(prefs, _model().clone_default())
        return {"FINISHED"}


class PVM_OT_MenuImport(bpy.types.Operator, ImportHelper):
    bl_idname = "pvm.menu_import"; bl_label = "Import Menu Configuration"
    filename_ext = ".json"; filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    confirm_replace: BoolProperty(
        name="Replace current menu configuration",
        description="The imported configuration replaces all current menu roots")
    def draw(self, context):
        self.layout.prop(self, "confirm_replace")
    def execute(self, context):
        if not self.confirm_replace:
            self.report({"ERROR"}, "Confirm replacement before importing")
            return {"CANCELLED"}
        try: config = _model().loads(Path(self.filepath).read_text(encoding="utf-8"))
        except (OSError, _model().MenuConfigError) as exc:
            self.report({"ERROR"}, f"Import failed: {exc}"); return {"CANCELLED"}
        prefs = get(); prefs.menu_current_id = ""; prefs.menu_selected_index = 0; _store_config(prefs, config)
        self.report({"INFO"}, "Menu configuration imported"); return {"FINISHED"}


class PVM_OT_MenuExport(bpy.types.Operator, ExportHelper):
    bl_idname = "pvm.menu_export"; bl_label = "Export Menu Configuration"
    filename_ext = ".json"; filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    def execute(self, context):
        try: Path(self.filepath).write_text(_model().dumps(get_menu_config()), encoding="utf-8")
        except (OSError, _model().MenuConfigError) as exc:
            self.report({"ERROR"}, f"Export failed: {exc}"); return {"CANCELLED"}
        self.report({"INFO"}, "Menu configuration exported"); return {"FINISHED"}


def load_dialog_settings(operator, property_names):
    prefs = get()
    if not prefs: return
    try: settings = json.loads(prefs.dialog_settings).get(operator.bl_idname, {})
    except (TypeError, ValueError): return
    for name in property_names:
        if name in settings:
            try: setattr(operator, name, settings[name])
            except (AttributeError, TypeError, ValueError): pass


def save_dialog_settings(operator, property_names):
    prefs = get()
    if not prefs: return
    try: settings = json.loads(prefs.dialog_settings)
    except (TypeError, ValueError): settings = {}
    settings[operator.bl_idname] = {name: getattr(operator, name) for name in property_names}
    prefs.dialog_settings = json.dumps(settings, separators=(",", ":"))


classes = (PV_Preferences, PVM_OT_DownloadDenoiser,
           PVM_OT_MenuNavigate, PVM_OT_MenuSelect, PVM_OT_MenuToggle,
           PVM_OT_MenuMove, PVM_OT_MenuAdd, PVM_OT_MenuRemove, PVM_OT_MenuChooseIcon,
           PVM_OT_MenuSetIcon, PVM_OT_MenuEdit,
           PVM_OT_MenuReset, PVM_OT_MenuImport, PVM_OT_MenuExport)


def register():
    registered = []
    try:
        for cls in classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
        prefs = get()
        if prefs:
            if prefs.menu_config:
                try: _model().loads(prefs.menu_config)
                except _model().MenuConfigError:
                    prefs.menu_config_recovery = prefs.menu_config; prefs.menu_config = _default_json()
            else: prefs.menu_config = _default_json()
    except Exception:
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except (RuntimeError, ValueError):
                pass
        raise


def unregister():
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
