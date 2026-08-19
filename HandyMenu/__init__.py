from typing import cast, TYPE_CHECKING
import bpy
from bpy.types import Mesh, Context
from .. import Preferences
if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import IconItems
WEIGHTED_NORMALS_MODIFIER_NAME = '__Weighted_Normal__'


def is_class_registered(class_type):
    try:
        bpy.utils.register_class(class_type)
    except Exception as e:
        if "already registered" in str(e):
            return True
    return False


def panel_exists(bl_idname):
    for panel_cls in bpy.types.Panel.__subclasses__():
        if not hasattr(panel_cls, "bl_idname"):
            continue
        if panel_cls.bl_idname != bl_idname:
            continue
        if is_class_registered(panel_cls):
            return True
    return False


class MZageHandyMenuSelectWeight(bpy.types.Operator):
    bl_idname = "mesh.mzage_select_weight"
    bl_label = "Select Weight"
    bl_description = "tooltip"
    bl_options = {"REGISTER"}

    strength: bpy.props.StringProperty(name="weight")

    def execute(self, context: Context):
        bpy.ops.mesh.mod_weighted_strength(set=False, face_strength=self.strength)
        return {"FINISHED"}


class MZageHandyMenuSetWeight(bpy.types.Operator):
    bl_idname = "mesh.mzage_set_weight"
    bl_label = "Set Weight"
    bl_description = "tooltip"
    bl_options = {"REGISTER", "UNDO"}

    strength: bpy.props.StringProperty(name="weight")

    def execute(self, context):
        sos = context.selected_objects
        ao = context.active_object
        if ao not in sos:
            sos.append(ao)
        bpy.ops.mesh.mod_weighted_strength(set=True, face_strength=self.strength)
        bpy.ops.object.mode_set(mode='OBJECT')
        for so in sos:
            mesh = cast(Mesh, so.data)
            if not WEIGHTED_NORMALS_MODIFIER_NAME in [mod.name for mod in so.modifiers]:
                sharpness = [edge.use_edge_sharp for edge in mesh.edges]
                context.view_layer.objects.active = so
                bpy.ops.object.shade_smooth(keep_sharp_edges=True)
                bpy.ops.mesh.customdata_custom_splitnormals_add()
                for i, edge in enumerate(mesh.edges):
                    edge.use_edge_sharp = sharpness[i]

                mod = so.modifiers.new(name=WEIGHTED_NORMALS_MODIFIER_NAME, type='WEIGHTED_NORMAL')
                mod.mode = 'FACE_AREA_WITH_ANGLE'
                mod.weight = 100
                mod.keep_sharp = True
                mod.use_face_influence = True
                mod.show_on_cage = True
        bpy.ops.object.mode_set(mode='EDIT')
        context.view_layer.objects.active = ao
        return {"FINISHED"}


def PieMenuButton(container: bpy.types.UILayout, menu: bpy.types.Menu, icon: 'IconItems' = "NONE"):
    container.operator('wm.call_menu_pie', text=menu.bl_label, icon=icon).name = menu.bl_idname


