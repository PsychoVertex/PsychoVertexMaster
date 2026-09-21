import ast
import json
import uuid

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Context, Mesh

from .. import Preferences
from . import model


WEIGHTED_NORMALS_MODIFIER_NAME = "__Weighted_Normal__"
_dynamic_classes = []
_menu_class_ids = {}


def panel_exists(bl_idname):
    return any(getattr(cls, "bl_idname", None) == bl_idname for cls in bpy.types.Panel.__subclasses__())


class MZageHandyMenuSelectWeight(bpy.types.Operator):
    bl_idname = "mesh.mzage_select_weight"
    bl_label = "Select Weight"
    bl_options = {"REGISTER"}
    strength: StringProperty(name="Weight")

    def execute(self, context):
        bpy.ops.mesh.mod_weighted_strength(set=False, face_strength=self.strength)
        return {"FINISHED"}


class MZageHandyMenuSetWeight(bpy.types.Operator):
    bl_idname = "mesh.mzage_set_weight"
    bl_label = "Set Weight"
    bl_options = {"REGISTER", "UNDO"}
    strength: StringProperty(name="Weight")

    def execute(self, context: Context):
        selected = list(context.selected_objects)
        active = context.active_object
        if active and active not in selected:
            selected.append(active)
        bpy.ops.mesh.mod_weighted_strength(set=True, face_strength=self.strength)
        bpy.ops.object.mode_set(mode="OBJECT")
        for obj in selected:
            if obj.type != "MESH":
                continue
            mesh: Mesh = obj.data
            if WEIGHTED_NORMALS_MODIFIER_NAME not in obj.modifiers:
                sharpness = [edge.use_edge_sharp for edge in mesh.edges]
                context.view_layer.objects.active = obj
                bpy.ops.object.shade_smooth(keep_sharp_edges=True)
                bpy.ops.mesh.customdata_custom_splitnormals_add()
                for index, edge in enumerate(mesh.edges):
                    edge.use_edge_sharp = sharpness[index]
                modifier = obj.modifiers.new(WEIGHTED_NORMALS_MODIFIER_NAME, "WEIGHTED_NORMAL")
                modifier.mode = "FACE_AREA_WITH_ANGLE"
                modifier.weight = 100
                modifier.keep_sharp = True
                modifier.use_face_influence = True
                modifier.show_on_cage = True
        bpy.ops.object.mode_set(mode="EDIT")
        context.view_layer.objects.active = active
        return {"FINISHED"}


def _op(label, icon, operator_id, properties=None, available=None):
    return {"label": label, "icon": icon, "operator": operator_id,
            "properties": properties or {}, "available": available}


def _face_mode(context):
    return bool(context.tool_settings.mesh_select_mode[2])


def _edge_mode(context):
    return bool(context.tool_settings.mesh_select_mode[1])


def _coplanar_mode(context):
    mode = context.tool_settings.mesh_select_mode
    return bool(mode[2] and not mode[0] and not mode[1])


def _has_mesh(context):
    return bool(context.active_object and context.active_object.type == "MESH")


def _has_history(_context):
    return panel_exists("DATA_PT_PsychoHistory_KM")


