from typing import cast
import os
import re
import time
import shutil
import math
import base64
import subprocess
from tempfile import TemporaryDirectory
import bpy
import bmesh
import numpy as np
from mathutils import Matrix, Vector
from bpy.types import Context, Operator, Object, Collection, LayerCollection, ShaderNodeTexImage, CompositorNodeImage, Material, Image, ShaderNodeVertexColor, ShaderNodeBsdfRayPortal, ShaderNodeBsdfTransparent, ShaderNodeAddShader
from bpy.props import BoolProperty, FloatProperty, StringProperty, IntProperty, EnumProperty
from bpy.app.handlers import persistent
from ..Pipeline import PipelineOperator, PipelineTask
from .. import Preferences
from .Denoisers import (
    DENOISER_ITEMS,
    DenoiseContext,
    create_backend,
    crop_pixels_to_mask,
    dilate_rgba,
    downsample_premultiplied,
    save_pixels_exr_atomic,
)

EPS = 1e-6
COLLISION_PREFIXES = ("UBX_", "UCX_", "UCP_", "USP_")
FILLER_ASSET_KEY = "pvm_filler_asset"
FILLER_GROUP_KEY = "pvm_filler_group"
FILLER_RELATIVE_MATRIX_KEY = "pvm_filler_relative_matrix"
BATCH_BAKED_KEY = "pvm_lightmap_baked"
BATCH_LIGHTMAP_KEY = "pvm_lightmap_path"
BATCH_NOISY_LIGHTMAP_KEY = "pvm_lightmap_noisy_path"
BATCH_SOURCE_COLLECTION_KEY = "pvm_source_collection"
BATCH_IMPORT_READY_KEY = "pvm_import_ready"
BATCH_PREPARATION_FAILURES_KEY = "pvm_preparation_failures"
BATCH_PREPARATION_FAILURE_COUNT_KEY = "pvm_preparation_failure_count"
BATCH_FILLERS_COLLECTION = "BATCH_FILLERS"

BAKE_DIALOG_SETTINGS = (
    "render_resolution", "samples", "margin", "use_adaptive_sampling",
    "adaptive_threshold", "clamp_direct", "clamp_indirect", "filter_glossy",
    "max_bounces", "diffuse_bounces", "glossy_bounces",
    "transmission_bounces", "transparent_bounces", "light_sampling_threshold",
    "use_pass_direct", "use_pass_indirect", "fast_bake",
)
DENOISE_DIALOG_SETTINGS = (
    "final_resolution", "denoiser", "denoiser_step", "margin_mode", "margin",
)
PACK_DIALOG_SETTINGS = (
    "uvMargin", "textureSize", "packLightmaps", "pixelPerfect", "heuristicDuration",
    "preparationErrorPolicy",
)
REPACK_DIALOG_SETTINGS = ("uvMargin", "textureSize", "pixelPerfect", "heuristicDuration")
SCALED_PACK_DIALOG_SETTINGS = ("heuristic", "pixel_margin", "texture_size")


def _send_windows_notification(title: str, message: str):
    """Show a non-blocking Windows notification-area balloon."""
    if os.name != "nt":
        return
    title = title.replace("'", "''")
    message = message.replace("'", "''")
    script = f"""
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $notification = New-Object System.Windows.Forms.NotifyIcon
    $notification.Icon = [System.Drawing.SystemIcons]::Information
    $notification.Visible = $true
    $notification.ShowBalloonTip(5000, '{title}', '{message}', [System.Windows.Forms.ToolTipIcon]::Info)
    Start-Sleep -Seconds 6
    $notification.Dispose()
    """
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded_script],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _is_bake_mesh(obj: Object) -> bool:
    return obj.type == 'MESH' and not obj.hide_render and not obj.name.startswith(COLLISION_PREFIXES)


def _batch_meshes(collection: Collection) -> list[Object]:
    objects = list(collection.objects)
    for child in collection.children:
        suffix = child.name[7:] if child.name.startswith("Fillers") else ""
        if suffix.isdigit():
            continue
        objects.extend(_batch_meshes(child))
    return [obj for obj in objects if _is_bake_mesh(obj)]


def _lightmap_uv_coverage(objects: list[Object], resolution: int) -> np.ndarray:
    """Conservatively rasterize light-baked UV triangles into a coverage mask."""
    coverage = np.zeros((resolution, resolution), dtype=bool)
    for obj in objects:
        mesh = obj.data
        uv_layer = mesh.uv_layers.get("LightMap")
        if uv_layer is None:
            continue
        mesh.calc_loop_triangles()
        for triangle in mesh.loop_triangles:
            polygon = mesh.polygons[triangle.polygon_index]
            material = (
                obj.material_slots[polygon.material_index].material
                if polygon.material_index < len(obj.material_slots) else None
            )
            if material is None or not material.light_baked:
                continue
            vertices = np.array(
                [
                    (uv_layer.data[index].uv.x, uv_layer.data[index].uv.y)
                    for index in triangle.loops
                ],
                dtype=np.float64,
            ) * resolution
            edge_a = vertices[1] - vertices[0]
            edge_b = vertices[2] - vertices[0]
            if abs(edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0]) <= EPS:
                continue
            x0 = max(0, math.floor(vertices[:, 0].min()))
            x1 = min(resolution - 1, math.floor(vertices[:, 0].max()))
            y0 = max(0, math.floor(vertices[:, 1].min()))
            y1 = min(resolution - 1, math.floor(vertices[:, 1].max()))
            if x0 > x1 or y0 > y1:
                continue
            pixel_x = np.arange(x0, x1 + 1, dtype=np.float64) + 0.5
            edges = (
                vertices[1] - vertices[0],
                vertices[2] - vertices[1],
                vertices[0] - vertices[2],
            )
            for y in range(y0, y1 + 1):
                pixel_y = y + 0.5
                intersects = np.ones(pixel_x.shape, dtype=bool)
                for edge in edges:
                    normal_x, normal_y = -edge[1], edge[0]
                    triangle_projection = (
                        vertices[:, 0] * normal_x
                        + vertices[:, 1] * normal_y
                    )
                    center_projection = pixel_x * normal_x + pixel_y * normal_y
                    pixel_radius = 0.5 * (abs(normal_x) + abs(normal_y))
                    intersects &= (
                        (center_projection + pixel_radius >= triangle_projection.min() - EPS)
                        & (center_projection - pixel_radius <= triangle_projection.max() + EPS)
                    )
                    if not intersects.any():
                        break
                coverage[y, x0:x1 + 1] |= intersects
    return coverage


def _publish_review_image(name: str, pixels: np.ndarray) -> Image:
    """Create or refresh an in-memory image for lightmap diagnostics."""
    height, width, channels = pixels.shape
    if channels != 4:
        raise RuntimeError("Review image must contain RGBA pixels")
    image = bpy.data.images.get(name)
    if image is None:
        image = bpy.data.images.new(
            name=name,
            width=width,
            height=height,
            alpha=True,
            float_buffer=True,
            is_data=True,
        )
    elif tuple(image.size) != (width, height):
        image.scale(width, height)
    image.colorspace_settings.name = 'Non-Color'
    image.pixels.foreach_set(np.asarray(pixels, dtype=np.float32).ravel())
    image.update()
    return image


def _matrix_to_property(matrix: Matrix) -> list[float]:
    return [value for row in matrix for value in row]


def _matrix_from_property(values) -> Matrix:
    if len(values) != 16:
        raise ValueError("stored transform must contain 16 values")
    return Matrix([values[index:index + 4] for index in range(0, 16, 4)])


def _batch_number(collection: Collection) -> int | None:
    match = re.match(r"^Batch(\d+)(?:\s.*)?$", collection.name)
    return int(match.group(1)) if match else None


def _batch_collection_name(number: int, source_collection: Collection) -> str:
    return f"Batch{number} ({source_collection.name})"


def _batch_source_name(batch: Collection) -> str | None:
    source_name = batch.get(BATCH_SOURCE_COLLECTION_KEY)
    if source_name:
        return source_name
    match = re.match(r"^Batch\d+\s+\((.+)\)$", batch.name)
    return match.group(1) if match else None


def _filler_collection_name(batch: Collection) -> str | None:
    source_name = _batch_source_name(batch)
    if not source_name or not source_name.startswith("S_"):
        return None
    return f"F_{source_name[2:]}"


def _batch_for_source(export: Collection, source: Collection) -> Collection | None:
    """Find metadata-backed output, with exact legacy-name compatibility."""
    legacy = None
    for batch in export.children:
        number = _batch_number(batch)
        if number is None:
            continue
        if batch.get(BATCH_SOURCE_COLLECTION_KEY) == source.name:
            return batch
        if batch.name == _batch_collection_name(number, source):
            legacy = batch
    return legacy


def _instance_collision_members(collection: Collection):
    """Collect visibility dependencies once; their live states are read later."""
    collections = []
    collision_objects = []
    seen_collections = set()
    seen_objects = set()
    pending = [collection]
    while pending:
        current = pending.pop()
        if current.as_pointer() in seen_collections:
            continue
        seen_collections.add(current.as_pointer())
        collections.append(current)
        pending.extend(list(current.children))
        for obj in current.objects:
            if obj.instance_collection is not None:
                pending.append(obj.instance_collection)
            if obj.as_pointer() in seen_objects or not obj.name.startswith(COLLISION_PREFIXES):
                continue
            seen_objects.add(obj.as_pointer())
            collision_objects.append(obj)
    return collections, collision_objects


def _temporarily_show_instance_collisions(collection: Collection, members=None):
    collections, collision_objects = members or _instance_collision_members(collection)
    collection_states = []
    object_states = []
    for current in collections:
        collection_states.append((current, current.hide_viewport))
        current.hide_viewport = False
    for obj in collision_objects:
        try:
            hidden = obj.hide_get()
            obj.hide_set(False)
        except RuntimeError:
            hidden = None
        object_states.append((obj, obj.hide_viewport, hidden))
        obj.hide_viewport = False
    return collection_states, object_states


def _deselect_selected(context: Context):
    """Deselect current objects without a full view-layer operator scan."""
    for obj in list(context.selected_objects):
        obj.select_set(False)


def _restore_instance_visibility(states):
    collection_states, object_states = states
    for obj, hide_viewport, hidden in object_states:
        obj.hide_viewport = hide_viewport
        if hidden is not None:
            obj.hide_set(hidden)
    for collection, hide_viewport in reversed(collection_states):
        collection.hide_viewport = hide_viewport


def _has_uvpackmaster(context: Context) -> bool:
    return hasattr(context.scene, "uvpm3_props") and hasattr(bpy.ops, "uvpackmaster3")


def _validate_pack_objects(operator: Operator, context: Context, objs: list[Object]):
    if not _has_uvpackmaster(context):
        operator.report({'ERROR'}, "UVPackmaster 3 is required and must be enabled")
        return False
    if not objs:
        operator.report({'ERROR'}, "Select at least one mesh in Edit Mode")
        return False
    for obj in objs:
        if obj.type != 'MESH' or obj.mode != 'EDIT':
            operator.report({'ERROR'}, f"'{obj.name}' must be a mesh in Edit Mode")
            return False
        if obj.data.uv_layers.get("LightMap") is None:
            operator.report({'ERROR'}, f"'{obj.name}' has no 'LightMap' UV layer")
            return False
        bm = bmesh.from_edit_mesh(obj.data)
    return True


def centroid(objs):
    return sum((o.matrix_world.translation for o in objs), Vector()) / len(objs)


def GetTempActiveObj(collection: Collection):
    obj = bpy.data.objects.new("TempObject", bpy.data.meshes.new("TempMesh"))
    collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


def ReleaseTempActiveObjByName():
    obj = bpy.data.objects.get("TempObject")
    if obj:
        ReleaseTempActiveObj(obj)


def ReleaseTempActiveObj(obj):
    bpy.data.objects.remove(obj, do_unlink=True)


def GetOrCreateCollection(name, parent=None) -> Collection:
    col = bpy.data.collections.get(name)
    if col:
        return col
    col = bpy.data.collections.new(name)
    if parent:
        parent.children.link(col)
    else:
        bpy.context.scene.collection.children.link(col)
    return col