class MZageHandyMenu(bpy.types.Menu):
    bl_label = ""
    bl_idname = "OBJECT_MT_mzage_handy_menu"

    def draw(self, context):
        ao = context.active_object
        if Preferences.get_mode() == "PIE_MENUS":
            pie = self.layout.menu_pie()

            if not ao:
                pie.separator()
                PieMenuButton(pie, ExportSubMenu, icon='EXPORT')
                PieMenuButton(pie, LightmappingSubMenu, icon='LIGHT')
                PieMenuButton(pie, DisplayOverlaySubMenu, icon='OVERLAY')
                return

            if ao.mode == "EDIT":
                PieMenuButton(pie, UVSubMenu, icon='UV')
                PieMenuButton(pie, MeshSubMenu, icon='MESH_DATA')
                PieMenuButton(pie, NormalsSubMenu, icon='NORMALS_VERTEX')
                PieMenuButton(pie, SelectionSubMenu, icon='RESTRICT_SELECT_OFF')
                PieMenuButton(pie, VertexColorSubMenu, icon='GROUP_VCOL')
                PieMenuButton(pie, WeightSubMenu, icon='MOD_VERTEX_WEIGHT')
                PieMenuButton(pie, LightmappingSubMenu, icon='LIGHT')
                PieMenuButton(pie, UtilsSubMenu, icon='TOOL_SETTINGS')

            elif ao.mode == "OBJECT":
                PieMenuButton(pie, ObjectSubMenu, icon='OBJECT_DATA')
                PieMenuButton(pie, ExportSubMenu, icon='EXPORT')
                PieMenuButton(pie, LightmappingSubMenu, icon='LIGHT')
                PieMenuButton(pie, DisplayOverlaySubMenu, icon='OVERLAY')
                PieMenuButton(pie, ToUnrealSubMenu, icon='EXPORT')
                PieMenuButton(pie, AssetBrowserSubMenu, icon='ASSET_MANAGER')
                pie.separator()
                PieMenuButton(pie, ObjectModeUtilsSubMenu, icon='OBJECT_ORIGIN')
        else:
            layout = self.layout
            layout.operator_context = 'INVOKE_DEFAULT'
            if ao:
                mode = ao.mode
                if mode == "EDIT":
                    row = layout.row()
                    col = row.column()
                    col.label(text="UV")
                    col.operator("mesh.mark_seam", text="Mark Seam", icon="GREASEPENCIL")
                    col.operator("mesh.mark_seam", text="Clear Seam", icon="OUTLINER_DATA_GP_LAYER").clear = True
                    col.operator("uv.unwrap", text="Unwrap", icon="MOD_SHRINKWRAP")
                    col.operator("uv.project_from_view", text="View Project", icon="PROP_PROJECTED").scale_to_bounds = False
                    col.operator("uv.reset", text="Reset UVs", icon="X")
                    col.separator()
                    col.label(text="Mesh")
                    if bpy.context.tool_settings.mesh_select_mode[1]:
                        col.operator("mesh.set_edge_flow", text="Set Flow", icon="SPHERECURVE")
                        col.operator("mesh.remove_checker", text="Remove Checker", icon="X")
                    col.operator("mesh.mark_sharp", text="Mark Sharp", icon="GREASEPENCIL")
                    col.operator("mesh.mark_sharp", text="Clear Sharp", icon="OUTLINER_DATA_GP_LAYER").clear = True
                    if bpy.context.tool_settings.mesh_select_mode[2]:
                        col.operator("object.add_mat_sel_faces", text="Add Material", icon="MATERIAL")

                    col.separator()
                    col.label(text="Vertex Color")
                    col.operator("mesh.vertex_color_hsv_paint", icon="BRUSH_DATA")
                    col.operator("mesh.select_same_vertex_color", icon="COLOR")
                    col.operator("mesh.copy_vertex_color", icon="COPYDOWN")
                    col.operator("mesh.paste_vertex_color", icon="PASTEDOWN")

                    col = row.column()
                    col.label(text="Normals")
                    col.operator("mesh.flip_normals", text="Flip", icon="ORIENTATION_NORMAL")
                    col.operator("mesh.normals_make_consistent", text="Recalculate", icon="NORMALS_VERTEX_FACE")
                    col.operator("transform.rotate_normal", text="Rotate", icon="NORMALS_VERTEX")
                    col.operator("mesh.normals_tools", text="Reset Normal", icon="SHADERFX").mode = "RESET"

                    col.separator()
                    col.operator(MZageHandyMenuSelectWeight.bl_idname, text="Select Weak", icon="RESTRICT_SELECT_ON").strength = 'WEAK'
                    col.operator(MZageHandyMenuSelectWeight.bl_idname, text="Select Medium", icon="RESTRICT_SELECT_ON").strength = 'MEDIUM'
                    col.operator(MZageHandyMenuSelectWeight.bl_idname, text="Select Strong", icon="RESTRICT_SELECT_ON").strength = 'STRONG'

                    col.separator()
                    col.operator(MZageHandyMenuSetWeight.bl_idname, text="Set Weak", icon="RESTRICT_SELECT_OFF").strength = 'WEAK'
                    col.operator(MZageHandyMenuSetWeight.bl_idname, text="Set Medium", icon="RESTRICT_SELECT_OFF").strength = 'MEDIUM'
                    col.operator(MZageHandyMenuSetWeight.bl_idname, text="Set Strong", icon="RESTRICT_SELECT_OFF").strength = 'STRONG'

                    col.separator()
                    col.label(text="Lightmapping")
                    col.operator("lightmap.set_scale", icon="FIXED_SIZE")
                    col.operator("lightmap.scaled_uv_packing", icon="UV")

                    col = row.column()
                    col.label(text="Selection")
                    col.operator("mesh.loop_multi_select", text="Select Rings", icon="MESH_CIRCLE").ring = True
                    col.operator("mesh.loop_multi_select", text="Select Loops", icon="STROKE").ring = False
                    col.operator("mesh.region_to_loop", text="Select Boundary", icon="MOD_LATTICE")
                    col.operator("mesh.loop_to_region", text="Select Inside", icon="OUTLINER_OB_LATTICE")
                    col.operator("mesh.select_nth", text="Checker Deselect", icon="TEXTURE")
                    if bpy.context.tool_settings.mesh_select_mode[2] and not bpy.context.tool_settings.mesh_select_mode[0] and not bpy.context.tool_settings.mesh_select_mode[1]:
                        col.operator("mesh.select_similar", text="Select Coplanar", icon="FACESEL").type = "FACE_COPLANAR"
                    col.operator("mesh.select_overlapping_vertices", text="Overlap Vert", icon="VERTEXSEL")

                    col.separator()
                    col.label(text="Utils")
                    col.operator("mesh.set_origin_to_selection", text="Origin to Selected", icon="OBJECT_ORIGIN")
                    col.operator("mesh.set_origin_to_selection_and_rotate", text="Fix Rotation", icon="OBJECT_ORIGIN")
                    col.operator("collision.add_box_collision_to_selected", icon="SHADING_BBOX")
                    col.operator("mesh.get_edge_length", icon="DRIVER_DISTANCE")
                    col.operator("mesh.get_edges_angle", icon="DRIVER_ROTATIONAL_DIFFERENCE")
                    # col.operator("mesh.snap_vertices_to_surface", icon="MOD_SHRINKWRAP")

                elif mode == "OBJECT":
                    row = layout.row()
                    col = row.column()

                    col.label(text="Object")
                    if ao.type == "MESH":
                        col.prop(ao, "display_type", text="", icon="NODE_MATERIAL")
                        col.prop(ao.data, "use_auto_smooth", icon="MOD_SMOOTH")
                    col.operator("object.create_empty_parent", icon="EMPTY_DATA")
                    col.operator("object.create_empty_parent_foreach", icon="EMPTY_DATA")
                    col.operator("object.create_empty_parent_active", icon="EMPTY_DATA")
                    if panel_exists("DATA_PT_PsychoHistory_KM"):
                        col.operator("wm.call_panel", text="Object History", icon="LOOP_BACK").name = "DATA_PT_PsychoHistory_KM"

                    col.separator()
                    col.label(text="Copy From Active")
                    col.operator("object.make_links_data", text="Copy Modifiers", icon="MODIFIER").type = "MODIFIERS"
                    col.operator("object.make_links_data", text="Copy Materials", icon="MATERIAL").type = "MATERIAL"

                    col.separator()
                    col.label(text="Copy To Active")
                    col.operator("object.selected_origins_to_active", text="Origins To Active", icon="TRANSFORM_ORIGINS")

                    col.separator()
                    col.label(text="Asset Browser")
                    col.operator("assetbrowser.make_collection_asset_from_selection", icon="ASSET_MANAGER")

                    col = row.column()
                    col.label(text="Lightmapping")
                    col.operator("lightmap.unpack_collections", icon="ACTION")
                    col.operator("lightmap.bake_batch", icon="LIGHT_DATA")
                    col.operator("lightmap.clear_lightmapping_stuff", icon="REMOVE")

                    col = row.column()
                    col.label(text="Display Overlays")
                    col.prop(context.area.spaces[0].overlay, "show_overlays", icon="OVERLAY")
                    col.prop(context.area.spaces[0].overlay, "show_wireframes", icon="SHADING_WIRE")
                    col.prop(context.area.spaces[0].overlay, "show_face_orientation", icon="FACESEL")
                    col.prop(context.scene, "display_collisions", icon="SHADING_BBOX")
                    col.prop(context.scene, "display_lighting", icon="LIGHT")
                    col.separator()
                    col.label(text="Import/Export")
                    col.operator("import_scene.fbx", text="Import FBX", icon="IMPORT")
                    col.operator("export_scene.fbx", text="Export FBX", icon="EXPORT")
                    col.operator("object.batch_export_selections_as_sm", text="Batch Export SM_", icon="EXPORT")
                    col.separator()
                    col.label(text="To Unreal")
                    col.operator("object.btus_setup", text="Setup for export", icon="SHADERFX")
                    col.operator("object.btus_export", text="Set Export", icon="FAKE_USER_ON")
                    col.operator("object.btus_dontexport", text="Set Dont Export", icon="FAKE_USER_OFF")
                    col.operator("object.btus_updatepath", text="Update Path", icon="FILEBROWSER")
            else:
                if bpy.context.mode == "OBJECT":
                    row = layout.row()
                    col = row.column()
                    col.label(text="Display Overlays")
                    col.prop(context.area.spaces[0].overlay, "show_overlays", icon="OVERLAY")
                    col.prop(context.area.spaces[0].overlay, "show_wireframes", icon="SHADING_WIRE")
                    col.prop(context.area.spaces[0].overlay, "show_face_orientation", icon="FACESEL")
                    col.prop(context.scene, "display_collisions", icon="SHADING_BBOX")
                    col.prop(context.scene, "display_lighting", icon="LIGHT")
                    col.separator()
                    col.label(text="Import/Export")
                    col.operator("import_scene.fbx", text="Import FBX", icon="IMPORT")
                    col.operator("export_scene.fbx", text="Export FBX", icon="EXPORT")
                    col = row.column()
                    col.label(text="Lightmapping")
                    col.operator("lightmap.unpack_collections", icon="ACTION")
                    col.operator("lightmap.bake_batch", icon="LIGHT_DATA")
                    col.operator("lightmap.clear_lightmapping_stuff", icon="REMOVE")