ACTION_CATALOG = {
    "uv.clear_seam": _op("Clear Seam", "REMOVE", "mesh.mark_seam", {"clear": True}),
    "uv.mark_seam": _op("Mark Seam", "ADD", "mesh.mark_seam"),
    "uv.reset": _op("Reset UVs", "X", "uv.reset"),
    "uv.unwrap": _op("Conformal", "MOD_SHRINKWRAP", "uv.unwrap"),
    "uv.project_view": _op("View Project", "PROP_PROJECTED", "uv.project_from_view", {"scale_to_bounds": False}),
    "mesh.clear_sharp": _op("Clear Sharp", "REMOVE", "mesh.mark_sharp", {"clear": True}),
    "mesh.mark_sharp": _op("Mark Sharp", "ADD", "mesh.mark_sharp"),
    "mesh.edge_flow": _op("Set Flow", "SPHERECURVE", "mesh.set_edge_flow", available=_edge_mode),
    "mesh.add_material": _op("Add Material", "MATERIAL", "object.add_mat_sel_faces", available=_face_mode),
    "mesh.remove_checker": _op("Remove Checker", "X", "mesh.remove_checker", available=_edge_mode),
    "normal.flip": _op("Flip", "ORIENTATION_NORMAL", "mesh.flip_normals"),
    "normal.recalculate": _op("Recalculate", "NORMALS_VERTEX_FACE", "mesh.normals_make_consistent"),
    "normal.reset": _op("Reset Normal", "SHADERFX", "mesh.normals_tools", {"mode": "RESET"}),
    "normal.rotate": _op("Rotate", "NORMALS_VERTEX", "transform.rotate_normal"),
    "weight.select_weak": _op("Select Weak", "RESTRICT_SELECT_ON", "mesh.mzage_select_weight", {"strength": "WEAK"}),
    "weight.select_medium": _op("Select Medium", "RESTRICT_SELECT_ON", "mesh.mzage_select_weight", {"strength": "MEDIUM"}),
    "weight.select_strong": _op("Select Strong", "RESTRICT_SELECT_ON", "mesh.mzage_select_weight", {"strength": "STRONG"}),
    "weight.set_weak": _op("Set Weak", "RESTRICT_SELECT_OFF", "mesh.mzage_set_weight", {"strength": "WEAK"}),
    "weight.set_medium": _op("Set Medium", "RESTRICT_SELECT_OFF", "mesh.mzage_set_weight", {"strength": "MEDIUM"}),
    "weight.set_strong": _op("Set Strong", "RESTRICT_SELECT_OFF", "mesh.mzage_set_weight", {"strength": "STRONG"}),
    "select.rings": _op("Rings", "MESH_CIRCLE", "mesh.loop_multi_select", {"ring": True}),
    "select.loops": _op("Loops", "STROKE", "mesh.loop_multi_select", {"ring": False}),
    "select.boundary": _op("Boundary", "MOD_LATTICE", "mesh.region_to_loop"),
    "select.checker": _op("Checker", "TEXTURE", "mesh.select_nth"),
    "select.inside": _op("Inside", "OUTLINER_OB_LATTICE", "mesh.loop_to_region"),
    "select.coplanar": _op("Coplanar", "FACESEL", "mesh.select_similar", {"type": "FACE_COPLANAR"}, _coplanar_mode),
    "select.overlap": _op("Overlapping Vertices", "VERTEXSEL", "mesh.select_overlapping_vertices"),
    "vcolor.copy": _op("Copy Vertex Color", "COPYDOWN", "mesh.copy_vertex_color"),
    "vcolor.paste": _op("Paste Vertex Color", "PASTEDOWN", "mesh.paste_vertex_color"),
    "vcolor.paint": _op("Vertex Color HSV Paint", "BRUSH_DATA", "mesh.vertex_color_hsv_paint"),
    "vcolor.select": _op("Select Same Vertex Color", "COLOR", "mesh.select_same_vertex_color"),
    "lightmap.scale": _op("Set Lightmap Scale", "FIXED_SIZE", "lightmap.set_scale"),
    "lightmap.pack": _op("Scaled UV Packing", "UV", "lightmap.scaled_uv_packing"),
    "lightmap.unpack": _op("Unpack All", "ACTION", "lightmap.unpack_collections"),
    "lightmap.clear": _op("Clear", "REMOVE", "lightmap.clear_lightmapping_stuff"),
    "lightmap.bake": _op("Bake", "LIGHT_DATA", "lightmap.bake_batch"),
    "lightmap.denoise": _op("Denoise", "IMAGE_DATA", "lightmap.denoise_batch"),
    "lightmap.restore_fillers": _op("Restore Fillers", "LOOP_BACK", "lightmap.clear_filler_replacements"),
    "lightmap.replace_fillers": _op("Replace Fillers", "DUPLICATE", "lightmap.replace_fillers"),
    "lightmap.unpack_active": _op("Unpack Active", "OUTLINER_COLLECTION", "lightmap.unpack_active_collection"),
    "lightmap.repack_active": _op("Repack Active", "UV", "lightmap.repack_active_batch"),
    "collision.box": _op("Add Box Collision", "SHADING_BBOX", "collision.add_box_collision_to_selected"),
    "collision.sphere": _op("Add Sphere Collision", "MESH_UVSPHERE", "collision.add_sphere_collision_to_selected"),
    "collision.capsule": _op("Add Capsule Collision", "META_CAPSULE", "collision.add_capsule_collision_to_selected"),
    "collision.convex": _op("Add Convex Collision", "MESH_ICOSPHERE", "collision.add_convex_collision_to_selected"),
    "util.origin_selection": _op("Origin to Selected", "OBJECT_ORIGIN", "mesh.set_origin_to_selection"),
    "util.fix_rotation": _op("Fix Rotation", "OBJECT_ORIGIN", "mesh.set_origin_to_selection_and_rotate"),
    "util.edge_length": _op("Get Edge Length", "DRIVER_DISTANCE", "mesh.get_edge_length"),
    "util.edge_angle": _op("Get Edges Angle", "DRIVER_ROTATIONAL_DIFFERENCE", "mesh.get_edges_angle"),
    "object.parent": _op("Create Empty Parent", "EMPTY_DATA", "object.create_empty_parent"),
    "object.parent_each": _op("Create Parent for Each", "EMPTY_DATA", "object.create_empty_parent_foreach"),
    "object.parent_active": _op("Parent to Active", "EMPTY_DATA", "object.create_empty_parent_active"),
    "object.history": _op("Object History", "LOOP_BACK", "wm.call_panel", {"name": "DATA_PT_PsychoHistory_KM"}, _has_history),
    "object.copy_modifiers": _op("Copy Modifiers From Active", "MODIFIER", "object.make_links_data", {"type": "MODIFIERS"}),
    "object.copy_materials": _op("Copy Materials From Active", "MATERIAL", "object.make_links_data", {"type": "MATERIAL"}),
    "object.origins_active": _op("Set Origins To Active", "TRANSFORM_ORIGINS", "object.selected_origins_to_active"),
    "object.add_active": _op("Add Active In Place", "DUPLICATE", "object.add_active_in_place_of_selected"),
    "object.replace_active": _op("Replace With Active", "FILE_REFRESH", "object.replace_selected_with_active"),
    "asset.create": _op("Make Collection Asset", "ASSET_MANAGER", "assetbrowser.make_collection_asset_from_selection"),
    "export.import_fbx": _op("Import FBX", "IMPORT", "import_scene.fbx"),
    "export.fbx": _op("Export FBX", "EXPORT", "export_scene.fbx"),
    "export.batch": _op("Batch Export SM_", "EXPORT", "object.batch_export_selections_as_sm"),
    "unreal.setup": _op("Setup for export", "SHADERFX", "object.btus_setup"),
    "unreal.export": _op("Set Export", "FAKE_USER_ON", "object.btus_export"),
    "unreal.noexport": _op("Set Dont Export", "FAKE_USER_OFF", "object.btus_dontexport"),
    "unreal.path": _op("Update Path", "FILEBROWSER", "object.btus_updatepath"),
}