def DeleteCollection(name):
    col = bpy.data.collections.get(name)
    if not col:
        return
    for child in list(col.children):
        DeleteCollection(child.name)
    for obj in list(col.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(col)


def GetLayerCollection(collection: str, current_layer_collection: LayerCollection):
    if current_layer_collection.collection.name == collection:
        return current_layer_collection
    for child in current_layer_collection.children:
        found = GetLayerCollection(collection, child)
        if found:
            return found
    return None


def _set_layer_excluded_recursive(layer_collection: LayerCollection, excluded: bool):
    layer_collection.exclude = excluded
    for child in layer_collection.children:
        _set_layer_excluded_recursive(child, excluded)


def UVPack_Normal(
    context: Context,
    normalize_scale: bool,
    heuristic: bool,
    pixel_margin: int,
    texture_size: int,
    heuristic_duration: int = 1,
    pixel_perfect: bool = False,
):
    props = context.scene.uvpm3_props
    props.normalize_scale = normalize_scale
    props.heuristic_enable = heuristic
    props.heuristic_search_time = heuristic_duration
    props.advanced_heuristic = True
    props.pixel_margin_enable = True
    props.pixel_margin = pixel_margin
    props.pixel_border_margin = pixel_margin
    props.pixel_margin_tex_size = texture_size
    if hasattr(props, "pixel_perfect_align"):
        props.pixel_perfect_align = pixel_perfect
    context.scene.tool_settings.use_uv_select_sync = True
    bpy.ops.mesh.reveal()
    bpy.ops.uv.select_all(action='SELECT')
    bpy.ops.uvpackmaster3.select_mode(mode_id="pack.single_tile")
    bpy.ops.uvpackmaster3.pack(mode_id="pack.single_tile", pack_op_type='1')


def UVPack_Scaled(
    context: Context,
    operator: Operator,
    objs: list[Object],
    heuristic: bool,
    pixel_margin: int,
    texture_size: int,
    heuristic_duration: int = 1,
    pixel_perfect: bool = False,
):
    if not _validate_pack_objects(operator, context, objs):
        return {'CANCELLED'}
    for obj in objs:
        ls_check = obj.data.uv_layers.get("LightMap")
        obj.data.uv_layers.active = ls_check
    UVPack_Normal(
        context,
        normalize_scale=True,
        heuristic=False,
        pixel_margin=pixel_margin,
        texture_size=texture_size,
        heuristic_duration=heuristic_duration,
        pixel_perfect=False,
    )

    # scale uvs and move to top right corner, adjust margin, and pin it
    for obj in objs:
        mat_slots = obj.material_slots
        bm = bmesh.from_edit_mesh(obj.data)
        ls_layer = bm.faces.layers.float.get("lightmap_scale")
        uv_layer = bm.loops.layers.uv.get("LightMap")
        for face in bm.faces:
            mat = mat_slots[face.material_index].material if face.material_index < len(mat_slots) else None
            for loop in face.loops:
                lightmap_scale = (face[ls_layer] if ls_layer else 1.0) if mat and mat.light_baked else 0
                uv = loop[uv_layer].uv * lightmap_scale
                if abs(uv.x) < EPS and abs(uv.y) < EPS:
                    loop[uv_layer].pin_uv = True
                    offset = 1 - (3 / texture_size)
                    uv += Vector((offset, offset))
                loop[uv_layer].uv = uv
        bmesh.update_edit_mesh(obj.data)

    UVPack_Normal(
        context,
        normalize_scale=False,
        heuristic=heuristic,
        pixel_margin=pixel_margin,
        texture_size=texture_size,
        heuristic_duration=heuristic_duration,
        pixel_perfect=pixel_perfect,
    )
    return {'FINISHED'}


class ReplaceFillers(Operator):
    bl_idname = "lightmap.replace_fillers"
    bl_label = "Replace Fillers"
    bl_description = "Replace FILLERS collection instances with copies of their matching batch objects"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Replace fillers for the active batch?", icon='OUTLINER_COLLECTION')
        layout.label(text="Other filler replacements are unchanged.")

    def execute(self, context: Context):
        fillers = bpy.data.collections.get("FILLERS")
        export = bpy.data.collections.get("EXPORT_STUFF")
        if fillers is None:
            self.report({'ERROR'}, "Create a collection named 'FILLERS'")
            return {'CANCELLED'}
        if export is None:
            self.report({'ERROR'}, "No EXPORT_STUFF collection; unpack collections first")
            return {'CANCELLED'}
        filler_layer = GetLayerCollection("FILLERS", context.view_layer.layer_collection)
        if filler_layer is None:
            self.report({'ERROR'}, "FILLERS is not present in the active View Layer")
            return {'CANCELLED'}

        active_batch = context.view_layer.active_layer_collection.collection
        if (
            _batch_number(active_batch) is None
            or export.children.get(active_batch.name) is not active_batch
        ):
            self.report({'ERROR'}, "Make the generated BatchN to replace active")
            return {'CANCELLED'}
        batches = [active_batch]

        groups_by_batch_asset = {}
        for batch in batches:
            batch_groups = {}
            # Realized source groups are linked directly to BatchN. Exclude prior
            # FillersN descendants, whose copies intentionally retain metadata.
            for obj in batch.objects:
                asset = obj.get(FILLER_ASSET_KEY)
                group_id = obj.get(FILLER_GROUP_KEY)
                relative = obj.get(FILLER_RELATIVE_MATRIX_KEY)
                if asset and group_id and relative is not None:
                    batch_groups.setdefault((asset, group_id), []).append(obj)
            for (asset, group_id), objects in batch_groups.items():
                groups_by_batch_asset.setdefault(batch, {}).setdefault(
                    asset, (group_id, objects))

        filler_instances = []
        filler_layers = {}
        seen = set()
        for batch in batches:
            filler_name = _filler_collection_name(batch)
            filler_collection = fillers.children.get(filler_name) if filler_name else None
            if filler_collection is None:
                continue
            layer = filler_layer.children.get(filler_collection.name)
            if layer is None:
                self.report({'ERROR'}, f"{filler_collection.name} is not present below FILLERS in the active View Layer")
                return {'CANCELLED'}
            filler_layers[batch] = layer
            for obj in filler_collection.all_objects:
                pointer = obj.as_pointer()
                if pointer in seen or obj.instance_collection is None:
                    continue
                seen.add(pointer)
                filler_instances.append((obj, batch))
        if not filler_instances:
            self.report({'ERROR'}, "No paired F_X collection contains collection-instance objects")
            return {'CANCELLED'}

        replacements = []
        unmatched = []
        for filler, batch in filler_instances:
            asset_name = filler.instance_collection.name
            match = groups_by_batch_asset.get(batch, {}).get(asset_name)
            if match is None:
                unmatched.append(f"{filler.name} ({asset_name})")
            else:
                replacements.append((filler, batch, *match))
        if not replacements:
            self.report({'ERROR'}, f"No FILLERS instances match {active_batch.name}")
            return {'CANCELLED'}
        if unmatched:
            print(
                f"[LIGHTMAP] Replace Fillers skipped {len(unmatched)} instance(s) "
                f"without a match in {active_batch.name}"
            )

        staged = {}
        previous = {}
        visibility_states = [(filler_layer, filler_layer.exclude)]
        visibility_states.extend(
            (layer, layer.exclude) for layer in filler_layers.values())
        fillers_root = export.children.get(BATCH_FILLERS_COLLECTION)
        created_fillers_root = False
        try:
            if fillers_root is None:
                fillers_root = bpy.data.collections.new(BATCH_FILLERS_COLLECTION)
                export.children.link(fillers_root)
                created_fillers_root = True
            for batch in {replacement[1] for replacement in replacements}:
                number = _batch_number(batch)
                stage = bpy.data.collections.new(f"__PVM_Fillers{number}_STAGING")
                fillers_root.children.link(stage)
                staged[batch] = stage

            for filler, batch, _group_id, source_objects in replacements:
                target = staged[batch]
                copies = {}
                for source in source_objects:
                    duplicate = source.copy()
                    target.objects.link(duplicate)
                    copies[source] = duplicate
                for source, duplicate in copies.items():
                    relative = _matrix_from_property(source[FILLER_RELATIVE_MATRIX_KEY])
                    if source.parent in copies:
                        duplicate.parent = copies[source.parent]
                        duplicate.matrix_parent_inverse = source.matrix_parent_inverse.copy()
                    else:
                        duplicate.parent = None
                    duplicate.matrix_world = filler.matrix_world @ relative

            for batch, stage in staged.items():
                final_name = f"Fillers{_batch_number(batch)}"
                old = fillers_root.children.get(final_name)
                if old is not None:
                    old.name = f"__PVM_{final_name}_PREVIOUS"
                    previous[batch] = old
                stage.name = final_name

            filler_layer.exclude = False
            for batch in staged:
                filler_layers[batch].exclude = True
        except Exception as ex:
            for layer, excluded in visibility_states:
                layer.exclude = excluded
            for stage in list(staged.values()):
                if stage.name in bpy.data.collections:
                    DeleteCollection(stage.name)
            for batch, old in previous.items():
                old.name = f"Fillers{_batch_number(batch)}"
            if created_fillers_root and fillers_root is not None and not fillers_root.children and not fillers_root.objects:
                bpy.data.collections.remove(fillers_root)
            self.report({'ERROR'}, f"Could not replace fillers: {ex}. Existing fillers were preserved")
            return {'CANCELLED'}

        for old in previous.values():
            DeleteCollection(old.name)

        # Remove legacy replacements from the old BatchN/FillersN layout only
        # after the new, isolated replacements have been created successfully.
        for batch in batches:
            legacy = batch.children.get(f"Fillers{_batch_number(batch)}")
            if legacy is not None:
                DeleteCollection(legacy.name)

        skipped = f"; skipped {len(unmatched)} without a match" if unmatched else ""
        self.report({'INFO'}, f"Replaced {len(replacements)} filler instance(s) for {active_batch.name}{skipped}")
        return {'FINISHED'}


class ClearFillerReplacements(Operator):
    bl_idname = "lightmap.clear_filler_replacements"
    bl_label = "Restore Fillers"
    bl_description = "Remove generated FillersN collections and reveal the original FILLERS collection"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Remove generated filler replacements?", icon='TRASH')
        layout.label(text="Batches and lightmaps are kept.")

    def execute(self, context: Context):
        export = bpy.data.collections.get("EXPORT_STUFF")
        filler_collections = []
        if export is not None:
            fillers_root = export.children.get(BATCH_FILLERS_COLLECTION)
            if fillers_root is not None:
                filler_collections.extend(list(fillers_root.children))
            for batch in export.children:
                number = _batch_number(batch)
                if number is None:
                    continue
                fillers = batch.children.get(f"Fillers{number}")
                if fillers is not None:
                    filler_collections.append(fillers)

        removed_objects = sum(len(collection.all_objects) for collection in filler_collections)
        for collection in filler_collections:
            DeleteCollection(collection.name)
        if export is not None:
            fillers_root = export.children.get(BATCH_FILLERS_COLLECTION)
            if fillers_root is not None:
                bpy.data.collections.remove(fillers_root)

        fillers_layer = GetLayerCollection("FILLERS", context.view_layer.layer_collection)
        if fillers_layer is not None:
            _set_layer_excluded_recursive(fillers_layer, False)

        if not filler_collections and fillers_layer is None:
            self.report({'WARNING'}, "No generated FillersN collections or visible FILLERS collection were found")
            return {'CANCELLED'}
        restored = "FILLERS restored" if fillers_layer is not None else "FILLERS is not in the active View Layer"
        self.report(
            {'INFO' if fillers_layer is not None else 'WARNING'},
            f"Cleared {len(filler_collections)} filler collection(s) and {removed_objects} object(s); {restored}",
        )
        return {'FINISHED'}


class ClearLightmappingStuff(Operator):
    bl_idname = "lightmap.clear_lightmapping_stuff"
    bl_label = "Clear Lightmapping Stuff"
    bl_options = {'REGISTER', "UNDO", "UNDO_GROUPED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        active = context.view_layer.active_layer_collection.collection
        export = bpy.data.collections.get("EXPORT_STUFF")
        number = _batch_number(active)
        if export is not None and number is not None and export.children.get(active.name) is active:
            layout.label(text=f"Delete {active.name} and its fillers?", icon='ERROR')
            return
        layout.label(text="Delete all generated lightmapping data?", icon='ERROR')

    def execute(self, context: Context):
        active = context.view_layer.active_layer_collection.collection
        export = bpy.data.collections.get("EXPORT_STUFF")
        number = _batch_number(active)
        if export is not None and number is not None and export.children.get(active.name) is active:
            source_name = active.get(BATCH_SOURCE_COLLECTION_KEY)
            filler_name = _filler_collection_name(active)
            source_layer = (
                GetLayerCollection(source_name, context.view_layer.layer_collection)
                if source_name else None
            )
            fillers_root = export.children.get(BATCH_FILLERS_COLLECTION)
            fillers = fillers_root.children.get(f"Fillers{number}") if fillers_root else None
            if fillers is not None:
                DeleteCollection(fillers.name)
            legacy_fillers = active.children.get(f"Fillers{number}")
            if legacy_fillers is not None:
                DeleteCollection(legacy_fillers.name)
            DeleteCollection(active.name)
            if fillers_root is not None and not fillers_root.children and not fillers_root.objects:
                bpy.data.collections.remove(fillers_root)
            fillers_layer = GetLayerCollection("FILLERS", context.view_layer.layer_collection)
            filler_layer = fillers_layer.children.get(filler_name) if fillers_layer and filler_name else None
            if filler_layer:
                filler_layer.exclude = False
            if source_layer:
                _set_layer_excluded_recursive(source_layer, False)
            self.report({'INFO'}, f"Cleared Batch{number} and its filler replacements")
            return {'FINISHED'}

        if bpy.data.collections.get('SOURCE'):
            source_layer = GetLayerCollection("SOURCE", context.view_layer.layer_collection)
            if source_layer:
                _set_layer_excluded_recursive(source_layer, False)
        fillers_layer = GetLayerCollection("FILLERS", context.view_layer.layer_collection)
        if fillers_layer:
            _set_layer_excluded_recursive(fillers_layer, False)
        DeleteCollection("EXPORT_STUFF")
        DeleteCollection("TEMP_EXPORT_STUFF")
        ReleaseTempActiveObjByName()

        for image in list(bpy.data.images):
            filepath = bpy.path.abspath(image.filepath) if image.filepath else ""
            basename = os.path.basename(filepath)
            if image.name == "NoisyLightmap" or re.fullmatch(
                r"LM_B\d+(?:_Noisy)?\.exr", basename, flags=re.IGNORECASE
            ):
                bpy.data.images.remove(image)

        if context.scene.node_tree:
            compositing_img_node = cast(CompositorNodeImage, context.scene.node_tree.nodes.get('Noisy Lightmap Slot'))
            if compositing_img_node:
                img = compositing_img_node.image
                compositing_img_node.image = None
                if img and img.name == "NoisyLightmap":
                    bpy.data.images.remove(img)

        # Purge Data
        bpy.ops.outliner.orphans_purge(do_linked_ids=True, do_local_ids=True, do_recursive=True)
        self.report({'INFO'}, "Lightmapping output and temporary data cleared")
        return {'FINISHED'}


class BakeBatch(PipelineOperator):
    bl_idname = "lightmap.bake_batch"
    bl_label = "Bake Batch"
    bl_options = {'REGISTER', "UNDO", "UNDO_GROUPED"}

    render_resolution: IntProperty(name="Render Resolution", default=4096, min=1)
    samples: IntProperty(name="Samples", default=128, min=1)
    margin: IntProperty(name="Margin", default=0, min=0)
    use_adaptive_sampling: BoolProperty(name="Adaptive Sampling", default=False)
    adaptive_threshold: FloatProperty(
        name="Noise Threshold", default=0.01, min=0.0, max=1.0, precision=4
    )
    clamp_direct: FloatProperty(
        name="Clamp Direct", description="0 disables direct-light clamping", default=0.0, min=0.0
    )
    clamp_indirect: FloatProperty(
        name="Clamp Indirect", description="Suppress indirect fireflies; 0 disables clamping", default=10.0, min=0.0
    )
    filter_glossy: FloatProperty(
        name="Filter Glossy", description="Blur glossy paths to reduce fireflies", default=1.0, min=0.0
    )
    max_bounces: IntProperty(name="Max Bounces", default=6, min=0, max=1024)
    diffuse_bounces: IntProperty(name="Diffuse Bounces", default=6, min=0, max=1024)
    glossy_bounces: IntProperty(name="Glossy Bounces", default=2, min=0, max=1024)
    transmission_bounces: IntProperty(name="Transmission Bounces", default=0, min=0, max=1024)
    transparent_bounces: IntProperty(name="Transparent Bounces", default=6, min=0, max=1024)
    light_sampling_threshold: FloatProperty(
        name="Light Threshold",
        description="Lower values sample weaker emissive lights; 0 samples all lights",
        default=0.01,
        min=0.0,
        max=1.0,
        precision=4,
    )
    use_pass_direct: BoolProperty(name="Direct Light", default=True)
    use_pass_indirect: BoolProperty(name="Indirect Light", default=True)
    fast_bake: BoolProperty(
        name="Fast Bake (Temporary Merge)",
        description="Join compatible temporary receiver copies to reduce Cycles bake startups",
        default=True,
    )

    def modal(self, context: Context, event):
        if event.type == 'ESC' and getattr(self, "_baking", False):
            return {'PASS_THROUGH'}
        return super().modal(context, event)

    def invoke(self, context, event):
        Preferences.load_dialog_settings(self, BAKE_DIALOG_SETTINGS)
        if not self.validate(context):
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        active_batch = context.view_layer.active_layer_collection.collection
        layout.label(text=f"Batch: {active_batch.name}", icon='OUTLINER_COLLECTION')
        layout.prop(self, "fast_bake")
        layout.prop(self, "render_resolution")
        layout.prop(self, "samples")
        layout.prop(self, "margin")
        sampling = layout.box()
        sampling.label(text="Sampling and Firefly Control")
        sampling.prop(self, "use_adaptive_sampling")
        threshold = sampling.row()
        threshold.enabled = self.use_adaptive_sampling
        threshold.prop(self, "adaptive_threshold")
        sampling.prop(self, "clamp_direct")
        sampling.prop(self, "clamp_indirect")
        sampling.prop(self, "filter_glossy")
        paths = layout.box()
        paths.label(text="Light Paths")
        paths.prop(self, "max_bounces")
        paths.prop(self, "diffuse_bounces")
        paths.prop(self, "glossy_bounces")
        paths.prop(self, "transmission_bounces")
        paths.prop(self, "transparent_bounces")
        paths.prop(self, "light_sampling_threshold")
        passes = layout.box()
        passes.label(text="Bake Contributions")
        passes.prop(self, "use_pass_direct")
        passes.prop(self, "use_pass_indirect")

    def validate(self, context: Context):
        export = bpy.data.collections.get('EXPORT_STUFF')
        if export is None:
            self.report({'ERROR'}, "No EXPORT_STUFF collection; run Unpack Active first")
            return False
        batch_collection = context.view_layer.active_layer_collection.collection
        batch_number = _batch_number(batch_collection)
        if export.children.get(batch_collection.name) is not batch_collection or batch_number is None:
            self.report({'ERROR'}, "Make a generated Batch collection active in the Outliner")
            return False
        if self.fast_bake and batch_collection.get(BATCH_IMPORT_READY_KEY, True) is False:
            self.report({'ERROR'}, "Fast Bake requires a fully prepared batch; regenerate it or disable Fast Bake")
            return False
        if not bpy.data.is_saved:
            self.report({'ERROR'}, "Save the .blend file before baking; lightmaps use a blend-relative path")
            return False
        if self.render_resolution < 1 or self.samples < 1 or self.margin < 0:
            self.report({'ERROR'}, "Resolution and samples must be positive; margin cannot be negative")
            return False
        candidate_objects = _batch_meshes(batch_collection)
        if not candidate_objects:
            self.report({'ERROR'}, f"'{batch_collection.name}' contains no render-visible, non-collision meshes")
            return False
        light_baked_count = 0
        for candidate in candidate_objects:
            if candidate.type != 'MESH' or candidate.data.uv_layers.get("LightMap") is None:
                self.report({'ERROR'}, f"'{candidate.name}' must be a mesh with a 'LightMap' UV layer")
                return False
            for slot in candidate.material_slots:
                mat = slot.material
                if mat and sum((mat.light_baked, mat.passthrough, mat.matfulltransparent)) > 1:
                    self.report({'ERROR'}, f"Material '{mat.name}' has conflicting Light Baking roles")
                    return False
                if mat and mat.light_baked:
                    light_baked_count += 1
                    if not mat.use_nodes or mat.node_tree is None:
                        self.report({'ERROR'}, f"Light-baked material '{mat.name}' must use nodes")
                        return False
                if mat and mat.passthrough and candidate.data.color_attributes.get("Color") is None:
                    self.report({'ERROR'}, f"Passthrough material '{mat.name}' requires a 'Color' attribute on '{candidate.name}'")
                    return False
        if light_baked_count == 0:
            self.report({'ERROR'}, "The selected batch has no materials marked Light Baked")
            return False
        return True

    def execute(self, context: Context):
        Preferences.save_dialog_settings(self, BAKE_DIALOG_SETTINGS)
        if not self.validate(context):
            return {'CANCELLED'}
        active_batch = context.view_layer.active_layer_collection.collection
        self._executing_batch = active_batch
        self._original_batch_baked = (BATCH_BAKED_KEY in active_batch, active_batch.get(BATCH_BAKED_KEY))
        self._original_batch_lightmap = (BATCH_LIGHTMAP_KEY in active_batch, active_batch.get(BATCH_LIGHTMAP_KEY))
        self._original_batch_noisy_lightmap = (
            BATCH_NOISY_LIGHTMAP_KEY in active_batch,
            active_batch.get(BATCH_NOISY_LIGHTMAP_KEY),
        )
        active_batch[BATCH_BAKED_KEY] = False
        if BATCH_LIGHTMAP_KEY in active_batch:
            del active_batch[BATCH_LIGHTMAP_KEY]
        self._materials_prepared = False
        self._saved_scene_settings = None
        self._baking = False
        self._original_active_object = context.view_layer.objects.active
        self._original_selected_objects = list(context.selected_objects)
        self._original_mode = context.object.mode if context.object else 'OBJECT'
        self._original_display_lighting = context.scene.display_lighting
        self._original_uv_states = {}
        self._original_lightmap_node_images = {}
        self._fast_temp_collection = None
        self._fast_temp_objects = []
        self._fast_temp_meshes = set()
        self._fast_original_visibility = {}
        self._fast_prepare_running = False
        self._previous_noisy_backup = None
        self._new_noisy_image = None
        return super().execute(context)

    def get_tasks(self):
        return [
            PipelineTask(self.make_export_collection),
            PipelineTask(self.get_batch),
            PipelineTask(self.set_render_settings),
            PipelineTask(self.set_quality_settings),
            PipelineTask(self.create_lightmap_image),
            PipelineTask(self.prepare_materials_for_baking),
            PipelineTask(
                self.start_fast_bake_preparation,
                poll=self.poll_fast_bake_preparation,
                deferred=False,
                timelog=True,
                label="Prepare Fast Bake Geometry",
            ),
            PipelineTask(self.set_normal_mode),
            PipelineTask(self.start_bake, poll=self.is_baking, timelog=True),
            PipelineTask(self.cleanup_fast_bake_geometry, label="Restore Separate Objects"),
            PipelineTask(self.save_noisy_lightmap, label="Save Noisy Lightmap"),
            PipelineTask(self.restore_materials),
            PipelineTask(self.set_lighting_mode),
        ]

    _baking: bool

    def start_bake(self, context: Context):
        self._baking = True
        self._bake_index = 0
        self._single_bake_running = False
        bpy.app.handlers.object_bake_complete.append(self.finished_bake)
        bpy.app.handlers.object_bake_cancel.append(self.cancelled_bake)
        self.report({'INFO'}, f"bake type: {context.scene.cycles.bake_type}")
        return self._start_next_object_bake(context)

    def _start_next_object_bake(self, context: Context):
        if self._bake_index >= len(self.bake_work_objects):
            self._baking = False
            self._remove_bake_handlers()
            return
        obj = self.bake_work_objects[self._bake_index]
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        context.scene.render.bake.use_clear = self._bake_index == 0
        self.set_pipeline_detail(
            f"Baking {obj.name}", self._bake_index + 1, len(self.bake_work_objects)
        )
        self._single_bake_running = True
        result = bpy.ops.object.bake('INVOKE_DEFAULT', type=context.scene.cycles.bake_type)
        if result == {'CANCELLED'}:
            self.cancelled_bake(None, None)
            return {'CANCELLED'}

    def is_baking(self):
        if self._baking and not self._single_bake_running:
            self._start_next_object_bake(bpy.context)
        return self._baking

    def finished_bake(self, obj, _):
        self._single_bake_running = False
        self._bake_index += 1
        if self._bake_index >= len(self.bake_work_objects):
            self._baking = False
            self._remove_bake_handlers()

    def cancelled_bake(self, obj, _):
        self._baking = False
        self._response = {'CANCELLED'}
        self.report({'WARNING'}, "Bake was cancelled; denoising was skipped")
        self._remove_bake_handlers()

    def _remove_bake_handlers(self):
        if self.finished_bake in bpy.app.handlers.object_bake_complete:
            bpy.app.handlers.object_bake_complete.remove(self.finished_bake)
        if self.cancelled_bake in bpy.app.handlers.object_bake_cancel:
            bpy.app.handlers.object_bake_cancel.remove(self.cancelled_bake)

    @staticmethod
    def _bake_visibility_key(obj: Object):
        return tuple(getattr(obj, name, None) for name in (
            "visible_camera",
            "visible_diffuse",
            "visible_glossy",
            "visible_transmission",
            "visible_volume_scatter",
            "visible_shadow",
            "is_shadow_catcher",
        ))

    def start_fast_bake_preparation(self, context: Context):
        self.bake_work_objects = list(self.bake_target_objects)
        ray_visibility_names = (
            "visible_diffuse",
            "visible_glossy",
            "visible_transmission",
            "visible_volume_scatter",
        )
        self._no_shadow_original_visibility = {}
        for obj in self.bake_target_objects:
            if obj.visible_shadow:
                continue
            saved = {}
            for name in ray_visibility_names:
                if hasattr(obj, name):
                    saved[name] = getattr(obj, name)
                    setattr(obj, name, False)
            self._no_shadow_original_visibility[obj] = saved
        if not self.fast_bake:
            self.set_pipeline_detail("Sequential mode", 1, 1)
            return

        stale = bpy.data.collections.get("__PVM_FAST_BAKE_TEMP")
        if stale is not None:
            DeleteCollection(stale.name)
        self._fast_temp_collection = bpy.data.collections.new("__PVM_FAST_BAKE_TEMP")
        self.export_collection.children.link(self._fast_temp_collection)
        self._fast_temp_objects = []
        self._fast_temp_meshes = set()
        self._fast_original_visibility = {
            obj: obj.hide_render for obj in self.bake_target_objects
        }
        self._fast_prepare_sources = list(self.bake_target_objects)
        self._fast_prepare_index = 0
        self._fast_join_groups = None
        self._fast_join_index = 0
        self._fast_prepare_started_at = time.perf_counter()
        self._fast_prepare_running = True
        bpy.ops.object.select_all(action='DESELECT')
        self.set_pipeline_detail(
            "Duplicating bake receivers", 0, len(self._fast_prepare_sources)
        )

    def poll_fast_bake_preparation(self):
        if not self._fast_prepare_running:
            return False

        context = bpy.context
        # Process multiple inexpensive receivers per modal tick, but yield often
        # enough to keep redraw and cancellation responsive. The previous
        # one-object-per-tick behavior added 20 ms of scheduler latency for
        # every receiver even when copying it was nearly instantaneous.
        prepare_deadline = time.perf_counter() + 0.008
        while self._fast_prepare_index < len(self._fast_prepare_sources):
            source = self._fast_prepare_sources[self._fast_prepare_index]
            current = self._fast_prepare_index + 1
            self.set_pipeline_detail(
                f"Copying receiver: {source.name}", current, len(self._fast_prepare_sources)
            )
            duplicate = source.copy()
            duplicate.data = source.data.copy()
            self._fast_temp_meshes.add(duplicate.data)
            duplicate.animation_data_clear()
            duplicate.parent = None
            duplicate.matrix_world = source.matrix_world.copy()
            duplicate.name = f"__PVM_BAKE_{source.name}"
            self._fast_temp_collection.objects.link(duplicate)
            self._fast_temp_objects.append(duplicate)

            if current > 1:
                self._fast_temp_objects[-2].select_set(False)
            duplicate.hide_set(False)
            duplicate.hide_viewport = False
            duplicate.hide_render = False
            duplicate.select_set(True)
            context.view_layer.objects.active = duplicate

            lightmap_uv = duplicate.data.uv_layers.get("LightMap")
            if lightmap_uv is None:
                raise RuntimeError(
                    f"Temporary bake copy of '{source.name}' has no LightMap UV"
                )
            duplicate.data.uv_layers.active = lightmap_uv
            lightmap_uv.active_render = True
            source.hide_render = True
            self._fast_prepare_index += 1
            if time.perf_counter() >= prepare_deadline:
                return True

        if self._fast_join_groups is None:
            grouped = {}
            for duplicate in self._fast_temp_objects:
                grouped.setdefault(self._bake_visibility_key(duplicate), []).append(duplicate)
            self._fast_join_groups = list(grouped.values())
            self._fast_joined_objects = []
            print(
                f"[LIGHTMAP] Fast bake: {len(self._fast_temp_objects)} receiver(s), "
                f"{len(self._fast_join_groups)} visibility group(s)"
            )

        if self._fast_join_index < len(self._fast_join_groups):
            group = self._fast_join_groups[self._fast_join_index]
            current = self._fast_join_index + 1
            self.set_pipeline_detail(
                "Joining compatible receivers", current, len(self._fast_join_groups)
            )
            bpy.ops.object.select_all(action='DESELECT')
            for duplicate in group:
                duplicate.select_set(True)
            active = group[0]
            context.view_layer.objects.active = active
            if len(group) > 1:
                result = bpy.ops.object.join()
                if result != {'FINISHED'}:
                    raise RuntimeError(
                        f"Could not join fast-bake visibility group {current}"
                    )
            active.name = f"__PVM_FAST_BAKE_GROUP_{current}"
            lightmap_uv = active.data.uv_layers.get("LightMap")
            if lightmap_uv is None:
                raise RuntimeError(
                    f"Joined fast-bake group {current} lost its LightMap UV"
                )
            active.data.uv_layers.active = lightmap_uv
            lightmap_uv.active_render = True
            self._fast_joined_objects.append(active)
            self._fast_join_index += 1
            return True

        self.bake_work_objects = list(self._fast_joined_objects)
        self._fast_prepare_running = False
        elapsed = time.perf_counter() - self._fast_prepare_started_at
        print(
            f"[LIGHTMAP] Fast bake geometry ready in {elapsed:.2f}s: "
            f"{len(self.bake_target_objects)} receiver(s) -> "
            f"{len(self.bake_work_objects)} bake object(s)"
        )
        self.set_pipeline_detail(
            "Fast bake geometry ready", len(self.bake_work_objects), len(self.bake_work_objects)
        )
        return False

    def cleanup_fast_bake_geometry(self):
        for obj, visibility in getattr(self, "_no_shadow_original_visibility", {}).items():
            try:
                if obj.name not in bpy.data.objects:
                    continue
                for name, value in visibility.items():
                    setattr(obj, name, value)
            except ReferenceError:
                pass
        self._no_shadow_original_visibility = {}
        for obj, hide_render in getattr(self, "_fast_original_visibility", {}).items():
            if obj.name in bpy.data.objects:
                obj.hide_render = hide_render
        self._fast_original_visibility = {}

        collection = getattr(self, "_fast_temp_collection", None)
        if collection is not None and collection.name in bpy.data.collections:
            DeleteCollection(collection.name)
        for mesh in getattr(self, "_fast_temp_meshes", set()):
            try:
                if mesh.name in bpy.data.meshes and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            except ReferenceError:
                pass
        self._fast_temp_collection = None
        self._fast_temp_objects = []
        self._fast_temp_meshes = set()
        self._fast_prepare_running = False

    def set_normal_mode(self, context: Context):
        context.scene.display_lighting = False

    def set_lighting_mode(self, context: Context):
        context.scene.display_lighting = True

    export_collection: Collection

    def make_export_collection(self):
        self.export_collection = bpy.data.collections.get('EXPORT_STUFF')
        if not self.export_collection:
            self.report({'ERROR'}, "EXPORT_STUFF no longer exists")
            return {'CANCELLED'}

    batch_collection: Collection
    bake_objects: list[Object]

    def get_batch(self, context: Context):
        self.batch_collection = context.view_layer.active_layer_collection.collection
        if self.export_collection.children.get(self.batch_collection.name) is not self.batch_collection:
            self.report({'ERROR'}, "The active collection is not a generated batch in EXPORT_STUFF")
            return {'CANCELLED'}
        self.bake_objects = _batch_meshes(self.batch_collection)
        if not self.bake_objects:
            self.report({'ERROR'}, f"'{self.batch_collection.name}' has no eligible meshes")
            return {'CANCELLED'}
        self.bake_target_objects = [
            obj for obj in self.bake_objects
            if any(slot.material and slot.material.light_baked for slot in obj.material_slots)
        ]
        if not self.bake_target_objects:
            self.report({'ERROR'}, f"'{self.batch_collection.name}' has no light-baked mesh targets")
            return {'CANCELLED'}

        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for obj in self.bake_objects:
            mesh = obj.data
            if mesh not in self._original_uv_states:
                self._original_uv_states[mesh] = (
                    mesh.uv_layers.active_index,
                    [layer.active_render for layer in mesh.uv_layers],
                )
            lightmap_uv = mesh.uv_layers.get("LightMap")
            mesh.uv_layers.active = lightmap_uv
            lightmap_uv.active_render = True
            obj.select_set(True)
        context.view_layer.objects.active = self.bake_objects[0]

    def set_render_settings(self, context: Context):
        scene = context.scene
        self._saved_scene_settings = {
            "use_compositing": scene.render.use_compositing,
            "resolution_x": scene.render.resolution_x,
            "resolution_y": scene.render.resolution_y,
            "resolution_percentage": scene.render.resolution_percentage,
            "pixel_aspect_x": scene.render.pixel_aspect_x,
            "pixel_aspect_y": scene.render.pixel_aspect_y,
            "filepath": scene.render.filepath,
            "file_format": scene.render.image_settings.file_format,
            "color_mode": scene.render.image_settings.color_mode,
            "color_depth": scene.render.image_settings.color_depth,
            "exr_codec": scene.render.image_settings.exr_codec,
            "color_management": scene.render.image_settings.color_management,
            "linear_colorspace": scene.render.image_settings.linear_colorspace_settings.name,
            "margin_type": scene.render.bake.margin_type,
            "margin": scene.render.bake.margin,
            "use_clear": scene.render.bake.use_clear,
            "use_pass_direct": scene.render.bake.use_pass_direct,
            "use_pass_indirect": scene.render.bake.use_pass_indirect,
            "samples": scene.cycles.samples,
            "use_adaptive_sampling": scene.cycles.use_adaptive_sampling,
            "adaptive_threshold": scene.cycles.adaptive_threshold,
            "sample_clamp_direct": scene.cycles.sample_clamp_direct,
            "sample_clamp_indirect": scene.cycles.sample_clamp_indirect,
            "blur_glossy": scene.cycles.blur_glossy,
            "max_bounces": scene.cycles.max_bounces,
            "diffuse_bounces": scene.cycles.diffuse_bounces,
            "glossy_bounces": scene.cycles.glossy_bounces,
            "transmission_bounces": scene.cycles.transmission_bounces,
            "transparent_max_bounces": scene.cycles.transparent_max_bounces,
            "light_sampling_threshold": scene.cycles.light_sampling_threshold,
        }
        os.makedirs(bpy.path.abspath("//Lightmaps"), exist_ok=True)
        context.scene.render.use_compositing = True
        context.scene.render.resolution_x = self.render_resolution
        context.scene.render.resolution_y = self.render_resolution
        context.scene.render.resolution_percentage = 100
        context.scene.render.pixel_aspect_x = 1
        context.scene.render.pixel_aspect_y = 1
        context.scene.render.image_settings.file_format = "OPEN_EXR"
        context.scene.render.image_settings.color_mode = "RGBA"
        context.scene.render.image_settings.color_depth = "32"
        context.scene.render.image_settings.exr_codec = "NONE"
        context.scene.render.image_settings.color_management = "OVERRIDE"
        context.scene.render.image_settings.linear_colorspace_settings.name = 'Non-Color'
        context.scene.update_render_engine()

    def set_quality_settings(self, context: Context):
        scene = context.scene
        scene.render.bake.margin_type = 'EXTEND' if self.margin == 0 else 'ADJACENT_FACES'
        scene.render.bake.margin = self.margin
        scene.render.bake.use_pass_direct = self.use_pass_direct
        scene.render.bake.use_pass_indirect = self.use_pass_indirect
        scene.cycles.samples = self.samples
        scene.cycles.use_adaptive_sampling = self.use_adaptive_sampling
        scene.cycles.adaptive_threshold = self.adaptive_threshold
        scene.cycles.sample_clamp_direct = self.clamp_direct
        scene.cycles.sample_clamp_indirect = self.clamp_indirect
        scene.cycles.blur_glossy = self.filter_glossy
        scene.cycles.max_bounces = self.max_bounces
        scene.cycles.diffuse_bounces = self.diffuse_bounces
        scene.cycles.glossy_bounces = self.glossy_bounces
        scene.cycles.transmission_bounces = self.transmission_bounces
        scene.cycles.transparent_max_bounces = self.transparent_bounces
        scene.cycles.light_sampling_threshold = self.light_sampling_threshold

    lightmap_image: Image

    def create_lightmap_image(self):
        self.lightmap_image = bpy.data.images.get("NoisyLightmap")
        if self.lightmap_image:
            bpy.data.images.remove(self.lightmap_image)
        self.lightmap_image = bpy.data.images.new(
            name="NoisyLightmap",
            width=self.render_resolution,
            height=self.render_resolution,
            alpha=True,
            is_data=True,
            float_buffer=True
        )
        self.lightmap_image.colorspace_settings.name = 'Non-Color'
        self.lightmap_image.update()

    img_nodes: list[ShaderNodeTexImage]
    original_materials: dict[Object, list[Material]]
    passthrough_mat: Material
    fulltransparent_mat: Material

    def prepare_materials_for_baking(self):
        self.img_nodes = []
        self.original_materials = {}
        self.batch_materials = {}
        self._materials_prepared = True

        # Create Fully Transparent Material
        self.fulltransparent_mat = bpy.data.materials.new("Fully Transparent Material")
        self.fulltransparent_mat.use_nodes = True
        fulltransparent_nodes = self.fulltransparent_mat.node_tree.nodes
        fulltransparent_links = self.fulltransparent_mat.node_tree.links
        fulltransparent_nodes.remove(fulltransparent_nodes["Principled BSDF"])

        transparent_shader = cast(ShaderNodeBsdfTransparent, fulltransparent_nodes.new(type="ShaderNodeBsdfTransparent"))
        transparent_shader.name = "Transparent BSDF"
        transparent_shader.location = (-200, 200)

        fulltransparent_links.new(transparent_shader.outputs[0], fulltransparent_nodes["Material Output"].inputs[0])

        # Create Passthrough Material
        self.passthrough_mat = bpy.data.materials.new("Passthrough Material")
        self.passthrough_mat.use_nodes = True
        passthrough_nodes = self.passthrough_mat.node_tree.nodes
        passthrough_links = self.passthrough_mat.node_tree.links
        passthrough_nodes.remove(passthrough_nodes["Principled BSDF"])

        vc_node = cast(ShaderNodeVertexColor, passthrough_nodes.new(type="ShaderNodeVertexColor"))
        vc_node.name = "Vertex Color"
        vc_node.location = (-400, 100)
        vc_node.layer_name = "Color"

        rayportal_shader = cast(ShaderNodeBsdfRayPortal, passthrough_nodes.new(type="ShaderNodeBsdfRayPortal"))
        rayportal_shader.name = "Ray Portal BSDF"
        rayportal_shader.location = (-200, 100)

        transparent_shader = cast(ShaderNodeBsdfTransparent, passthrough_nodes.new(type="ShaderNodeBsdfTransparent"))
        transparent_shader.name = "Transparent BSDF"
        transparent_shader.location = (-200, 200)
        passthrough_links.new(vc_node.outputs[0], rayportal_shader.inputs[0])
        passthrough_links.new(vc_node.outputs[0], transparent_shader.inputs[0])

        add_shader = cast(ShaderNodeAddShader, passthrough_nodes.new(type="ShaderNodeAddShader"))
        add_shader.name = "Add Shader"
        add_shader.location = (-25, 100)
        passthrough_links.new(transparent_shader.outputs[0], add_shader.inputs[0])
        passthrough_links.new(rayportal_shader.outputs[0], add_shader.inputs[1])

        passthrough_links.new(add_shader.outputs[0], passthrough_nodes["Material Output"].inputs[0])

        # Preparation
        for bake_object in self.bake_objects:
            self.original_materials[bake_object] = []
            for slot in bake_object.material_slots:
                if slot.material and slot.material.light_baked:
                    original_mat = slot.material
                    material_key = original_mat.as_pointer()
                    mat = self.batch_materials.get(material_key)
                    if mat is None:
                        batch_suffix = f"_Batch{_batch_number(self.batch_collection)}"
                        already_batch_material = (
                            original_mat.name.endswith(batch_suffix)
                            and original_mat.library is None
                            and original_mat.use_nodes
                            and original_mat.node_tree
                            and original_mat.node_tree.nodes.get("LightMapImageNode")
                        )
                        mat = original_mat if already_batch_material else original_mat.copy()
                        if not already_batch_material:
                            mat.name = f"{original_mat.name}{batch_suffix}"
                        self.batch_materials[material_key] = mat
                    if mat.node_tree is None:
                        self.report({'ERROR'}, f"Material '{mat.name}' has no node tree")
                        return {'CANCELLED'}
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links
                    for node in nodes:
                        node.select = False
                    img_node = cast(ShaderNodeTexImage, nodes.get("LightMapImageNode"))
                    if img_node:
                        self._original_lightmap_node_images.setdefault(img_node, img_node.image)
                        img_node.image = self.lightmap_image
                        img_node.interpolation = 'Closest'
                        img_node.select = True
                        nodes.active = img_node
                        self.img_nodes.append(img_node)
                    else:
                        uv_node = nodes.new(type="ShaderNodeUVMap")
                        uv_node.location = (-750, 350)
                        uv_node.uv_map = "LightMap"
                        img_node = cast(ShaderNodeTexImage, nodes.new(type="ShaderNodeTexImage"))
                        img_node.name = img_node.label = "LightMapImageNode"
                        img_node.image = self.lightmap_image
                        img_node.interpolation = 'Closest'
                        img_node.location = (-500, 500)
                        links.new(uv_node.outputs["UV"], img_node.inputs["Vector"])
                        img_node.select = True
                        nodes.active = img_node
                        self.img_nodes.append(img_node)
                    for link in list(links):
                        if img_node and link.from_node == img_node:
                            links.remove(link)
                    nodes.active = img_node
                    self.original_materials[bake_object].append(mat)
                    slot.material = mat
                elif slot.material and slot.material.passthrough:
                    self.original_materials[bake_object].append(slot.material)
                    slot.material = self.passthrough_mat
                elif slot.material and slot.material.matfulltransparent:
                    self.original_materials[bake_object].append(slot.material)
                    slot.material = self.fulltransparent_mat
                else:
                    self.original_materials[bake_object].append(slot.material)
                    slot.material = None

    def save_noisy_lightmap(self):
        i = _batch_number(self.batch_collection)
        filepath = f"//Lightmaps/LM_B{i}_Noisy.exr"
        absolute_path = bpy.path.abspath(filepath)
        if os.path.isfile(absolute_path):
            self._previous_noisy_backup = TemporaryDirectory(
                prefix="pvm_previous_noisy_", dir=os.path.dirname(absolute_path)
            )
            backup_path = os.path.join(self._previous_noisy_backup.name, "previous.exr")
            shutil.copy2(absolute_path, backup_path)
        width, height = self.lightmap_image.size
        pixels = np.empty(width * height * 4, dtype=np.float32)
        self.lightmap_image.pixels.foreach_get(pixels)
        save_pixels_exr_atomic(
            pixels.reshape((height, width, 4)), absolute_path, width
        )
        self._saved_noisy_filepath = filepath
        noisy_image = bpy.data.images.load(absolute_path, check_existing=True)
        noisy_image.reload()
        noisy_image.name = f"LM_B{i}_Noisy"
        noisy_image.colorspace_settings.name = 'Non-Color'
        self._new_noisy_image = noisy_image
        for img_node in self.img_nodes:
            img_node.image = noisy_image

    def restore_materials(self):
        for bake_object in getattr(self, "bake_objects", []):
            for i, slot in enumerate(bake_object.material_slots):
                materials = getattr(self, "original_materials", {}).get(bake_object, [])
                if i >= len(materials):
                    continue
                mat = materials[i]
                if not mat:
                    continue
                if not mat.use_nodes or mat.node_tree is None:
                    slot.material = mat
                    continue
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                img_node = nodes.get("LightMapImageNode")
                if not img_node:
                    slot.material = mat
                    continue
                for node in nodes:
                    lightmap_input = node.inputs.get("LightMap")
                    if lightmap_input:
                        links.new(img_node.outputs["Color"], lightmap_input)
                slot.material = mat
        if getattr(self, "passthrough_mat", None) and self.passthrough_mat.name in bpy.data.materials:
            bpy.data.materials.remove(self.passthrough_mat)
        if getattr(self, "fulltransparent_mat", None) and self.fulltransparent_mat.name in bpy.data.materials:
            bpy.data.materials.remove(self.fulltransparent_mat)
        self._materials_prepared = False

    def on_pipeline_finished(self, context: Context, cancelled: bool):
        self._remove_bake_handlers()
        self._baking = False
        self.cleanup_fast_bake_geometry()
        if cancelled and getattr(self, "_materials_prepared", False):
            self.restore_materials()
        if cancelled:
            for node, image in getattr(self, "_original_lightmap_node_images", {}).items():
                try:
                    node.image = image
                except ReferenceError:
                    pass
            backup = getattr(self, "_previous_noisy_backup", None)
            batch_for_path = getattr(self, "batch_collection", None) or getattr(
                self, "_executing_batch", None
            )
            noisy_output = (
                bpy.path.abspath(f"//Lightmaps/LM_B{_batch_number(batch_for_path)}_Noisy.exr")
                if batch_for_path else None
            )
            if backup is not None:
                backup_path = os.path.join(backup.name, "previous.exr")
                if noisy_output and os.path.isfile(backup_path):
                    os.replace(backup_path, noisy_output)
                    image = getattr(self, "_new_noisy_image", None)
                    if image is not None:
                        try:
                            image.reload()
                        except ReferenceError:
                            pass
            elif noisy_output and getattr(self, "_saved_noisy_filepath", None):
                if os.path.isfile(noisy_output):
                    os.remove(noisy_output)
        saved = getattr(self, "_saved_scene_settings", None)
        if saved:
            scene = context.scene
            scene.render.use_compositing = saved["use_compositing"]
            scene.render.resolution_x = saved["resolution_x"]
            scene.render.resolution_y = saved["resolution_y"]
            scene.render.resolution_percentage = saved["resolution_percentage"]
            scene.render.pixel_aspect_x = saved["pixel_aspect_x"]
            scene.render.pixel_aspect_y = saved["pixel_aspect_y"]
            scene.render.filepath = saved["filepath"]
            scene.render.image_settings.file_format = saved["file_format"]
            scene.render.image_settings.color_mode = saved["color_mode"]
            scene.render.image_settings.color_depth = saved["color_depth"]
            scene.render.image_settings.exr_codec = saved["exr_codec"]
            scene.render.image_settings.color_management = saved["color_management"]
            if saved["linear_colorspace"]:
                try:
                    scene.render.image_settings.linear_colorspace_settings.name = saved["linear_colorspace"]
                except TypeError:
                    self.report({'WARNING'}, "Previous output color space is no longer available")
            scene.render.bake.margin_type = saved["margin_type"]
            scene.render.bake.margin = saved["margin"]
            scene.render.bake.use_clear = saved["use_clear"]
            scene.render.bake.use_pass_direct = saved["use_pass_direct"]
            scene.render.bake.use_pass_indirect = saved["use_pass_indirect"]
            scene.cycles.samples = saved["samples"]
            scene.cycles.use_adaptive_sampling = saved["use_adaptive_sampling"]
            scene.cycles.adaptive_threshold = saved["adaptive_threshold"]
            scene.cycles.sample_clamp_direct = saved["sample_clamp_direct"]
            scene.cycles.sample_clamp_indirect = saved["sample_clamp_indirect"]
            scene.cycles.blur_glossy = saved["blur_glossy"]
            scene.cycles.max_bounces = saved["max_bounces"]
            scene.cycles.diffuse_bounces = saved["diffuse_bounces"]
            scene.cycles.glossy_bounces = saved["glossy_bounces"]
            scene.cycles.transmission_bounces = saved["transmission_bounces"]
            scene.cycles.transparent_max_bounces = saved["transparent_max_bounces"]
            scene.cycles.light_sampling_threshold = saved["light_sampling_threshold"]
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for mesh, (active_index, render_states) in getattr(self, "_original_uv_states", {}).items():
            if mesh.name not in bpy.data.meshes:
                continue
            if 0 <= active_index < len(mesh.uv_layers):
                mesh.uv_layers.active_index = active_index
            for index, was_render_active in enumerate(render_states):
                if index < len(mesh.uv_layers):
                    mesh.uv_layers[index].active_render = was_render_active
        bpy.ops.object.select_all(action='DESELECT')
        for obj in getattr(self, "_original_selected_objects", []):
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
        original_active = getattr(self, "_original_active_object", None)
        if original_active and original_active.name in context.view_layer.objects:
            context.view_layer.objects.active = original_active
            original_mode = getattr(self, "_original_mode", 'OBJECT')
            if original_mode != 'OBJECT':
                try:
                    bpy.ops.object.mode_set(mode=original_mode)
                except RuntimeError:
                    self.report({'WARNING'}, f"Could not restore {original_mode} mode")
        if cancelled:
            context.scene.display_lighting = getattr(self, "_original_display_lighting", context.scene.display_lighting)
            batch = getattr(self, "batch_collection", None) or getattr(self, "_executing_batch", None)
            if batch:
                baked_existed, baked_value = getattr(self, "_original_batch_baked", (False, None))
                lightmap_existed, lightmap_value = getattr(self, "_original_batch_lightmap", (False, None))
                noisy_existed, noisy_value = getattr(
                    self, "_original_batch_noisy_lightmap", (False, None)
                )
                if baked_existed:
                    batch[BATCH_BAKED_KEY] = baked_value
                elif BATCH_BAKED_KEY in batch:
                    del batch[BATCH_BAKED_KEY]
                if lightmap_existed:
                    batch[BATCH_LIGHTMAP_KEY] = lightmap_value
                elif BATCH_LIGHTMAP_KEY in batch:
                    del batch[BATCH_LIGHTMAP_KEY]
                if noisy_existed:
                    batch[BATCH_NOISY_LIGHTMAP_KEY] = noisy_value
                elif BATCH_NOISY_LIGHTMAP_KEY in batch:
                    del batch[BATCH_NOISY_LIGHTMAP_KEY]
        else:
            batch = getattr(self, "batch_collection", None)
            if batch:
                batch[BATCH_BAKED_KEY] = True
                noisy_path = getattr(
                    self,
                    "_saved_noisy_filepath",
                    f"//Lightmaps/LM_B{_batch_number(batch)}_Noisy.exr",
                )
                batch[BATCH_NOISY_LIGHTMAP_KEY] = noisy_path
                batch[BATCH_LIGHTMAP_KEY] = noisy_path
            for image in getattr(self, "_original_lightmap_node_images", {}).values():
                if image is None:
                    continue
                try:
                    if image.name in bpy.data.images and image.users == 0:
                        bpy.data.images.remove(image)
                except ReferenceError:
                    pass
        if cancelled:
            image = getattr(self, "_new_noisy_image", None)
            if image is not None:
                try:
                    if (
                        getattr(self, "_previous_noisy_backup", None) is None
                        and getattr(self, "_saved_noisy_filepath", None)
                    ):
                        if image.name in bpy.data.images and image.users == 0:
                            bpy.data.images.remove(image)
                except ReferenceError:
                    pass
        backup = getattr(self, "_previous_noisy_backup", None)
        if backup is not None:
            backup.cleanup()
            self._previous_noisy_backup = None
        if not cancelled:
            _send_windows_notification("PsychoVertexMaster", "Lightmap bake finished")


class DenoiseBatch(PipelineOperator):
    bl_idname = "lightmap.denoise_batch"
    bl_label = "Denoise Batch"
    bl_description = "Denoise the active batch's saved noisy lightmap"
    bl_options = {'REGISTER', "UNDO"}

    final_resolution: IntProperty(name="Final Resolution", default=1024, min=1)
    denoiser: EnumProperty(name="Denoiser", items=DENOISER_ITEMS, default='LIGHTMAP_OIDN')
    denoiser_step: EnumProperty(
        name="Denoiser Step",
        items=(
            ('BEFORE_DOWNSAMPLING', "Before Down Sampling", "Denoise at bake resolution, then downsample"),
            ('AFTER_DOWNSAMPLING', "After Down Sampling", "Downsample the noisy bake first, then denoise"),
        ),
        default='BEFORE_DOWNSAMPLING',
    )
    margin_mode: EnumProperty(
        name="Margin Handling",
        items=(
            ('NONE', "None (Bake Margins)", "Keep the original baked margins without rebuilding them"),
            ('INCLUDE', "Include", "Dilate the noisy texture before denoising"),
            ('EXCLUDE', "Exclude", "Denoise island texels, then dilate the final texture"),
        ),
        default='EXCLUDE',
    )
    margin: IntProperty(
        name="Margin Size",
        description="Dilation distance measured in final-output pixels",
        default=0,
        min=0,
    )

    def invoke(self, context, event):
        Preferences.load_dialog_settings(self, DENOISE_DIALOG_SETTINGS)
        if not self.validate(context, validate_settings=False):
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        batch = context.view_layer.active_layer_collection.collection
        layout.label(text=f"Batch: {batch.name}", icon='OUTLINER_COLLECTION')
        layout.prop(self, "final_resolution")
        layout.prop(self, "denoiser")
        layout.prop(self, "denoiser_step")
        margin_box = layout.box()
        margin_box.label(text="Margin", icon='MOD_EXPLODE')
        margin_box.prop(self, "margin_mode")
        if self.margin_mode != 'NONE':
            margin_box.prop(self, "margin")
        prefs = Preferences.get()
        if self.denoiser == 'LIGHTMAP_OIDN':
            path = Preferences.get_oidn_path(prefs)
            if not os.path.isfile(bpy.path.abspath(path)):
                layout.label(text="Configured oidnDenoise.exe was not found", icon='ERROR')
        elif self.denoiser == 'OPTIX':
            path = Preferences.get_optix_path(prefs)
            if not os.path.isfile(bpy.path.abspath(path)):
                layout.label(text="Configured Denoiser.exe was not found", icon='ERROR')

    def validate(self, context: Context, validate_settings=True):
        export = bpy.data.collections.get('EXPORT_STUFF')
        if export is None:
            self.report({'ERROR'}, "No EXPORT_STUFF collection; unpack and bake first")
            return False
        batch = context.view_layer.active_layer_collection.collection
        if export.children.get(batch.name) is not batch or _batch_number(batch) is None:
            self.report({'ERROR'}, "Make a generated Batch collection active in the Outliner")
            return False
        noisy_path = batch.get(BATCH_NOISY_LIGHTMAP_KEY)
        if not noisy_path:
            self.report({'ERROR'}, "This batch has no persistent noisy lightmap; rebake it first")
            return False
        absolute_path = bpy.path.abspath(noisy_path)
        if not os.path.isfile(absolute_path):
            self.report({'ERROR'}, f"Noisy lightmap was not found: {absolute_path}")
            return False
        if validate_settings and (self.final_resolution < 1 or self.margin < 0):
            self.report({'ERROR'}, "Final resolution must be positive; margin cannot be negative")
            return False
        has_target_node = False
        for obj in _batch_meshes(batch):
            for slot in obj.material_slots:
                material = slot.material
                if (
                    material and material.use_nodes and material.node_tree
                    and material.node_tree.nodes.get("LightMapImageNode")
                ):
                    has_target_node = True
                    break
            if has_target_node:
                break
        if not has_target_node:
            self.report({'ERROR'}, "The batch has no LightMapImageNode; rebake it first")
            return False
        return True

    def execute(self, context: Context):
        Preferences.save_dialog_settings(self, DENOISE_DIALOG_SETTINGS)
        if not self.validate(context):
            return {'CANCELLED'}
        self.batch_collection = context.view_layer.active_layer_collection.collection
        self._backend = None
        self._noisy_image = None
        self._denoised_pixels = None
        self._source_pixels = None
        self._coverage_masks = {}
        self._output_assigned = False
        self._previous_final_backup = None
        self._final_written = False
        return super().execute(context)

    def get_tasks(self):
        return [
            PipelineTask(self.load_noisy_lightmap),
            PipelineTask(self.prepare_denoiser),
            PipelineTask(self.start_denoiser, poll=self.poll_denoiser, timelog=True),
            PipelineTask(self.collect_denoised_pixels),
            PipelineTask(self.apply_output_margin),
            PipelineTask(self.save_final_lightmap),
            PipelineTask(self.assign_final_lightmap),
        ]

    def load_noisy_lightmap(self):
        path = bpy.path.abspath(self.batch_collection[BATCH_NOISY_LIGHTMAP_KEY])
        self._noisy_image = bpy.data.images.load(path, check_existing=True)
        self._noisy_image.reload()
        self._noisy_image.name = f"LM_B{_batch_number(self.batch_collection)}_Noisy"
        self._noisy_image.colorspace_settings.name = 'Non-Color'
        width, height = self._noisy_image.size
        if width != height:
            self.report({'ERROR'}, f"Noisy lightmap must be square, got {width}x{height}")
            return {'CANCELLED'}
        if self.final_resolution > width:
            self.report({'ERROR'}, "Final resolution cannot exceed the noisy bake resolution")
            return {'CANCELLED'}
        self.render_resolution = width
        pixels = np.empty(width * height * 4, dtype=np.float32)
        self._noisy_image.pixels.foreach_get(pixels)
        pixels = pixels.reshape((height, width, 4))
        if self.margin_mode != 'NONE':
            coverage = self._coverage_mask(width)
            pixels = crop_pixels_to_mask(pixels, coverage)
        if self.margin_mode == 'EXCLUDE':
            batch_number = _batch_number(self.batch_collection)
            mask_pixels = np.zeros((height, width, 4), dtype=np.float32)
            mask_pixels[:, :, :3] = coverage[:, :, None]
            mask_pixels[:, :, 3] = 1.0
            _publish_review_image(
                f"LM_B{batch_number}_UV_Coverage_{width}", mask_pixels
            )
            _publish_review_image(
                f"LM_B{batch_number}_Noisy_No_Margin_{width}", pixels
            )
        if not np.any(pixels[:, :, 3] > EPS):
            self.report({'ERROR'}, "No baked pixels overlap the current light-baked LightMap UV faces")
            return {'CANCELLED'}
        if self.denoiser_step == 'AFTER_DOWNSAMPLING':
            pixels = downsample_premultiplied(pixels, self.final_resolution)
            self.render_resolution = self.final_resolution
            if self.margin_mode != 'NONE':
                pixels = crop_pixels_to_mask(
                    pixels, self._coverage_mask(self.final_resolution)
                )
            if not np.any(pixels[:, :, 3] > EPS):
                self.report({'ERROR'}, "No LightMap UV coverage remains at the final resolution")
                return {'CANCELLED'}
        if self.margin_mode == 'INCLUDE':
            working_margin = self.margin
            if self.render_resolution != self.final_resolution:
                working_margin = math.ceil(
                    self.margin * self.render_resolution / self.final_resolution
                )
            pixels = dilate_rgba(pixels, working_margin)
        self._source_pixels = pixels

    def _coverage_mask(self, resolution: int) -> np.ndarray:
        mask = self._coverage_masks.get(resolution)
        if mask is None:
            mask = _lightmap_uv_coverage(
                _batch_meshes(self.batch_collection), resolution
            )
            self._coverage_masks[resolution] = mask
        return mask

    def prepare_denoiser(self, context: Context):
        prefs = Preferences.get()
        output_path = bpy.path.abspath(f"//Lightmaps/LM_B{_batch_number(self.batch_collection)}.exr")
        denoise_context = DenoiseContext(
            source_image=self._noisy_image,
            output_path=output_path,
            render_resolution=self.render_resolution,
            scene=context.scene,
            report=self.report,
            source_pixels=self._source_pixels,
            oidn_path=Preferences.get_oidn_path(prefs),
            optix_path=Preferences.get_optix_path(prefs),
        )
        self._backend = create_backend(self.denoiser, denoise_context)
        error = self._backend.validate()
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

    def start_denoiser(self):
        self._backend.start()

    def poll_denoiser(self):
        return self._backend.poll()

    def collect_denoised_pixels(self):
        self._denoised_pixels = self._backend.take_result_pixels()
        self._backend.cleanup()
        self._backend = None
        if self.denoiser_step == 'BEFORE_DOWNSAMPLING':
            self._denoised_pixels = downsample_premultiplied(
                self._denoised_pixels, self.final_resolution
            )

    def apply_output_margin(self):
        if self.margin_mode != 'EXCLUDE':
            return
        self._denoised_pixels = crop_pixels_to_mask(
            self._denoised_pixels,
            self._coverage_mask(self.final_resolution),
        )
        self._denoised_pixels = dilate_rgba(self._denoised_pixels, self.margin)

    def save_final_lightmap(self):
        self._final_filepath = f"//Lightmaps/LM_B{_batch_number(self.batch_collection)}.exr"
        absolute_path = bpy.path.abspath(self._final_filepath)
        if os.path.isfile(absolute_path):
            self._previous_final_backup = TemporaryDirectory(
                prefix="pvm_previous_final_", dir=os.path.dirname(absolute_path)
            )
            shutil.copy2(
                absolute_path,
                os.path.join(self._previous_final_backup.name, "previous.exr"),
            )
        save_pixels_exr_atomic(
            self._denoised_pixels,
            absolute_path,
            self.final_resolution,
        )
        self._final_written = True

    def assign_final_lightmap(self):
        image = bpy.data.images.load(
            bpy.path.abspath(self._final_filepath), check_existing=False
        )
        image.colorspace_settings.name = 'Non-Color'
        visited_materials = set()
        replaced_images = set()
        assigned = 0
        for obj in _batch_meshes(self.batch_collection):
            for slot in obj.material_slots:
                material = slot.material
                if material is None or material.as_pointer() in visited_materials:
                    continue
                visited_materials.add(material.as_pointer())
                if not material.use_nodes or material.node_tree is None:
                    continue
                node = material.node_tree.nodes.get("LightMapImageNode")
                if node is not None:
                    if node.image is not None and node.image != image:
                        replaced_images.add(node.image)
                    node.image = image
                    node.interpolation = 'Linear'
                    assigned += 1
        if assigned == 0:
            bpy.data.images.remove(image)
            self.report({'ERROR'}, "No batch LightMapImageNode could receive the final image")
            return {'CANCELLED'}
        self.batch_collection[BATCH_LIGHTMAP_KEY] = self._final_filepath
        self._output_assigned = True
        noisy_path = bpy.path.abspath(self.batch_collection[BATCH_NOISY_LIGHTMAP_KEY])
        for replaced in replaced_images:
            try:
                is_noisy_source = bool(
                    replaced.filepath
                    and bpy.path.abspath(replaced.filepath) == noisy_path
                )
                if (
                    not is_noisy_source
                    and replaced.name in bpy.data.images
                    and replaced.users == 0
                ):
                    bpy.data.images.remove(replaced)
            except ReferenceError:
                pass
        self.report({'INFO'}, f"Assigned final lightmap to {assigned} material(s)")

    def on_pipeline_finished(self, context: Context, cancelled: bool):
        backend = getattr(self, "_backend", None)
        if backend is not None:
            if cancelled:
                backend.cancel()
            else:
                backend.cleanup()
            self._backend = None
        # Leave the normally-owned LM_BN_Noisy datablock available for undo.
        # It intentionally has no fake user, so the user may delete or purge it.
        self._noisy_image = None
        self._denoised_pixels = None
        self._source_pixels = None
        self._coverage_masks = {}
        backup = getattr(self, "_previous_final_backup", None)
        if cancelled and getattr(self, "_final_written", False):
            output = bpy.path.abspath(getattr(self, "_final_filepath", ""))
            backup_path = os.path.join(backup.name, "previous.exr") if backup else ""
            if backup_path and os.path.isfile(backup_path):
                os.replace(backup_path, output)
            elif output and os.path.isfile(output):
                os.remove(output)
        if backup is not None:
            backup.cleanup()
            self._previous_final_backup = None
        if not cancelled:
            _send_windows_notification("PsychoVertexMaster", "Lightmap denoise finished")


class RepackActiveBatch(PipelineOperator):
    bl_idname = "lightmap.repack_active_batch"
    bl_label = "Repack Active"
    bl_description = "Repack the existing LightMap UVs of the active generated batch"
    bl_options = {'REGISTER', "UNDO", "UNDO_GROUPED"}

    uvMargin: IntProperty(
        name="UV Margin",
        description="Packing margin measured in pixels at the texture resolution",
        default=8,
        min=0,
    )
    textureSize: IntProperty(
        name="Texture Resolution",
        description="Target resolution used for pixel margins and pixel-perfect alignment",
        default=4096,
        min=16,
    )
    pixelPerfect: BoolProperty(
        name="Pixel-Perfect Packing",
        description="Align packed island bounds to the target texture-resolution grid",
        default=True,
    )
    heuristicDuration: IntProperty(
        name="Heuristic Duration",
        description="Seconds UVPackmaster spends searching for a better layout",
        default=10,
        min=1,
        max=3600,
    )

    def invoke(self, context, event):
        Preferences.load_dialog_settings(self, REPACK_DIALOG_SETTINGS)
        if not self.validate(context):
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.label(
            text=f"Batch: {context.view_layer.active_layer_collection.collection.name}",
            icon='OUTLINER_COLLECTION',
        )
        layout.prop(self, "textureSize")
        layout.prop(self, "uvMargin")
        layout.prop(self, "pixelPerfect")
        layout.prop(self, "heuristicDuration")

    def validate(self, context: Context):
        export = bpy.data.collections.get("EXPORT_STUFF")
        batch = context.view_layer.active_layer_collection.collection
        if export is None or _batch_number(batch) is None or export.children.get(batch.name) is not batch:
            self.report({'ERROR'}, "Make a generated Batch collection active in the Outliner")
            return False
        if not _has_uvpackmaster(context):
            self.report({'ERROR'}, "UVPackmaster 3 is required and must be enabled")
            return False
        if self.pixelPerfect and not hasattr(context.scene.uvpm3_props, "pixel_perfect_align"):
            self.report({'ERROR'}, "Pixel-perfect packing requires UVPackmaster 3.2.6 or newer")
            return False
        meshes = _batch_meshes(batch)
        if not meshes:
            self.report({'ERROR'}, f"'{batch.name}' contains no render-visible, non-collision meshes")
            return False
        missing_uv = [obj.name for obj in meshes if obj.data.uv_layers.get("LightMap") is None]
        if missing_uv:
            self.report({'ERROR'}, "Missing 'LightMap' UV layer: " + ", ".join(missing_uv[:4]))
            return False
        return True

    def execute(self, context: Context):
        Preferences.save_dialog_settings(self, REPACK_DIALOG_SETTINGS)
        if not self.validate(context):
            return {'CANCELLED'}
        self.batch_collection = context.view_layer.active_layer_collection.collection
        self._pack_announced = False
        return super().execute(context)

    def get_tasks(self):
        return [PipelineTask(
            self.start_pack,
            poll=self.poll_pack,
            deferred=False,
            timelog=True,
            label="Repack Active Lightmap UVs",
        )]

    def start_pack(self):
        _deselect_selected(bpy.context)
        self._pack_announced = False
        self.set_pipeline_detail(self.batch_collection.name, 0, 1)

    def poll_pack(self):
        meshes = _batch_meshes(self.batch_collection)
        if not self._pack_announced:
            self._pack_announced = True
            self.set_pipeline_detail(
                f"{self.batch_collection.name}: {len(meshes)} mesh objects", 0, 1
            )
            return True
        for obj in meshes:
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            obj.select_set(True)
        bpy.context.view_layer.objects.active = meshes[0]
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            result = UVPack_Scaled(
                bpy.context,
                self,
                meshes,
                True,
                self.uvMargin,
                self.textureSize,
                self.heuristicDuration,
                self.pixelPerfect,
            )
        finally:
            if bpy.context.object and bpy.context.object.mode == 'EDIT':
                bpy.ops.object.mode_set(mode="OBJECT")
        if result == {'CANCELLED'}:
            self._response = result
            return False
        for obj in meshes:
            obj.select_set(False)
        self.set_pipeline_detail("Repacked active batch", 1, 1)
        self.report({'INFO'}, f"Repacked {self.batch_collection.name}")
        return False


class _UnpackBase(PipelineOperator):
    """Shared implementation for active-collection unpacking."""

    uvMargin: IntProperty(
        name="UV Margin",
        description="Packing margin measured in pixels at the texture resolution",
        default=8,
        min=0,
    )
    textureSize: IntProperty(
        name="Texture Resolution",
        description="Target resolution used for pixel margins and pixel-perfect alignment",
        default=4096,
        min=1,
    )
    packLightmaps: BoolProperty(
        name="Pack Lightmaps",
        description="Pack all eligible meshes in each batch into one shared LightMap UV space",
        default=True,
    )
    pixelPerfect: BoolProperty(
        name="Pixel-Perfect Packing",
        description="Align packed island bounds to the target texture-resolution grid",
        default=True,
    )
    heuristicDuration: IntProperty(
        name="Heuristic Duration",
        description="Seconds UVPackmaster spends searching for a better layout for each batch",
        default=10,
        min=1,
        max=3600,
    )
    preparationErrorPolicy: EnumProperty(
        name="On Preparation Error",
        description="Choose whether an object preparation failure cancels unpacking",
        items=(
            ('CANCEL', "Cancel", "Discard staged output and preserve existing batches"),
            ('IGNORE', "Ignore", "Keep the failed object unchanged and continue"),
        ),
        default='CANCEL',
    )

    def invoke(self, context, event):
        Preferences.load_dialog_settings(self, PACK_DIALOG_SETTINGS)
        if not self.validate(context):
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "textureSize")
        layout.prop(self, "uvMargin")
        layout.prop(self, "packLightmaps")
        pixel_row = layout.row()
        pixel_row.enabled = self.packLightmaps
        pixel_row.prop(self, "pixelPerfect")
        heuristic_row = layout.row()
        heuristic_row.enabled = self.packLightmaps
        heuristic_row.prop(self, "heuristicDuration")
        layout.prop(self, "preparationErrorPolicy")

    def validate(self, context: Context):
        source = bpy.data.collections.get('SOURCE')
        if source is None:
            self.report({'ERROR'}, "Create a top-level collection named 'SOURCE'")
            return False
        if GetLayerCollection("SOURCE", context.view_layer.layer_collection) is None:
            self.report({'ERROR'}, "SOURCE is not present in the active View Layer")
            return False
        if not source.children:
            self.report({'ERROR'}, "SOURCE has no child collections to turn into batches")
            return False
        if self.packLightmaps and not _has_uvpackmaster(context):
            self.report({'ERROR'}, "UVPackmaster 3 is required and must be enabled")
            return False
        if (self.packLightmaps and self.pixelPerfect
                and not hasattr(context.scene.uvpm3_props, "pixel_perfect_align")):
            self.report({'ERROR'}, "Pixel-perfect packing requires UVPackmaster 3.2.6 or newer")
            return False
        if bpy.data.collections.get("TEMP_EXPORT_STUFF"):
            self.report({'ERROR'}, "TEMP_EXPORT_STUFF remains from an interrupted run; clear lightmapping data first")
            return False
        empty_batches = [collection.name for collection in source.children if not collection.all_objects]
        if empty_batches:
            self.report({'ERROR'}, "No objects found in batch collection(s): " + ", ".join(empty_batches[:4]))
            return False
        return True

    def execute(self, context: Context):
        Preferences.save_dialog_settings(self, PACK_DIALOG_SETTINGS)
        if not self.validate(context):
            return {'CANCELLED'}
        return super().execute(context)

    def get_tasks(self):
        tasks = [
            PipelineTask(self.get_source, label="Inspect Source"),
            PipelineTask(self.realize_instances, poll=self.is_realizing_instances, deferred=False, timelog=True,
                         label="Build Batch Collections"),
            PipelineTask(self.prepare_batches, poll=self.is_preparing_batches, deferred=False, timelog=True,
                         label="Prepare UE5 Geometry"),
        ]
        if self.packLightmaps:
            tasks.append(
                PipelineTask(self.pack_uvs, poll=self.is_packing_uvs, deferred=False, timelog=True,
                             label="Pack Shared Lightmap UVs")
            )
        return tasks

    source_collection: Collection

    def get_source(self):
        self.source_collection = bpy.data.collections.get('SOURCE')
        if not self.source_collection:
            self.report({'ERROR'}, "SOURCE no longer exists")
            return {'CANCELLED'}
        export = GetOrCreateCollection("EXPORT_STUFF")
        all_sources = list(self.source_collection.children)
        existing = {
            source.as_pointer(): _batch_for_source(export, source)
            for source in all_sources
        }
        for source in all_sources:
            batch = existing[source.as_pointer()]
            if batch is not None and BATCH_SOURCE_COLLECTION_KEY not in batch:
                batch[BATCH_SOURCE_COLLECTION_KEY] = source.name
        self.source_batches = [
            source for source in all_sources if existing[source.as_pointer()] is None
        ]
        used_numbers = {
            number for number in (_batch_number(batch) for batch in export.children)
            if number is not None
        }
        generated_names = {}
        final_names = {}
        next_number = 0
        for source in self.source_batches:
            while (
                next_number in used_numbers
                or bpy.data.collections.get(_batch_collection_name(next_number, source)) is not None
            ):
                next_number += 1
            final_name = _batch_collection_name(next_number, source)
            pending_name = f"__PVM_PENDING_{final_name}"
            stale = bpy.data.collections.get(pending_name)
            if stale is not None:
                DeleteCollection(stale.name)
            generated_names[source.as_pointer()] = pending_name
            final_names[source.as_pointer()] = final_name
            used_numbers.add(next_number)
            next_number += 1
        self._generated_batch_names = generated_names
        self._pending_batch_final_names = final_names
        self._existing_batch_count = len(all_sources) - len(self.source_batches)
        self.set_pipeline_detail(
            f"{len(self.source_batches)} missing; {self._existing_batch_count} already generated",
            len(all_sources),
            len(all_sources),
        )

    export_collection: Collection
    batches: list[Collection]

    def realize_instances(self, context: Context):
        """Prepare cooperative realization; one SOURCE child is handled per poll."""
        obj = GetTempActiveObj(self.source_collection)
        bpy.ops.object.mode_set(mode="OBJECT")
        ReleaseTempActiveObj(obj)
        self.export_collection = GetOrCreateCollection("EXPORT_STUFF")
        self.batches = []
        self._realize_jobs = []
        self._realize_index = 0
        self._realize_announced = False
        self._realize_finished = False
        self._collision_visibility_cache = {}
        self._batch_group_matrices = {}
        for collection in self.source_batches:
            batch_collection = bpy.data.collections.new(
                self._generated_batch_names[collection.as_pointer()]
            )
            batch_collection[BATCH_SOURCE_COLLECTION_KEY] = collection.name
            self.export_collection.children.link(batch_collection)
            collection_objects = list(collection.all_objects)
            collection_object_names = {obj.name for obj in collection_objects}
            source_roots = [obj for obj in collection_objects if obj.parent not in collection_objects]
            self.batches.append(batch_collection)
            self._realize_jobs.append((batch_collection, source_roots, collection_object_names))
        self._batch_instance_indices = {batch.as_pointer(): 0 for batch in self.batches}
        self.set_pipeline_detail("Queued object hierarchies", 0, len(self._realize_jobs))

    def is_realizing_instances(self):
        total = len(self._realize_jobs)
        if self._realize_index < total:
            batch_collection, source_roots, collection_object_names = self._realize_jobs[self._realize_index]
            if not self._realize_announced:
                self.set_pipeline_detail(
                    f"{batch_collection.name}: {len(source_roots)} root hierarchies",
                    self._realize_index + 1,
                    total,
                )
                print(f"[UNPACK] Batched realization -> {batch_collection.name}: "
                      f"{len(source_roots)} roots ({self._realize_index + 1}/{total})")
                self._realize_announced = True
                return True
            result = self._realize_batch(batch_collection, source_roots, collection_object_names)
            if result == {'CANCELLED'}:
                self._response = result
                return False
            self._realize_index += 1
            self._realize_announced = False
            return True
        if not self._realize_finished:
            for batch_collection in self.batches:
                if not batch_collection.objects:
                    self.report({'ERROR'}, f"'{batch_collection.name}' produced no objects")
                    self._response = {'CANCELLED'}
                    return False
                print(f"[UNPACK] {batch_collection.name}: {len(batch_collection.objects)} objects")
            self._finish_realization()
            self._realize_finished = True
        return False

    def _realize_batch(self, batch_collection, source_roots, collection_object_names):
        """Fast path: duplicate and realize every hierarchy in one operator batch."""
        started = time.perf_counter()
        try:
            result = self._realize_batch_fast(batch_collection, source_roots, collection_object_names)
            if result == {'CANCELLED'}:
                raise RuntimeError("batched ownership validation failed")
            print(f"[UNPACK] {batch_collection.name} batched path completed in "
                  f"{time.perf_counter() - started:.2f}s")
            return result
        except Exception as ex:
            print(f"[UNPACK] {batch_collection.name} batched path unavailable: {ex}")
            print(f"[UNPACK] Rebuilding {batch_collection.name} with compatibility path")
            self.report({'WARNING'}, f"{batch_collection.name}: using compatibility realization")
            self._clear_generated_batch(batch_collection)
            self._batch_instance_indices[batch_collection.as_pointer()] = 0
            for index, source_root in enumerate(source_roots, 1):
                self.set_pipeline_detail(
                    f"{batch_collection.name} fallback: {source_root.name}",
                    index,
                    len(source_roots),
                )
                result = self._realize_one(batch_collection, source_root, collection_object_names)
                if result == {'CANCELLED'}:
                    return result
            return {'FINISHED'}

    def _clear_generated_batch(self, batch_collection):
        for obj in list(batch_collection.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for obj in getattr(self, "_batched_generated_objects", []):
            try:
                if bpy.data.objects.get(obj.name) is obj:
                    bpy.data.objects.remove(obj, do_unlink=True)
            except ReferenceError:
                pass
        self._batched_generated_objects = []

    def _realize_batch_fast(self, batch_collection, source_roots, collection_object_names):
        phase_start = time.perf_counter()
        _deselect_selected(bpy.context)
        for source_root in source_roots:
            source_root.select_set(True)
            for child in source_root.children_recursive:
                if child.name in collection_object_names:
                    child.select_set(True)
        if not bpy.context.selected_objects:
            self.report({'ERROR'}, f"'{batch_collection.name}' has no selectable source objects")
            return {'CANCELLED'}
        bpy.context.view_layer.objects.active = source_roots[0]
        if bpy.ops.object.duplicate() == {'CANCELLED'}:
            return {'CANCELLED'}
        generated_objects = list(bpy.context.selected_objects)
        self._batched_generated_objects = list(generated_objects)
        generated_ids = {obj.as_pointer() for obj in generated_objects}
        print(f"[UNPACK] {batch_collection.name} duplicate: "
              f"{len(generated_objects)} objects in {time.perf_counter() - phase_start:.2f}s")

        phase_start = time.perf_counter()
        for generated_obj in generated_objects:
            if generated_obj.parent and generated_obj.parent.as_pointer() not in generated_ids:
                world_matrix = generated_obj.matrix_world.copy()
                generated_obj.parent = None
                generated_obj.matrix_world = world_matrix
            for owner in list(generated_obj.users_collection):
                owner.objects.unlink(generated_obj)
            batch_collection.objects.link(generated_obj)

        top_records = {}
        pending_anchors = []
        batch_key = batch_collection.as_pointer()
        for generated_obj in generated_objects:
            if generated_obj.instance_collection is None:
                continue
            group_id = f"{batch_collection.name}:{self._batch_instance_indices[batch_key]}"
            self._batch_instance_indices[batch_key] += 1
            record = {
                "anchor": generated_obj,
                "asset_name": generated_obj.instance_collection.name,
                "matrix": generated_obj.matrix_world.copy(),
                "group_id": group_id,
            }
            self._batch_group_matrices[(batch_key, group_id)] = record["matrix"]
            top_records[generated_obj.as_pointer()] = record
            pending_anchors.append((generated_obj, record))

        generation = 0
        while pending_anchors:
            generation += 1
            if generation > 64:
                raise RuntimeError("nested instance depth exceeded 64 generations")
            next_anchors = self._realize_anchor_generation(
                batch_collection, pending_anchors
            )
            pending_anchors = next_anchors

        for record in top_records.values():
            anchor = record["anchor"]
            if not anchor.get(FILLER_GROUP_KEY):
                raise RuntimeError(f"instance '{anchor.name}' produced no mapped group")
        _deselect_selected(bpy.context)
        print(f"[UNPACK] {batch_collection.name} realize/link: "
              f"{len(top_records)} instances in {time.perf_counter() - phase_start:.2f}s")
        self._batched_generated_objects = []
        return {'FINISHED'}

    def _realize_anchor_generation(self, batch_collection, anchor_records):
        generation_started = time.perf_counter()
        anchors = [anchor for anchor, _record in anchor_records]
        existing_descendants = {
            anchor.as_pointer(): {obj.as_pointer() for obj in anchor.children_recursive}
            for anchor in anchors
        }
        visibility_collections = []
        visibility_objects = []
        seen_collections = set()
        seen_objects = set()
        for anchor in anchors:
            instance_collection = anchor.instance_collection
            if instance_collection is None:
                continue
            cache_key = instance_collection.as_pointer()
            members = self._collision_visibility_cache.get(cache_key)
            if members is None:
                members = _instance_collision_members(instance_collection)
                self._collision_visibility_cache[cache_key] = members
            for collection in members[0]:
                pointer = collection.as_pointer()
                if pointer not in seen_collections:
                    seen_collections.add(pointer)
                    visibility_collections.append(collection)
            for obj in members[1]:
                pointer = obj.as_pointer()
                if pointer not in seen_objects:
                    seen_objects.add(pointer)
                    visibility_objects.append(obj)

        _deselect_selected(bpy.context)
        for anchor in anchors:
            anchor.select_set(True)
        bpy.context.view_layer.objects.active = anchors[0]
        visibility_states = _temporarily_show_instance_collisions(
            anchors[0].instance_collection,
            (visibility_collections, visibility_objects),
        )
        try:
            result = bpy.ops.object.duplicates_make_real(use_base_parent=True, use_hierarchy=True)
        finally:
            _restore_instance_visibility(visibility_states)
        if result == {'CANCELLED'}:
            return []
        realized_at = time.perf_counter()

        anchor_by_pointer = {anchor.as_pointer(): anchor for anchor in anchors}
        preexisting = set().union(*existing_descendants.values())
        created_by_anchor = {pointer: [] for pointer in anchor_by_pointer}
        candidates = {}
        for anchor in anchors:
            for obj in anchor.children_recursive:
                pointer = obj.as_pointer()
                if pointer not in preexisting:
                    candidates[pointer] = obj
        for obj in candidates.values():
            owner = obj.parent
            while owner is not None and owner.as_pointer() not in anchor_by_pointer:
                owner = owner.parent
            if owner is None:
                raise RuntimeError(f"object '{obj.name}' has no instance owner")
            created_by_anchor[owner.as_pointer()].append(obj)
        for anchor in anchors:
            if not created_by_anchor[anchor.as_pointer()]:
                raise RuntimeError(f"instance '{anchor.name}' produced no descendants")
        all_created = list(candidates.values())
        self._batched_generated_objects.extend(all_created)
        for anchor in anchors:
            anchor.instance_type = 'NONE'
            anchor.instance_collection = None

        _deselect_selected(bpy.context)
        for obj in anchors + all_created:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = anchors[0]
        bpy.ops.object.make_local(type='SELECT_OBDATA')
        localized_at = time.perf_counter()

        next_anchors = []
        assigned = set()
        for anchor, record in anchor_records:
            group_objects = [anchor] + created_by_anchor[anchor.as_pointer()]
            inverse = record["matrix"].inverted_safe()
            for realized_obj in group_objects:
                pointer = realized_obj.as_pointer()
                if pointer in assigned:
                    raise RuntimeError(f"object '{realized_obj.name}' mapped to multiple instances")
                assigned.add(pointer)
                realized_obj[FILLER_ASSET_KEY] = record["asset_name"]
                realized_obj[FILLER_GROUP_KEY] = record["group_id"]
                realized_obj[FILLER_RELATIVE_MATRIX_KEY] = _matrix_to_property(
                    inverse @ realized_obj.matrix_world
                )
                if realized_obj.name.startswith(COLLISION_PREFIXES):
                    realized_obj.hide_viewport = True
                    realized_obj.hide_render = True
                for owner in list(realized_obj.users_collection):
                    owner.objects.unlink(realized_obj)
                batch_collection.objects.link(realized_obj)
                if realized_obj.instance_collection is not None:
                    next_anchors.append((realized_obj, record))
                realized_obj.select_set(False)
        finished_at = time.perf_counter()
        print(
            f"[UNPACK] {batch_collection.name} generation: {len(anchors)} anchors, "
            f"{len(all_created)} objects | realize {realized_at - generation_started:.2f}s | "
            f"localize {localized_at - realized_at:.2f}s | "
            f"metadata/link {finished_at - localized_at:.2f}s"
        )
        return next_anchors

    def _realize_one(self, batch_collection, obj, collection_object_names):
        _deselect_selected(bpy.context)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        for child in obj.children_recursive:
            if child.name in collection_object_names:
                child.select_set(True)
        if bpy.ops.object.duplicate() == {'CANCELLED'}:
            self.report({'ERROR'}, f"Could not duplicate object hierarchy '{obj.name}'")
            return {'CANCELLED'}
        generated_objects = list(bpy.context.selected_objects)
        if not generated_objects:
            self.report({'ERROR'}, f"'{obj.name}' produced no generated objects")
            return {'CANCELLED'}
        generated_ids = {generated_obj.as_pointer() for generated_obj in generated_objects}
        for generated_obj in generated_objects:
            if generated_obj.parent and generated_obj.parent.as_pointer() not in generated_ids:
                world_matrix = generated_obj.matrix_world.copy()
                generated_obj.parent = None
                generated_obj.matrix_world = world_matrix
            for owner in list(generated_obj.users_collection):
                owner.objects.unlink(generated_obj)
            batch_collection.objects.link(generated_obj)
        for generated_obj in list(generated_objects):
            if generated_obj.instance_collection is None:
                continue
            asset_name = generated_obj.instance_collection.name
            instance_matrix = generated_obj.matrix_world.copy()
            batch_key = batch_collection.as_pointer()
            group_id = f"{batch_collection.name}:{self._batch_instance_indices[batch_key]}"
            self._batch_instance_indices[batch_key] += 1
            self._batch_group_matrices[(batch_key, group_id)] = instance_matrix
            _deselect_selected(bpy.context)
            generated_obj.select_set(True)
            bpy.context.view_layer.objects.active = generated_obj
            instance_collection = generated_obj.instance_collection
            cache_key = instance_collection.as_pointer()
            visibility_members = self._collision_visibility_cache.get(cache_key)
            if visibility_members is None:
                visibility_members = _instance_collision_members(instance_collection)
                self._collision_visibility_cache[cache_key] = visibility_members
            visibility_states = _temporarily_show_instance_collisions(
                instance_collection, visibility_members
            )
            try:
                realize_result = bpy.ops.object.duplicates_make_real(use_base_parent=True, use_hierarchy=True)
            finally:
                _restore_instance_visibility(visibility_states)
            if realize_result == {'CANCELLED'}:
                self.report({'ERROR'}, f"Could not realize instance '{generated_obj.name}'")
                return {'CANCELLED'}
            bpy.ops.object.make_local(type='SELECT_OBDATA')
            generated_obj.instance_type = 'NONE'
            generated_obj.instance_collection = None
            realized_group = list(bpy.context.selected_objects)
            if generated_obj not in realized_group:
                realized_group.insert(0, generated_obj)
            inverse_instance_matrix = instance_matrix.inverted_safe()
            for realized_obj in realized_group:
                realized_obj[FILLER_ASSET_KEY] = asset_name
                realized_obj[FILLER_GROUP_KEY] = group_id
                realized_obj[FILLER_RELATIVE_MATRIX_KEY] = _matrix_to_property(
                    inverse_instance_matrix @ realized_obj.matrix_world
                )
                if realized_obj.name.startswith(COLLISION_PREFIXES):
                    realized_obj.hide_viewport = True
                    realized_obj.hide_render = True
                for owner in list(realized_obj.users_collection):
                    owner.objects.unlink(realized_obj)
                batch_collection.objects.link(realized_obj)
                realized_obj.select_set(False)
        _deselect_selected(bpy.context)

    @staticmethod
    def _hierarchy_depth(obj):
        depth = 0
        parent = obj.parent
        while parent is not None:
            depth += 1
            parent = parent.parent
        return depth

    def prepare_batches(self, context: Context):
        self._prepare_jobs = []
        self._prepare_failures = {batch.as_pointer(): [] for batch in self.batches}
        for batch in self.batches:
            objects = sorted(list(batch.objects), key=self._hierarchy_depth)
            self._prepare_jobs.extend((batch, obj) for obj in objects)
            batch[BATCH_IMPORT_READY_KEY] = True
            batch[BATCH_PREPARATION_FAILURE_COUNT_KEY] = 0
            batch[BATCH_PREPARATION_FAILURES_KEY] = ""
        self._prepare_index = 0
        self._prepare_failed_objects = set()
        self._prepare_metadata_finished = False
        self.set_pipeline_detail("Queued generated objects", 0, len(self._prepare_jobs))

    def _prepare_object_transactionally(self, context: Context, obj: Object):
        if obj.type == 'LIGHT':
            # Applying object scale is unsupported for light datablocks. Keep the
            # duplicated light transform intact; its data already carries the
            # actual point/spot/area-light settings.
            return

        determinant = obj.matrix_world.to_3x3().determinant()
        if abs(determinant) <= EPS:
            raise RuntimeError("effective world scale has a zero axis")
        mirrored = determinant < 0.0
        child_world = {child: child.matrix_world.copy() for child in obj.children}
        owners = list(obj.users_collection)
        if not owners:
            raise RuntimeError("object is not linked to a collection")

        working = obj.copy()
        if obj.data is not None:
            working.data = obj.data.copy()
        working.animation_data_clear()
        owners[0].objects.link(working)
        working.parent = obj.parent
        working.matrix_parent_inverse = obj.matrix_parent_inverse.copy()
        working.matrix_world = obj.matrix_world.copy()
        try:
            _deselect_selected(context)
            working.hide_set(False)
            working.hide_viewport = False
            working.select_set(True)
            context.view_layer.objects.active = working
            for modifier in list(working.modifiers):
                modifier_name = modifier.name
                try:
                    result = bpy.ops.object.modifier_apply(modifier=modifier_name)
                except Exception as ex:
                    raise RuntimeError(f"modifier '{modifier_name}' failed: {ex}") from ex
                if result != {'FINISHED'}:
                    raise RuntimeError(f"modifier '{modifier_name}' could not be applied")

            result = bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            if result != {'FINISHED'}:
                raise RuntimeError("scale could not be applied")
            if any(abs(component - 1.0) > EPS for component in working.scale):
                raise RuntimeError("scale could not be fully normalized")

            if mirrored and working.type == 'MESH':
                mesh_bm = bmesh.new()
                try:
                    mesh_bm.from_mesh(working.data)
                    bmesh.ops.reverse_faces(mesh_bm, faces=list(mesh_bm.faces))
                    mesh_bm.to_mesh(working.data)
                    working.data.update()
                finally:
                    mesh_bm.free()

            if _is_bake_mesh(working) and working.data.uv_layers.get("LightMap") is None:
                raise RuntimeError("applied modifiers removed the LightMap UV layer")

            prepared_data = working.data
            if prepared_data is not None:
                obj.data = prepared_data
            obj.modifiers.clear()
            obj.matrix_world = working.matrix_world.copy()
            for child, matrix in child_world.items():
                child.matrix_world = matrix
        finally:
            working_data = working.data
            bpy.data.objects.remove(working, do_unlink=True)
            if working_data is not None and working_data.users == 0:
                bpy.data.batch_remove(ids=(working_data,))
            _deselect_selected(context)

    def _refresh_filler_relative_matrices(self, batch: Collection):
        batch_key = batch.as_pointer()
        for obj in batch.objects:
            group_id = obj.get(FILLER_GROUP_KEY)
            if not group_id:
                continue
            anchor_matrix = self._batch_group_matrices.get((batch_key, group_id))
            if anchor_matrix is not None:
                obj[FILLER_RELATIVE_MATRIX_KEY] = _matrix_to_property(
                    anchor_matrix.inverted_safe() @ obj.matrix_world
                )

    def is_preparing_batches(self):
        total = len(self._prepare_jobs)
        if self._prepare_index < total:
            batch, obj = self._prepare_jobs[self._prepare_index]
            self.set_pipeline_detail(
                f"Preparing {obj.name}", self._prepare_index + 1, total
            )
            failed_ancestor = obj.parent
            while (
                failed_ancestor is not None
                and failed_ancestor not in self._prepare_failed_objects
            ):
                failed_ancestor = failed_ancestor.parent
            try:
                if failed_ancestor is not None:
                    raise RuntimeError(
                        f"ancestor '{failed_ancestor.name}' was not prepared"
                    )
                self._prepare_object_transactionally(bpy.context, obj)
            except Exception as ex:
                message = f"{obj.name}: {ex}"
                if self.preparationErrorPolicy == 'CANCEL':
                    self.report({'ERROR'}, f"Batch preparation failed: {message}")
                    self._response = {'CANCELLED'}
                    return False
                self._prepare_failed_objects.add(obj)
                self._prepare_failures[batch.as_pointer()].append(message)
                self.report({'WARNING'}, f"Ignored preparation failure: {message}")
            self._prepare_index += 1
            return True

        if not self._prepare_metadata_finished:
            for batch in self.batches:
                failures = self._prepare_failures[batch.as_pointer()]
                batch[BATCH_IMPORT_READY_KEY] = not failures
                batch[BATCH_PREPARATION_FAILURE_COUNT_KEY] = len(failures)
                batch[BATCH_PREPARATION_FAILURES_KEY] = " | ".join(failures)
                self._refresh_filler_relative_matrices(batch)
            self._prepare_metadata_finished = True
        return False

    def _finish_realization(self):
        source_layer = GetLayerCollection("SOURCE", bpy.context.view_layer.layer_collection)
        if source_layer is None:
            self.report({'ERROR'}, "SOURCE is not available in the active View Layer")
            self._response = {'CANCELLED'}
            return
        source_layer.exclude = True

    def pack_uvs(self, context: Context):
        _deselect_selected(context)
        self._pack_index = 0
        self._pack_announced = False
        self.set_pipeline_detail("Preparing batches", 0, len(self.batches))

    def is_packing_uvs(self):
        total = len(self.batches)
        if self._pack_index >= total:
            return False
        batch_collection = self.batches[self._pack_index]
        batch_meshes = _batch_meshes(batch_collection)
        if not self._pack_announced:
            self.set_pipeline_detail(
                f"{batch_collection.name}: {len(batch_meshes)} mesh objects",
                self._pack_index + 1,
                total,
            )
            print(f"[UNPACK] Packing {batch_collection.name}: {len(batch_meshes)} meshes "
                  f"({self._pack_index + 1}/{total})")
            self._pack_announced = True
            return True
        if not batch_meshes:
            self.report({'ERROR'}, f"'{batch_collection.name}' contains no render-visible, non-collision meshes")
            self._response = {'CANCELLED'}
            return False
        for obj in batch_meshes:
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            obj.select_set(True)
        bpy.context.view_layer.objects.active = batch_meshes[0]
        bpy.ops.object.mode_set(mode="EDIT")
        result = UVPack_Scaled(
            bpy.context,
            self,
            batch_meshes,
            True,
            self.uvMargin,
            self.textureSize,
            self.heuristicDuration,
            self.pixelPerfect,
        )
        bpy.ops.object.mode_set(mode="OBJECT")
        if result == {'CANCELLED'}:
            self._response = result
            return False
        for obj in batch_meshes:
            obj.select_set(False)
        self._pack_index += 1
        self._pack_announced = False
        return self._pack_index < total

    def on_pipeline_finished(self, context: Context, cancelled: bool):
        if not cancelled:
            for batch in getattr(self, "batches", []):
                source_name = batch.get(BATCH_SOURCE_COLLECTION_KEY)
                source = bpy.data.collections.get(source_name) if source_name else None
                if source is not None:
                    batch.name = self._pending_batch_final_names[source.as_pointer()]
            created = len(getattr(self, "batches", []))
            skipped = getattr(self, "_existing_batch_count", 0)
            self.report({'INFO'}, f"Created {created} missing batch(es); kept {skipped} existing batch(es)")
            return
        source_layer = GetLayerCollection("SOURCE", context.view_layer.layer_collection)
        if source_layer:
            source_layer.exclude = False
        DeleteCollection("TEMP_EXPORT_STUFF")
        for batch in list(getattr(self, "batches", [])):
            try:
                if bpy.data.collections.get(batch.name) is batch:
                    DeleteCollection(batch.name)
            except ReferenceError:
                pass
        ReleaseTempActiveObjByName()
        self.report({'INFO'}, "Partial new batches were removed; existing batches were preserved")


class UnpackActiveCollection(_UnpackBase):
    bl_idname = "lightmap.unpack_active_collection"
    bl_label = "Unpack Active Collection"
    bl_description = "Create or replace the batch for the active direct child of SOURCE"
    bl_options = {'REGISTER', "UNDO", "UNDO_GROUPED"}

    uvMargin: IntProperty(
        name="UV Margin",
        description="Packing margin measured in pixels at the texture resolution",
        default=8,
        min=0,
    )
    packLightmaps: BoolProperty(
        name="Pack Lightmaps",
        description="Pack eligible meshes into one shared LightMap UV space",
        default=True,
    )
    pixelPerfect: BoolProperty(
        name="Pixel-Perfect Packing",
        description="Align packed island bounds to the target texture-resolution grid",
        default=True,
    )
    heuristicDuration: IntProperty(
        name="Heuristic Duration",
        description="Seconds UVPackmaster spends searching for a better layout",
        default=10,
        min=1,
        max=3600,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.label(text=f"Source: {self._active_source(context).name}", icon='OUTLINER_COLLECTION')
        layout.prop(self, "textureSize")
        layout.prop(self, "uvMargin")
        layout.prop(self, "packLightmaps")
        pixel_row = layout.row()
        pixel_row.enabled = self.packLightmaps
        pixel_row.prop(self, "pixelPerfect")
        heuristic_row = layout.row()
        heuristic_row.enabled = self.packLightmaps
        heuristic_row.prop(self, "heuristicDuration")
        layout.prop(self, "preparationErrorPolicy")

    @staticmethod
    def _active_source(context):
        return context.view_layer.active_layer_collection.collection

    def validate(self, context: Context):
        source = bpy.data.collections.get('SOURCE')
        if source is None:
            self.report({'ERROR'}, "Create a top-level collection named 'SOURCE'")
            return False
        active = self._active_source(context)
        if source.children.get(active.name) is not active:
            self.report({'ERROR'}, "Make a direct child collection of SOURCE active in the Outliner")
            return False
        if not active.all_objects:
            self.report({'ERROR'}, f"'{active.name}' contains no objects")
            return False
        if self.packLightmaps and not _has_uvpackmaster(context):
            self.report({'ERROR'}, "UVPackmaster 3 is required when Pack Lightmaps is enabled")
            return False
        if (self.packLightmaps and self.pixelPerfect
                and not hasattr(context.scene.uvpm3_props, "pixel_perfect_align")):
            self.report({'ERROR'}, "Pixel-perfect packing requires UVPackmaster 3.2.6 or newer")
            return False
        if bpy.data.collections.get("TEMP_EXPORT_STUFF"):
            self.report({'ERROR'}, "TEMP_EXPORT_STUFF remains from an interrupted run; clear lightmapping data first")
            return False
        return True

    def execute(self, context: Context):
        Preferences.save_dialog_settings(self, PACK_DIALOG_SETTINGS)
        if not self.validate(context):
            return {'CANCELLED'}
        self._requested_source = self._active_source(context)
        self._previous_batch = None
        self._pending_batch = None
        return PipelineOperator.execute(self, context)

    def get_source(self):
        source_root = bpy.data.collections.get('SOURCE')
        source = getattr(self, "_requested_source", None)
        if source_root is None or source is None or source_root.children.get(source.name) is not source:
            self.report({'ERROR'}, "The active source collection is no longer a direct child of SOURCE")
            return {'CANCELLED'}
        export = GetOrCreateCollection("EXPORT_STUFF")
        exact_legacy_name = None
        for batch in export.children:
            if batch.get(BATCH_SOURCE_COLLECTION_KEY) == source.name:
                self._previous_batch = batch
                break
            batch_number = _batch_number(batch)
            if batch_number is not None and batch.name == _batch_collection_name(batch_number, source):
                exact_legacy_name = batch
        if self._previous_batch is None:
            self._previous_batch = exact_legacy_name
        if self._previous_batch is not None:
            number = _batch_number(self._previous_batch)
        else:
            used = {_batch_number(batch) for batch in export.children}
            number = 0
            while (
                number in used
                or bpy.data.collections.get(_batch_collection_name(number, source)) is not None
            ):
                number += 1
        self.source_collection = source
        self.source_batches = [source]
        self._target_batch_name = _batch_collection_name(number, source)
        pending_name = f"__PVM_PENDING_{self._target_batch_name}"
        stale = bpy.data.collections.get(pending_name)
        if stale is not None:
            DeleteCollection(stale.name)
        self._generated_batch_names = {source.as_pointer(): pending_name}
        self.set_pipeline_detail(f"Preparing {source.name} as Batch{number}", 1, 1)

    def _finish_realization(self):
        # Visibility changes only after realization and optional packing both succeed.
        return None

    def realize_instances(self, context: Context):
        result = super().realize_instances(context)
        self._pending_batch = self.batches[0] if self.batches else None
        return result

    def on_pipeline_finished(self, context: Context, cancelled: bool):
        pending = getattr(self, "_pending_batch", None)
        if cancelled:
            if pending is not None and bpy.data.collections.get(pending.name) is pending:
                DeleteCollection(pending.name)
            source_layer = GetLayerCollection(self._requested_source.name, context.view_layer.layer_collection)
            if source_layer:
                source_layer.exclude = False
            DeleteCollection("TEMP_EXPORT_STUFF")
            ReleaseTempActiveObjByName()
            self.report({'INFO'}, "Partial active-collection output was removed")
            return
        previous = getattr(self, "_previous_batch", None)
        if previous is not None and bpy.data.collections.get(previous.name) is previous:
            DeleteCollection(previous.name)
        pending.name = self._target_batch_name
        pending[BATCH_SOURCE_COLLECTION_KEY] = self._requested_source.name
        source_layer = GetLayerCollection(self._requested_source.name, context.view_layer.layer_collection)
        if source_layer is None:
            self.report({'WARNING'}, "Batch created, but its source collection was not found in the active View Layer")
        else:
            source_layer.exclude = True
        self.report({'INFO'}, f"Created {pending.name} and hid {self._requested_source.name}")
        _send_windows_notification("PsychoVertexMaster", "Lightmap unpack finished")


class ScaledUVPacking(Operator):
    bl_idname = "lightmap.scaled_uv_packing"
    bl_label = "Scaled UV Packing"
    bl_options = {'REGISTER', "UNDO", "UNDO_GROUPED"}

    heuristic: bpy.props.BoolProperty(name="Heuristic", default=True)
    pixel_margin: bpy.props.IntProperty(name="Pixel Margin", default=6, min=1, max=256)
    texture_size: bpy.props.IntProperty(name="Texture Size", default=2048, min=32, max=4096)

    def invoke(self, context, event):
        if not _validate_pack_objects(self, context, list(context.selected_objects)):
            return {'CANCELLED'}
        Preferences.load_dialog_settings(self, SCALED_PACK_DIALOG_SETTINGS)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "heuristic")
        layout.prop(self, "pixel_margin")
        layout.prop(self, "texture_size")

    def execute(self, context: Context):
        Preferences.save_dialog_settings(self, SCALED_PACK_DIALOG_SETTINGS)
        result = UVPack_Scaled(context, self, list(context.selected_objects), self.heuristic, self.pixel_margin, self.texture_size)
        if result == {'FINISHED'}:
            self.report({'INFO'}, f"Packed {len(context.selected_objects)} mesh object(s)")
        return result


class SetLightmapScaleOperator(Operator):
    bl_idname = "lightmap.set_scale"
    bl_label = "Set Lightmap Scale"
    bl_description = "Sets the lightmap scale after normalization for the currently selected faces"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    scale: bpy.props.FloatProperty(name="Size", min=0, max=5, default=1)

    @classmethod
    def poll(cls, context: Context) -> bool:
        ao = context.active_object
        return ao is not None and ao.mode == "EDIT" and ao.type == "MESH"

    def invoke(self, context, event):
        Preferences.load_dialog_settings(self, ("scale",))
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "scale")

    def execute(self, context):
        Preferences.save_dialog_settings(self, ("scale",))
        changed_faces = 0
        for obj in context.selected_objects:
            if obj.type != "MESH" or obj.mode != "EDIT":
                continue
            mesh = obj.data
            bm = bmesh.from_edit_mesh(mesh)
            # Get or create a BMesh face layer for the lightmap scale
            layer = bm.faces.layers.float.get("lightmap_scale")
            if not layer:
                layer = bm.faces.layers.float.new("lightmap_scale")
                for face in bm.faces:
                    face[layer] = 1.0
            # Assign the scale to selected faces
            for face in bm.faces:
                if face.select:
                    face[layer] = self.scale
                    changed_faces += 1
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        if changed_faces == 0:
            self.report({'WARNING'}, "No mesh faces were selected")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Set lightmap scale to {self.scale:g} on {changed_faces} face(s)")
        return {'FINISHED'}


class SelectSmallMeshIslands(Operator):
    bl_idname = "lightmap.select_small_mesh_islands"
    bl_label = "Select Small Mesh Islands"
    bl_description = "Select connected mesh islands smaller than the specified world-space area"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    max_surface_area: FloatProperty(
        name="Maximum Surface Area (m²)", default=1.0, min=0.0, precision=4,
    )

    @classmethod
    def poll(cls, context: Context) -> bool:
        active = context.active_object
        return active is not None and active.type == 'MESH' and active.mode == 'EDIT'

    def invoke(self, context: Context, event):
        Preferences.load_dialog_settings(self, ("max_surface_area",))
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context: Context):
        self.layout.prop(self, "max_surface_area")

    @staticmethod
    def _world_face_area(face, matrix: Matrix) -> float:
        loops = list(face.loops)
        if len(loops) < 3:
            return 0.0
        origin = matrix @ loops[0].vert.co
        return sum(
            ((matrix @ loops[index].vert.co - origin).cross(
                matrix @ loops[index + 1].vert.co - origin
            )).length * 0.5
            for index in range(1, len(loops) - 1)
        )

    def execute(self, context: Context):
        Preferences.save_dialog_settings(self, ("max_surface_area",))
        meter_scale = context.scene.unit_settings.scale_length
        selected_islands = 0
        selected_faces = 0

        for obj in context.objects_in_mode:
            if obj.type != 'MESH':
                continue
            bm = bmesh.from_edit_mesh(obj.data)
            visited = set()
            islands = []
            for face in bm.faces:
                if face in visited:
                    continue
                island = []
                pending = [face]
                visited.add(face)
                while pending:
                    current = pending.pop()
                    island.append(current)
                    for edge in current.edges:
                        for linked_face in edge.link_faces:
                            if linked_face not in visited:
                                visited.add(linked_face)
                                pending.append(linked_face)
                islands.append(island)

            for island in islands:
                area = sum(self._world_face_area(face, obj.matrix_world) for face in island)
                is_small = area * meter_scale * meter_scale < self.max_surface_area
                for face in island:
                    face.select_set(is_small)
                if is_small:
                    selected_islands += 1
                    selected_faces += len(island)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)

        if selected_islands == 0:
            self.report({'INFO'}, f"No mesh islands are smaller than {self.max_surface_area:g} m²")
        else:
            self.report({'INFO'}, f"Selected {selected_islands} island(s), {selected_faces} face(s) below {self.max_surface_area:g} m²")
        return {'FINISHED'}