# -----------------------------
# EDIT MODE SUB-MENUS
# -----------------------------
class UVSubMenu(bpy.types.Menu):
    bl_label = "UV"
    bl_idname = "OBJECT_MT_mzage_uv_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("mesh.mark_seam", text="Clear Seam", icon="REMOVE").clear = True
        layout.operator("mesh.mark_seam", text="Mark Seam", icon="ADD")
        layout.operator("uv.reset", text="Reset UVs", icon="X")
        layout.operator("uv.unwrap", text="Conformal", icon="MOD_SHRINKWRAP")
        layout.operator("uv.project_from_view", text="View Project", icon="PROP_PROJECTED").scale_to_bounds = False
        layout.separator()


class MeshSubMenu(bpy.types.Menu):
    bl_label = "Mesh"
    bl_idname = "OBJECT_MT_mzage_mesh_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("mesh.mark_sharp", text="Clear Sharp", icon="REMOVE").clear = True
        layout.operator("mesh.mark_sharp", text="Mark Sharp", icon="ADD")
        layout.operator("mesh.set_edge_flow", text="Set Flow", icon="SPHERECURVE")
        layout.operator("object.add_mat_sel_faces", icon="MATERIAL")
        if context.tool_settings.mesh_select_mode[1]:
            layout.operator("mesh.remove_checker", text="Remove Checker", icon="X")