PROPERTY_ACTIONS = {
    "object.display_type": ("Display Type", "NODE_MATERIAL", lambda c: c.active_object, "display_type", _has_mesh),
    "object.auto_smooth": ("Auto Smooth", "MOD_SMOOTH", lambda c: c.active_object.data, "use_auto_smooth", _has_mesh),
    "overlay.collisions": ("Display Collisions", "SHADING_BBOX", lambda c: c.scene, "display_collisions", None),
    "overlay.lighting": ("Display Lighting", "LIGHT", lambda c: c.scene, "display_lighting", None),
    "overlay.all": ("Show Overlays", "OVERLAY", lambda c: c.space_data.overlay, "show_overlays", lambda c: hasattr(c.space_data, "overlay")),
    "overlay.wire": ("Show Wireframes", "SHADING_WIRE", lambda c: c.space_data.overlay, "show_wireframes", lambda c: hasattr(c.space_data, "overlay")),
    "overlay.faces": ("Face Orientation", "FACESEL", lambda c: c.space_data.overlay, "show_face_orientation", lambda c: hasattr(c.space_data, "overlay")),
}

# Preserve presentation details from the original hard-coded pie menus.
PROPERTY_PIE_TEXT = {
    "object.display_type": "",
}


def _configuration():
    prefs = Preferences.get()
    if prefs:
        try:
            return model.loads(prefs.menu_config)
        except model.MenuConfigError:
            pass
    return model.clone_default()


