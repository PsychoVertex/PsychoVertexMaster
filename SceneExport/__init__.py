"""Export prepared lightmapping batches for deterministic Unreal reconstruction."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Collection, Context, Object
from mathutils import Matrix

from .. import Preferences
from ..Pipeline import PipelineOperator, PipelineTask, send_windows_notification
from . import model


ASSET_KEY = "pvm_filler_asset"
RELATIVE_MATRIX_KEY = "pvm_filler_relative_matrix"
GROUP_KEY = "pvm_filler_group"
OCCURRENCE_KEY = "pvm_asset_occurrence"
IMPORT_READY_KEY = "pvm_import_ready"
BATCH_FILLERS = "BATCH_FILLERS"
FILLERS_COLLECTION = "FILLERS"
SOURCE_NAME_KEY = "pvm_source_collection"
EXPORT_DIALOG_SETTINGS = (
    "directory", "export_scene_json_only", "preview_static_meshes",
)


@dataclass
class Occurrence:
    asset_id: str
    anchor: Matrix
    members: list[Object]
    kind: str
    batch: int
    library_id: str | None = None


@dataclass
class Placement:
    asset_id: str
    anchor: Matrix
    kind: str
    batch: int
    source_name: str


@dataclass
class Asset:
    asset_id: str
    export_name: str
    occurrences: list[Occurrence] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)

    @property
    def canonical(self):
        return next((item for item in self.occurrences if item.kind == "canonical"), self.occurrences[0])


def _batch_number(collection: Collection):
    match = re.match(r"^Batch(\d+)(?:\s.*)?$", collection.name)
    return int(match.group(1)) if match else None


def _matrix_property(values):
    if values is None or len(values) != 16:
        raise ValueError("stored relative transform must contain 16 values")
    return Matrix([values[index:index + 4] for index in range(0, 16, 4)])


def _matrix_close(first: Matrix, second: Matrix, tolerance=1e-5):
    """Compare affine matrices without penalizing large-world float cancellation."""
    for row in range(3):
        for column in range(3):
            if abs(first[row][column] - second[row][column]) > tolerance:
                return False
    for row in range(3):
        magnitude = max(1.0, abs(first[row][3]), abs(second[row][3]))
        if abs(first[row][3] - second[row][3]) > max(tolerance, magnitude * 1e-5):
            return False
    return all(abs(first[3][column] - second[3][column]) <= tolerance for column in range(4))


def _decompose(matrix: Matrix):
    model.validate_affine_matrix(matrix)
    if any(not math.isfinite(value) for row in matrix for value in row):
        raise ValueError("transform contains a non-finite value")
    if not _matrix_close(matrix, Matrix.LocRotScale(*matrix.decompose()), 1e-4):
        raise ValueError("transform contains shear that Unreal FTransform cannot reproduce")
    location, rotation, scale = matrix.decompose()
    if min(abs(value) for value in scale) <= 1e-8:
        raise ValueError("transform has a zero scale axis")
    return location, rotation, scale


def _discover_occurrences(collection: Collection, batch: int, kind: str):
    metadata = {}
    unowned = []
    for obj in collection.objects:
        asset_id = obj.get(ASSET_KEY)
        relative_values = obj.get(RELATIVE_MATRIX_KEY)
        if asset_id and relative_values is not None:
            relative = _matrix_property(relative_values)
            anchor = obj.matrix_world @ relative.inverted_safe()
            occurrence_key = obj.get(OCCURRENCE_KEY)
            if not occurrence_key and kind == "canonical":
                occurrence_key = obj.get(GROUP_KEY)
            key = (asset_id, occurrence_key or model.matrix_key(anchor, 5))
            record = metadata.setdefault(key, [anchor, [], obj])
            record[1].append(obj)
        else:
            unowned.append(obj)

    found = []
    for (library_id, _key), (anchor, members, reference) in metadata.items():
        # A realized BatchN occurrence is an independently prepared/baked
        # source asset even when another occurrence came from the same source
        # collection. The original collection name is retained only for
        # matching true FILLERS instances to a deterministic prepared source.
        identity = Matrix.Identity(4)
        member_set = set(members)
        roots = [obj for obj in members if obj.parent not in member_set]
        anchors = [
            obj for obj in roots
            if obj.get(RELATIVE_MATRIX_KEY) is not None
            and _matrix_close(_matrix_property(obj[RELATIVE_MATRIX_KEY]), identity)
        ]
        source = min(
            anchors or roots or [reference], key=lambda obj: obj.name.casefold()
        )
        found.append(Occurrence(
            source.name, anchor, members, kind, batch, library_id,
        ))
    unowned_set = set(unowned)
    for root in (obj for obj in unowned if obj.parent not in unowned_set):
        members = [root] + [child for child in root.children_recursive if child in unowned_set]
        found.append(Occurrence(root.name, root.matrix_world.copy(), members, kind, batch))
    return found


def _source_name_for_batch(batch):
    source_name = batch.get(SOURCE_NAME_KEY)
    if not source_name:
        match = re.match(r"^Batch\d+\s+\((.+)\)$", batch.name)
        source_name = match.group(1) if match else None
    return source_name


def _filler_collection_for_batch(fillers_root, batch):
    source_name = _source_name_for_batch(batch)
    if not source_name or not source_name.startswith("S_"):
        return None
    return fillers_root.children.get(f"F_{source_name[2:]}") if fillers_root else None


def _layer_collection_path(layer_collection, target, path=None):
    path = (path or []) + [layer_collection]
    if layer_collection.collection is target:
        return path
    for child in layer_collection.children:
        found = _layer_collection_path(child, target, path)
        if found is not None:
            return found
    return None


def _evaluated_filler_instances(context: Context, collection: Collection):
    """Capture matrices after evaluating an F_X collection hidden by replacement."""
    path = _layer_collection_path(context.view_layer.layer_collection, collection)
    if path is None:
        raise ValueError(
            f"'{collection.name}' is not present in the active View Layer"
        )
    exclusions = [(layer, layer.exclude) for layer in path]
    try:
        for layer, _excluded in exclusions:
            layer.exclude = False
        context.view_layer.update()
        seen = set()
        instances = []
        for obj in collection.all_objects:
            pointer = obj.as_pointer()
            if pointer in seen or obj.instance_collection is None:
                continue
            seen.add(pointer)
            evaluated = obj.evaluated_get(context.evaluated_depsgraph_get())
            instances.append((obj, evaluated.matrix_world.copy()))
        return instances
    finally:
        for layer, excluded in reversed(exclusions):
            layer.exclude = excluded
        context.view_layer.update()


def _append_placements(context: Context, assets, batches):
    """Use the same generated anchors that define FBX pivots for canonical placement."""
    fillers_root = bpy.data.collections.get(FILLERS_COLLECTION)
    assets_by_id = {asset.asset_id: asset for asset in assets}

    for batch in batches:
        batch_number = _batch_number(batch)
        candidate_ids = set()
        filler_asset_by_library = {}
        for asset in assets:
            for occurrence in asset.occurrences:
                if occurrence.batch != batch_number:
                    continue
                candidate_ids.add(asset.asset_id)
                if occurrence.library_id:
                    filler_asset_by_library.setdefault(
                        occurrence.library_id, asset.asset_id
                    )
                asset.placements.append(Placement(
                    asset.asset_id, occurrence.anchor.copy(), "canonical",
                    batch_number, occurrence.members[0].name,
                ))

        filler_collection = _filler_collection_for_batch(fillers_root, batch)
        if filler_collection is None:
            continue
        for obj, matrix_world in _evaluated_filler_instances(context, filler_collection):
            asset_id = filler_asset_by_library.get(obj.instance_collection.name)
            if asset_id is None or asset_id not in candidate_ids:
                continue
            assets_by_id[asset_id].placements.append(Placement(
                asset_id, matrix_world, "filler", batch_number, obj.name,
            ))

    missing = [asset.asset_id for asset in assets if not asset.placements]
    if missing:
        raise ValueError(
            "No EXPORT_STUFF placement found for generated asset(s): "
            + ", ".join(sorted(missing))
        )
    for asset in assets:
        for placement in asset.placements:
            _decompose(placement.anchor)


def discover_assets(context: Context):
    export = bpy.data.collections.get("EXPORT_STUFF")
    if export is None:
        raise ValueError("No EXPORT_STUFF collection; unpack at least one source collection")
    batches = sorted((item for item in export.children if _batch_number(item) is not None), key=_batch_number)
    if not batches:
        raise ValueError("EXPORT_STUFF contains no generated BatchN collections")
    for batch in batches:
        if batch.get(IMPORT_READY_KEY, True) is False:
            raise ValueError(f"'{batch.name}' is incomplete; regenerate it before export")

    occurrences = []
    for batch in batches:
        occurrences.extend(_discover_occurrences(batch, _batch_number(batch), "canonical"))

    assets = {}
    export_names = {}
    for occurrence in occurrences:
        if not any(obj.type == 'MESH' and model.collision_prefix(obj.name) is None for obj in occurrence.members):
            continue
        export_name = model.static_mesh_name(occurrence.asset_id)
        previous = export_names.setdefault(export_name.casefold(), occurrence.asset_id)
        if previous != occurrence.asset_id:
            raise ValueError(f"Unreal name collision: '{previous}' and '{occurrence.asset_id}' both become '{export_name}'")
        asset = assets.setdefault(occurrence.asset_id, Asset(occurrence.asset_id, export_name))
        asset.occurrences.append(occurrence)
    if not assets:
        raise ValueError("No exportable logical mesh assets were found")

    ordered_assets = sorted(assets.values(), key=lambda item: item.export_name.casefold())
    _append_placements(context, ordered_assets, batches)
    return ordered_assets


class _ContextState:
    def __init__(self, context: Context):
        self.context = context
        self.active = context.view_layer.objects.active
        self.selected = list(context.selected_objects)
        self.mode = self.active.mode if self.active else 'OBJECT'

    def restore(self):
        if self.context.object and self.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for obj in self.selected:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if self.active and self.active.name in bpy.data.objects:
            self.context.view_layer.objects.active = self.active
        if self.mode != 'OBJECT' and self.context.object:
            try:
                bpy.ops.object.mode_set(mode=self.mode)
            except RuntimeError:
                pass


def _export_asset(context: Context, asset: Asset, filepath: Path):
    temporary = bpy.data.collections.new(f"__PVM_EXPORT_{uuid.uuid4().hex}")
    context.scene.collection.children.link(temporary)
    created = []
    try:
        anchor_inverse = asset.canonical.anchor.inverted_safe()
        render_objects = []
        collision_objects = []
        collision_counts = {}
        for source in asset.canonical.members:
            if source.type != 'MESH':
                continue
            duplicate = source.copy()
            duplicate.data = source.data.copy()
            duplicate.animation_data_clear()
            duplicate.parent = None
            # Bake every prepared mesh into the same asset-pivot space at the
            # data level.  Keeping FBX nodes at identity avoids parent-inverse
            # and exporter transform differences between render and collision
            # objects, whose individual origins are intentionally independent.
            relative_values = source.get(RELATIVE_MATRIX_KEY)
            local_matrix = (
                _matrix_property(relative_values)
                if relative_values is not None
                else anchor_inverse @ source.matrix_world
            )
            duplicate.data.transform(local_matrix, shape_keys=True)
            duplicate.matrix_world = Matrix.Identity(4)
            duplicate.data.update()
            duplicate.hide_viewport = False
            duplicate.hide_render = False
            duplicate.hide_set(False)
            temporary.objects.link(duplicate)
            created.append(duplicate)
            prefix = model.collision_prefix(source.name)
            if prefix:
                collision_counts[prefix] = collision_counts.get(prefix, 0) + 1
                duplicate.name = f"{prefix}{asset.export_name}_{collision_counts[prefix]:02d}"
                collision_objects.append(duplicate)
            else:
                render_objects.append(duplicate)
        if not render_objects:
            raise ValueError(f"Asset '{asset.asset_id}' contains no render mesh")

        bpy.ops.object.select_all(action='DESELECT')
        for obj in render_objects:
            obj.select_set(True)
        context.view_layer.objects.active = render_objects[0]
        if len(render_objects) > 1 and bpy.ops.object.join() != {'FINISHED'}:
            raise RuntimeError(f"Could not join render meshes for '{asset.asset_id}'")
        render = context.view_layer.objects.active
        render.name = asset.export_name

        bpy.ops.object.select_all(action='DESELECT')
        for obj in list(temporary.objects):
            obj.select_set(True)
        context.view_layer.objects.active = render
        result = bpy.ops.export_scene.fbx(
            filepath=str(filepath), use_selection=True, object_types={'MESH'},
            global_scale=1.0, apply_unit_scale=True, apply_scale_options='FBX_SCALE_UNITS',
            use_space_transform=True, bake_space_transform=False,
            axis_forward='-Y', axis_up='Z', use_mesh_modifiers=False,
            mesh_smooth_type='FACE', use_tspace=True, use_triangles=False,
            bake_anim=False, path_mode='AUTO', embed_textures=False,
        )
        if result != {'FINISHED'}:
            raise RuntimeError(f"FBX export failed for '{asset.asset_id}'")
    finally:
        for obj in list(temporary.objects):
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data and data.users == 0:
                bpy.data.meshes.remove(data)
        bpy.data.collections.remove(temporary)


def _document(assets):
    from .. import bl_info

    blend_path = bpy.data.filepath
    unit_scale = bpy.context.scene.unit_settings.scale_length
    reconstruction = model.reconstruction_id(blend_path)
    asset_rows = []
    placements = []
    for asset in assets:
        batches = sorted({item.batch for item in asset.placements})
        asset_rows.append({
            "id": asset.asset_id,
            "static_mesh_name": asset.export_name,
            "fbx": asset.export_name + ".fbx",
            "source_batches": batches,
        })
        ordered = sorted(
            asset.placements,
            key=lambda item: (item.batch, item.kind, item.source_name, model.matrix_key(item.anchor)),
        )
        for occurrence in ordered:
            location, rotation, scale = _decompose(occurrence.anchor)
            matrix_values = [[occurrence.anchor[row][column] for column in range(4)] for row in range(4)]
            placements.append({
                "id": model.occurrence_id(
                    reconstruction, asset.asset_id, occurrence.kind,
                    occurrence.batch, matrix_values, occurrence.source_name,
                ),
                "asset_id": asset.asset_id,
                "kind": occurrence.kind,
                "source_batch": occurrence.batch,
                "transform": model.blender_to_unreal_transform(
                    location, (rotation.x, rotation.y, rotation.z, rotation.w), scale, unit_scale),
            })
    return model.make_document(
        addon_version=bl_info["version"], scene_name=bpy.context.scene.name,
        blend_path=blend_path, unit_scale_meters=unit_scale,
        assets=asset_rows, placements=placements)


def _publish(staging: Path, destination: Path):
    backup = staging / "__previous"
    backup.mkdir()
    names = [item.name for item in staging.iterdir() if item.name != "__previous"]
    replaced = []
    published = []
    try:
        for name in names:
            final = destination / name
            if final.exists():
                os.replace(final, backup / name)
                replaced.append(name)
            os.replace(staging / name, final)
            published.append(name)
    except Exception:
        for name in reversed(published):
            final = destination / name
            if final.exists():
                final.unlink()
        for name in replaced:
            previous = backup / name
            if previous.exists():
                os.replace(previous, destination / name)
        raise


class ExportUnrealScene(PipelineOperator):
    bl_idname = "lightmap.export_unreal_scene"
    bl_label = "Export Baked Scene for Unreal"
    bl_description = "Export reusable FBXs and one Unreal reconstruction JSON"

    directory: StringProperty(name="Export Folder", subtype='DIR_PATH')
    export_scene_json_only: BoolProperty(
        name="Export Scene Json Only",
        description="Write PVMScene.json without re-exporting Static Mesh FBXs",
        default=False,
    )
    preview_static_meshes: BoolProperty(
        name="Preview Static Meshes",
        description="Select the BatchN mesh objects that would be exported without writing any files",
        default=False,
    )

    def invoke(self, context, event):
        Preferences.load_dialog_settings(self, EXPORT_DIALOG_SETTINGS)
        if not self.directory and bpy.data.filepath:
            self.directory = str(Path(bpy.data.filepath).parent)
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "directory")
        layout.prop(self, "preview_static_meshes")
        row = layout.row()
        row.enabled = not self.preview_static_meshes
        row.prop(self, "export_scene_json_only")
        box = layout.box()
        box.label(text="Uses the first canonical occurrence for FBX geometry.", icon='INFO')
        box.label(text="FBX geometry comes from EXPORT_STUFF batches.")
        box.label(text="Batch occurrences and their paired FILLERS/F_X instances become JSON placements.")
        box.label(text="Invalid transforms, names, and incomplete batches still cancel export.")

    def execute(self, context):
        Preferences.save_dialog_settings(self, EXPORT_DIALOG_SETTINGS)
        if not self.preview_static_meshes and not bpy.data.filepath:
            self.report({'ERROR'}, "Save the .blend file before exporting")
            return {'CANCELLED'}
        if not self.preview_static_meshes:
            destination = Path(bpy.path.abspath(self.directory))
            if not destination.is_dir():
                self.report({'ERROR'}, "Choose an existing writable export folder")
                return {'CANCELLED'}
            self._destination = destination
            self._staging = destination / f".__pvm_scene_export_{uuid.uuid4().hex}"
        else:
            self._destination = None
            self._staging = None
        self._context_state = _ContextState(context)
        self._assets = []
        self._document_data = None
        self._asset_index = 0
        self.cancellable = True
        return PipelineOperator.execute(self, context)

    def get_tasks(self):
        if self.preview_static_meshes:
            return [PipelineTask(self.preview_export, label="Preview Static Meshes")]
        tasks = [PipelineTask(self.prepare_export, label="Prepare Unreal Export")]
        if not self.export_scene_json_only:
            tasks.append(PipelineTask(
                self.begin_asset_exports, self.poll_asset_exports,
                deferred=False, label="Export Static Mesh FBXs",
            ))
        tasks.append(PipelineTask(self.publish_export, label="Publish Unreal Scene"))
        return tasks

    def preview_export(self, context: Context):
        self._assets = discover_assets(context)
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in list(context.selected_objects):
            obj.select_set(False)
        meshes = []
        seen = set()
        for asset in self._assets:
            for obj in asset.canonical.members:
                pointer = obj.as_pointer()
                if obj.type != 'MESH' or pointer in seen:
                    continue
                seen.add(pointer)
                obj.select_set(True)
                meshes.append(obj)
        if not meshes:
            raise ValueError("No Static Mesh objects would be exported")
        context.view_layer.objects.active = meshes[0]
        self.set_pipeline_detail(
            f"Selected {len(meshes)} mesh object(s) in {len(self._assets)} asset(s)",
            len(meshes), len(meshes),
        )

    def prepare_export(self, context: Context):
        self._assets = discover_assets(context)
        self._document_data = _document(self._assets)
        self._staging.mkdir()
        if (
            not self.export_scene_json_only
            and context.object
            and context.object.mode != 'OBJECT'
        ):
            bpy.ops.object.mode_set(mode='OBJECT')
        self.set_pipeline_detail(
            f"Prepared {len(self._assets)} asset(s)", 0, len(self._assets)
        )

    def begin_asset_exports(self):
        self._asset_index = 0
        self.set_pipeline_detail("Ready to export FBXs", 0, len(self._assets))

    def poll_asset_exports(self):
        if self._asset_index >= len(self._assets):
            return False
        asset = self._assets[self._asset_index]
        self.set_pipeline_detail(
            asset.export_name, self._asset_index + 1, len(self._assets)
        )
        _export_asset(
            bpy.context, asset, self._staging / f"{asset.export_name}.fbx"
        )
        self._asset_index += 1
        return self._asset_index < len(self._assets)

    def publish_export(self):
        # Once publication begins, Escape must not report cancellation after
        # the destination files have already been atomically replaced.
        self.cancellable = False
        (self._staging / "PVMScene.json").write_text(
            json.dumps(self._document_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _publish(self._staging, self._destination)
        published = (
            "Published PVMScene.json only"
            if self.export_scene_json_only
            else "Published PVMScene.json and FBXs"
        )
        self.set_pipeline_detail(published, 1, 1)

    def on_pipeline_finished(self, context: Context, cancelled: bool):
        state = getattr(self, "_context_state", None)
        staging = getattr(self, "_staging", None)
        try:
            if state is not None and (cancelled or not self.preview_static_meshes):
                state.restore()
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        if cancelled:
            if self.preview_static_meshes:
                self.report({'INFO'}, "Static Mesh preview cancelled")
            else:
                self.report({'INFO'}, "Partial Unreal export removed; existing files were preserved")
            return
        if self.preview_static_meshes:
            mesh_count = sum(
                1 for obj in context.selected_objects if obj.type == 'MESH'
            )
            self.report({"INFO"}, f"Preview selected {mesh_count} Static Mesh object(s); no files were written")
            return
        if self.export_scene_json_only:
            summary = (
                f"Exported JSON with {len(self._document_data['placements'])} "
                "placements; existing FBXs unchanged"
            )
        else:
            summary = (
                f"Exported {len(self._assets)} assets and "
                f"{len(self._document_data['placements'])} placements"
            )
        self.report({'INFO'}, summary)
        notification = (
            "Unreal scene JSON export finished"
            if self.export_scene_json_only
            else "Unreal scene export finished"
        )
        send_windows_notification("PsychoVertexMaster", notification)


def register():
    bpy.utils.register_class(ExportUnrealScene)


def unregister():
    bpy.utils.unregister_class(ExportUnrealScene)