class VertexColorSubMenu(bpy.types.Menu):
    bl_label = "Vertex Color"
    bl_idname = "OBJECT_MT_mzage_vertex_color_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("mesh.copy_vertex_color", icon="COPYDOWN")
        layout.operator("mesh.paste_vertex_color", icon="PASTEDOWN")
        layout.operator("mesh.vertex_color_hsv_paint", icon="BRUSH_DATA")
        layout.operator("mesh.select_same_vertex_color", icon="COLOR")


class NormalsSubMenu(bpy.types.Menu):
    bl_label = "Normals"
    bl_idname = "OBJECT_MT_mzage_normals_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("mesh.flip_normals", text="Flip", icon="ORIENTATION_NORMAL")
        layout.operator("mesh.normals_make_consistent", text="Recalculate", icon="NORMALS_VERTEX_FACE")
        layout.operator("transform.rotate_normal", text="Rotate", icon="NORMALS_VERTEX")
        layout.operator("mesh.normals_tools", text="Reset Normal", icon="SHADERFX").mode = "RESET"


class SelectionSubMenu(bpy.types.Menu):
    bl_label = "Selection"
    bl_idname = "OBJECT_MT_mzage_selection_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("mesh.loop_multi_select", text="Rings", icon="MESH_CIRCLE").ring = True
        layout.operator("mesh.loop_multi_select", text="Loops", icon="STROKE").ring = False
        layout.operator("mesh.region_to_loop", text="Boundary", icon="MOD_LATTICE")
        layout.operator("mesh.select_nth", text="Checker", icon="TEXTURE")
        layout.operator("mesh.loop_to_region", text="Inside", icon="OUTLINER_OB_LATTICE")
        ts = bpy.context.tool_settings.mesh_select_mode
        if ts[2] and not ts[0] and not ts[1]:
            layout.operator("mesh.select_similar", text="Coplanar", icon="FACESEL").type = "FACE_COPLANAR"
        layout.operator("mesh.select_overlapping_vertices", text="Overlapping Vertices", icon="VERTEXSEL")