def _root_key(context):
    active = context.active_object
    if not active:
        return "no_active"
    return "edit" if active.mode == "EDIT" else "object"


def _operator_exists(operator_id):
    try:
        category, name = operator_id.split(".", 1)
        return getattr(getattr(bpy.ops, category), name)
    except (AttributeError, ValueError):
        return None


def _draw_action(layout, item, context, pie=False):
    action_id = item.get("action")
    if action_id in PROPERTY_ACTIONS:
        label, icon, owner_getter, prop_name, check = PROPERTY_ACTIONS[action_id]
        label, icon = item.get("label") or label, item.get("icon") or icon
        try:
            available = not check or check(context)
            owner = owner_getter(context) if available else None
            available = available and owner is not None and hasattr(owner, prop_name)
        except Exception:
            available = False
            owner = None
        if available:
            if label.strip().upper() == "HIDDEN":
                text = ""
            else:
                text = PROPERTY_PIE_TEXT.get(action_id, label) if pie else label
            layout.prop(owner, prop_name, text=text, icon=icon)
        else:
            layout.separator()
        return
    action = ACTION_CATALOG.get(action_id)
    if not action:
        layout.separator()
        return
    label = item.get("label") or action["label"]
    icon = item.get("icon") or action["icon"]
    operator = _operator_exists(action["operator"])
    if operator is None:
        layout.separator()
        return
    button = layout.operator(action["operator"], text=label, icon=icon)
    for name, value in action["properties"].items():
        try:
            setattr(button, name, value)
        except (AttributeError, TypeError):
            pass


def _resolve_property_path(path, context):
    model.parse_property_path(path)
    node = ast.parse(path, mode="eval").body

    def resolve(part):
        if isinstance(part, ast.Name):
            return context
        if isinstance(part, ast.Attribute):
            if isinstance(part.value, ast.Name) and part.value.id == "bpy":
                return bpy.data
            return getattr(resolve(part.value), part.attr)
        return resolve(part.value)[part.slice.value]

    if isinstance(node, ast.Attribute):
        return resolve(node.value), node.attr, None
    key = node.slice.value
    if isinstance(key, int) and isinstance(node.value, ast.Attribute):
        return resolve(node.value.value), node.value.attr, key
    return resolve(node.value), f"[{json.dumps(key)}]", None