class MZAGE_PT_MaterialMenu(bpy.types.Panel):
    bl_label = "Light Baking"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        obj = bpy.context.object
        mat = bpy.context.material
        layout.use_property_split = True
        layout.use_property_decorate = False
        if mat and obj and obj.type == "MESH":
            layout.prop(mat, "light_baked")
            layout.prop(mat, "passthrough")
            layout.prop(mat, "matfulltransparent")
            if sum((mat.light_baked, mat.passthrough, mat.matfulltransparent)) > 1:
                layout.label(text="Choose only one Light Baking role", icon='ERROR')


def OnDisplayLightingChanged(self, context: bpy.types.Context):
    val = self.display_lighting
    export_collection: Collection | None = bpy.data.collections.get("EXPORT_STUFF")
    if export_collection is None:
        return
    visited_materials = set()
    for batch_collection in export_collection.children:
        for batch_object in batch_collection.all_objects:
            if batch_object.type != 'MESH':
                continue
            for slot in batch_object.material_slots:
                material = slot.material
                material_id = material.as_pointer() if material else None
                if material is None or material.library or material_id in visited_materials:
                    continue
                visited_materials.add(material_id)
                if not material.use_nodes or material.node_tree is None:
                    continue
                for node in material.node_tree.nodes:
                    lighting_mode_input = node.inputs.get("LightingMode")
                    if lighting_mode_input:
                        lighting_mode_input.default_value = val