class WeightSubMenu(bpy.types.Menu):
    bl_label = "Weight"
    bl_idname = "OBJECT_MT_mzage_weight_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.separator()
        layout.separator()
        layout.operator(MZageHandyMenuSelectWeight.bl_idname, text="Select Medium", icon="RESTRICT_SELECT_ON").strength = 'MEDIUM'
        layout.operator(MZageHandyMenuSetWeight.bl_idname, text="Set Medium", icon="RESTRICT_SELECT_OFF").strength = 'MEDIUM'
        layout.operator(MZageHandyMenuSetWeight.bl_idname, text="Set Weak", icon="RESTRICT_SELECT_OFF").strength = 'WEAK'
        layout.operator(MZageHandyMenuSetWeight.bl_idname, text="Set Strong", icon="RESTRICT_SELECT_OFF").strength = 'STRONG'
        layout.operator(MZageHandyMenuSelectWeight.bl_idname, text="Select Weak", icon="RESTRICT_SELECT_ON").strength = 'WEAK'
        layout.operator(MZageHandyMenuSelectWeight.bl_idname, text="Select Strong", icon="RESTRICT_SELECT_ON").strength = 'STRONG'


class LightmappingSubMenu(bpy.types.Menu):
    bl_label = "Lightmapping"
    bl_idname = "OBJECT_MT_mzage_lightmapping_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        ao = context.active_object
        if ao and ao.mode == "EDIT":
            layout.operator("lightmap.set_scale", icon="FIXED_SIZE")
            layout.operator("lightmap.scaled_uv_packing", icon="UV")
        else:
            layout.operator("lightmap.unpack_collections", icon="ACTION")
            layout.operator("lightmap.clear_lightmapping_stuff", icon="REMOVE")
            layout.operator("lightmap.bake_batch", icon="LIGHT_DATA")


class UtilsSubMenu(bpy.types.Menu):
    bl_label = "Utils"
    bl_idname = "OBJECT_MT_mzage_utils_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("mesh.set_origin_to_selection", text="Origin to Selected", icon="OBJECT_ORIGIN")
        layout.operator("mesh.set_origin_to_selection_and_rotate", text="Fix Rotation", icon="OBJECT_ORIGIN")
        layout.operator("mesh.get_edge_length", icon="DRIVER_DISTANCE")
        layout.operator("mesh.get_edges_angle", icon="DRIVER_ROTATIONAL_DIFFERENCE")
        layout.operator("collision.add_box_collision_to_selected", icon="SHADING_BBOX")
        # layout.operator("mesh.snap_vertices_to_surface", icon="MOD_SHRINKWRAP")