def _draw_menu(layout, menu, context, pie):
    target = layout.menu_pie() if pie else layout
    target.operator_context = "INVOKE_DEFAULT"
    # Disabled standard-menu entries remain visible. In a pie they do not
    # consume one of Blender's eight physical slots, so later enabled items
    # can still appear.
    items = ([item for item in menu["items"] if item.get("enabled", True)][:8]
             if pie else menu["items"])
    for item in items:
        item_type = item["type"]
        if item_type == "separator":
            target.separator()
            continue
        item_layout = target
        label = item.get("label") or ""
        icon = item.get("icon") or "NONE"
        if not item.get("enabled", True):
            if pie:
                target.separator()
                continue
            item_layout = target.row()
            item_layout.enabled = False
        if item_type == "submenu":
            class_id = _menu_class_ids.get(item["menu"]["id"])
            if not class_id:
                item_layout.separator()
            elif pie:
                item_layout.operator("wm.call_menu_pie", text=label or item["menu"]["label"], icon=icon).name = class_id
            else:
                item_layout.menu(class_id, text=label or item["menu"]["label"], icon=icon)
        elif item_type == "plugin_action":
            _draw_action(item_layout, item, context, pie)
        elif item_type == "custom_operator":
            operator = _operator_exists(item["operator_id"])
            if operator is None:
                item_layout.separator()
            else:
                button = item_layout.operator(item["operator_id"],
                                              text=label or item["operator_id"], icon=icon)
                for name, value in item.get("properties", {}).items():
                    try:
                        setattr(button, name, value)
                    except (AttributeError, TypeError):
                        pass
        elif item_type == "custom_property":
            try:
                owner, prop_name, prop_index = _resolve_property_path(
                    item["property_path"], context)
                property_text = "" if label.strip().upper() == "HIDDEN" else label
                property_layout = item_layout.row(align=True)
                if not pie and prop_index is None:
                    try:
                        rna_property = owner.bl_rna.properties[prop_name]
                        if rna_property.is_array:
                            property_layout.ui_units_x = max(
                                12.0, rna_property.array_length * 4.0)
                    except (AttributeError, KeyError, TypeError):
                        pass
                if prop_index is None:
                    property_layout.prop(owner, prop_name, text=property_text, icon=icon)
                else:
                    property_layout.prop(owner, prop_name, text=property_text,
                                         icon=icon, index=prop_index)
            except Exception:
                item_layout.separator()


class MZageHandyMenu(bpy.types.Menu):
    bl_label = "Psycho Vertex Master"
    bl_idname = "OBJECT_MT_mzage_handy_menu"

    def draw(self, context):
        config = _configuration()
        menu = config["roots"][_root_key(context)]
        _draw_menu(self.layout, menu, context, Preferences.get_mode() == "PIE_MENUS")


def rebuild_dynamic_menus():
    global _dynamic_classes, _menu_class_ids
    for cls in reversed(_dynamic_classes):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
    _dynamic_classes = []
    _menu_class_ids = {}
    config = _configuration()

    def make_draw(configured_id):
        def draw(self, context):
            current, _ = model.find_menu(_configuration(), configured_id)
            if current:
                _draw_menu(self.layout, current, context, Preferences.get_mode() == "PIE_MENUS")
        return draw

    for index, (menu, _parents) in enumerate(model.walk_menus(config)):
        class_id = f"PVM_MT_config_{index}_{menu['id'].replace('-', '_')[:24]}"
        _menu_class_ids[menu["id"]] = class_id
        cls = type(class_id, (bpy.types.Menu,), {
            "bl_idname": class_id,
            "bl_label": menu["label"],
            "draw": make_draw(menu["id"]),
        })
        bpy.utils.register_class(cls)
        _dynamic_classes.append(cls)


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


classes = (
    MZageHandyMenuSelectWeight,
    MZageHandyMenuSetWeight,
    MZageHandyMenu,
)


def _unregister_legacy_menu_operators():
    for name in ("PVM_OT_UnavailableMenuItem", "PVM_OT_RunCustomOperator"):
        cls = getattr(bpy.types, name, None)
        if cls:
            try:
                bpy.utils.unregister_class(cls)
            except (RuntimeError, ValueError):
                pass


def register():
    registered = []
    try:
        _unregister_legacy_menu_operators()
        for cls in classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
        rebuild_dynamic_menus()
    except Exception:
        for dynamic_cls in reversed(_dynamic_classes):
            try:
                bpy.utils.unregister_class(dynamic_cls)
            except (RuntimeError, ValueError):
                pass
        _dynamic_classes.clear()
        _menu_class_ids.clear()
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except (RuntimeError, ValueError):
                pass
        raise


def unregister():
    global _dynamic_classes
    for cls in reversed(_dynamic_classes):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
    _dynamic_classes = []
    _menu_class_ids.clear()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _unregister_legacy_menu_operators()