@persistent
def InitDisplayLighting(dummy):
    OnDisplayLightingChanged(bpy.context.scene, bpy.context)


def register():
    bpy.utils.register_class(SelectSmallMeshIslands)
    bpy.utils.register_class(SetLightmapScaleOperator)
    bpy.utils.register_class(ScaledUVPacking)
    bpy.utils.register_class(RepackActiveBatch)
    bpy.utils.register_class(UnpackActiveCollection)
    bpy.utils.register_class(BakeBatch)
    bpy.utils.register_class(DenoiseBatch)
    bpy.utils.register_class(ReplaceFillers)
    bpy.utils.register_class(ClearFillerReplacements)
    bpy.utils.register_class(ClearLightmappingStuff)
    bpy.utils.register_class(MZAGE_PT_MaterialMenu)

    bpy.types.Material.light_baked = bpy.props.BoolProperty(name="Light Baked", description="Whether it should be baked", default=False)
    bpy.types.Material.passthrough = bpy.props.BoolProperty(
        name="Glass (Ray Portal)", description="It provides a Ray Portal BSDF mixed with Transparent BSDF, Color controlled by Vertex Color", default=False)
    bpy.types.Material.matfulltransparent = bpy.props.BoolProperty(name="Passthrough", description="If material should not get affected and affect the lightmapping in ANY WAY", default=False)
    bpy.types.Scene.display_lighting = bpy.props.BoolProperty(name="Lighting Mode", default=False, update=OnDisplayLightingChanged)
    bpy.app.handlers.load_post.append(InitDisplayLighting)


def unregister():
    bpy.utils.unregister_class(MZAGE_PT_MaterialMenu)
    bpy.utils.unregister_class(ClearLightmappingStuff)
    bpy.utils.unregister_class(ClearFillerReplacements)
    bpy.utils.unregister_class(ReplaceFillers)
    bpy.utils.unregister_class(DenoiseBatch)
    bpy.utils.unregister_class(BakeBatch)
    bpy.utils.unregister_class(UnpackActiveCollection)
    bpy.utils.unregister_class(RepackActiveBatch)
    bpy.utils.unregister_class(ScaledUVPacking)
    bpy.utils.unregister_class(SetLightmapScaleOperator)
    bpy.utils.unregister_class(SelectSmallMeshIslands)

    del bpy.types.Material.light_baked
    del bpy.types.Material.passthrough
    del bpy.types.Material.matfulltransparent
    del bpy.types.Scene.display_lighting
    if InitDisplayLighting in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(InitDisplayLighting)