# -----------------------------
# OBJECT MODE SUB-MENUS
# -----------------------------


class ObjectSubMenu(bpy.types.Menu):
    bl_label = "Object"
    bl_idname = "OBJECT_MT_mzage_object_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        ao = context.active_object
        if ao.type == "MESH":
            layout.prop(ao, "display_type", text="", icon="NODE_MATERIAL")
            layout.prop(ao.data, "use_auto_smooth", icon="MOD_SMOOTH")
        layout.operator("object.create_empty_parent", icon="EMPTY_DATA")
        layout.operator("object.create_empty_parent_foreach", icon="EMPTY_DATA")
        layout.operator("object.create_empty_parent_active", icon="EMPTY_DATA")
        if panel_exists("DATA_PT_PsychoHistory_KM"):
            layout.operator("wm.call_panel", text="Object History", icon="LOOP_BACK").name = "DATA_PT_PsychoHistory_KM"


class ObjectModeUtilsSubMenu(bpy.types.Menu):
    bl_label = "Utils"
    bl_idname = "OBJECT_MT_mzage_copy_from_active_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("object.make_links_data", text="Copy Modifiers From Active", icon="MODIFIER").type = "MODIFIERS"
        layout.operator("object.make_links_data", text="Copy Materials From Active", icon="MATERIAL").type = "MATERIAL"
        layout.operator("object.selected_origins_to_active", text="Set Origins To Active", icon="TRANSFORM_ORIGINS")


class ExportSubMenu(bpy.types.Menu):
    bl_label = "Import ➕ Export"
    bl_idname = "OBJECT_MT_mzage_export_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("import_scene.fbx", icon="IMPORT")
        layout.operator("export_scene.fbx", icon="EXPORT")
        layout.operator("object.batch_export_selections_as_sm", text="Batch Export SM_", icon="EXPORT")


class DisplayOverlaySubMenu(bpy.types.Menu):
    bl_label = "Display Overlays"
    bl_idname = "OBJECT_MT_mzage_overlay_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.prop(context.scene, "display_collisions", icon="SHADING_BBOX")
        layout.prop(context.scene, "display_lighting", icon="LIGHT")
        layout.prop(context.area.spaces[0].overlay, "show_overlays", icon="OVERLAY")
        layout.prop(context.area.spaces[0].overlay, "show_wireframes", icon="SHADING_WIRE")
        layout.prop(context.area.spaces[0].overlay, "show_face_orientation", icon="FACESEL")


class ToUnrealSubMenu(bpy.types.Menu):
    bl_label = "To Unreal"
    bl_idname = "OBJECT_MT_mzage_to_unreal_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("object.btus_setup", text="Setup for export", icon="SHADERFX")
        layout.operator("object.btus_export", text="Set Export", icon="FAKE_USER_ON")
        layout.operator("object.btus_dontexport", text="Set Dont Export", icon="FAKE_USER_OFF")
        layout.operator("object.btus_updatepath", text="Update Path", icon="FILEBROWSER")


class AssetBrowserSubMenu(bpy.types.Menu):
    bl_label = "Asset Browser"
    bl_idname = "OBJECT_MT_mzage_asset_browser_menu"

    def draw(self, context):
        layout = self.layout.menu_pie()
        layout.operator("assetbrowser.make_collection_asset_from_selection", icon="ASSET_MANAGER")


classes = [
    MZageHandyMenuSelectWeight,
    MZageHandyMenuSetWeight,
    MZageHandyMenu,
    UVSubMenu,
    MeshSubMenu,
    VertexColorSubMenu,
    NormalsSubMenu,
    SelectionSubMenu,
    WeightSubMenu,
    LightmappingSubMenu,
    UtilsSubMenu,
    ObjectSubMenu,
    ObjectModeUtilsSubMenu,
    ExportSubMenu,
    DisplayOverlaySubMenu,
    ToUnrealSubMenu,
    AssetBrowserSubMenu
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
