# Unreal Engine Assets Exporter integration reference

This document records how the installed **Unreal Engine Assets Exporter** (historically “Blender for Unreal Engine” or BFU) works, with emphasis on integration from PsychoVertexMaster.

## Audited installation

- Extension ID: `unrealengine_assets_exporter`
- Display name: `Unreal Engine Assets Exporter`
- Version: `4.4.3`
- Minimum Blender version: `4.5.0`
- Installed package: `%APPDATA%/Blender Foundation/Blender/4.5/extensions/user_default/unrealengine_assets_exporter`
- Upstream: <https://github.com/xavier150/unrealengine_assets_exporter-Addons/>
- Audit date: 2026-09-21

This reference was derived from the installed source. It does not claim Blender or Unreal runtime validation.

## Mental model

BFU does not export the current selection as one undifferentiated file. It discovers logical assets, expands each asset into one or more export packages, temporarily stages Blender data for each package, exports files, restores the scene, and emits JSON plus Unreal Python scripts.

```text
Blender objects/collections/actions
        |
        | bfu_* properties and scene filters
        v
registered BFU asset classes
        |
        | discover AssetToExport records
        v
AssetToExport -> AssetPackage(s) + optional additional-data JSON
        |
        | duplicate/stage/transform/export/restore
        v
FBX / GLB / ABC files + ImportAssetData.json
        |
        | generated Unreal Python import script
        v
Unreal import task + post-import settings
```

The important abstraction types are in `bfu_assets_manager/bfu_asset_manager_type.py`:

- `AssetToExport`: logical Unreal asset, import name/path, packages, and optional additional data.
- `AssetPackage`: one physical export operation and file, with objects or a collection, optional action/frame range, and an export callback.
- `PackageFile`: directory, filename, file type, and content type.
- `BFU_BaseAssetClass`: plug-in interface for discovery, naming, package construction, UI, and additional metadata.

## Registration and modules

The root `__init__.py` imports and registers many small feature packages. Registration order matters because asset classes register themselves into a global list. Major modules include:

- `bfu_assets_manager`: asset registry, discovery, and data model.
- `bfu_export_control`: the per-object `bfu_export_type` contract.
- `bfu_export_filter`: scene-level enabled asset categories and selection filter.
- `bfu_export_nomenclature`: prefixes, disk paths, and Unreal destination paths.
- `bfu_static_mesh`, `bfu_skeletal_mesh`, `bfu_base_collection`: primary mesh exporters.
- `bfu_anim_action`, `bfu_anim_nla`, `bfu_alembic_animation`: animation asset types.
- `bfu_camera`, `bfu_spline`, `bfu_groom`: specialized exporters.
- `bfu_collision`, `bfu_socket`, `bfu_lod`, `bfu_material`, `bfu_vertex_color`, `bfu_light_map`, `bfu_nanite`: export preparation and additional-data providers.
- `bfu_export`: scene staging and physical FBX/glTF export.
- `bfu_export_text_files`: export log, import JSON, and generated script copies.
- `bfu_import_module`: code intended to execute inside Unreal Editor.
- `fbxio`: bundled/customized Blender FBX exporter used by BFU's custom FBX procedures.

## Asset discovery

Asset classes declare whether they support a Blender datum and whether their category is enabled. The manager asks all registered classes and builds the final asset cache. Supported logical types are:

| BFU type | Meaning |
|---|---|
| `StaticMesh` | Static mesh rooted at an object |
| `CollectionStaticMesh` | Collection exported as a static mesh |
| `SkeletalMesh` | Armature and associated meshes |
| `Action` | Skeletal action animation |
| `Pose` | Single-frame pose animation |
| `NonLinearAnimation` | NLA animation |
| `AlembicAnimation` | Alembic animation |
| `GroomSimulation` | Groom cache/simulation |
| `Camera` | Camera animation/data |
| `Spline` | Curve/spline data; the current Blender-side class constructs an asset whose serialized type is `StaticMesh`, while spline data is carried as additional metadata |

Scene category switches are `bfu_use_static_export`, `bfu_use_static_collection_export`, `bfu_use_skeletal_export`, `bfu_use_animation_export`, `bfu_use_alembic_export`, `bfu_use_groom_simulation_export`, `bfu_use_camera_export`, and `bfu_use_spline_export`. All default to enabled in 4.4.3.

`scene.bfu_export_selection_filter` controls the final cache:

- `default`: normal recursive-export discovery.
- `only_object`: selected and visible objects only.
- `only_object_and_active`: selected/visible objects plus only the active action or modular part.

## Object export ownership

`Object.bfu_export_type` has three stable values:

- `auto`: exported with a parent whose value is `export_recursive`.
- `export_recursive`: defines an exported root and includes eligible descendants.
- `dont_export`: never exported.

This is a hierarchy contract, not merely a selection flag. PsychoVertexMaster should normally mark only intended asset roots `export_recursive`. Marking every selected descendant can create multiple assets or overlapping exports depending on type discovery.

## Main export operation

The primary operator is:

```python
bpy.ops.object.exportforunreal()
```

Its implementation is `BFU_OT_ExportForUnrealEngineButton`. Before export it requires:

- Blender's `io_scene_fbx` add-on to be active.
- At least one enabled asset category.
- At least one discovered asset.
- A saved `.blend` file.
- NLA tweak mode to be exited.

The operator then:

1. Forces a full asset-cache refresh.
2. Clears prior BFU logs.
3. Runs BFU's general corrective pass.
4. Saves scene state, selection, active object, modes, visibility, collection/layer visibility, frame state, and relevant datablock names.
5. Moves out of local view, enters Object Mode, and makes scene content selectable and available for package staging.
6. Optionally deletes configured export directory trees when the add-on preference `revertExportPath` is enabled.
7. Exports every logical asset package.
8. Writes additional JSON, import data/scripts, sequencer data/scripts, and the export log according to scene switches.
9. Restores the saved scene and selection and removes actions created during export.
10. Prints per-asset results and clears runtime logs.

Notable behavior: failed readiness checks report a warning but the top-level operator returns `FINISHED`, not `CANCELLED`.

## Package staging and restoration

Before each package, BFU hides objects not belonging to the package and unhides package objects. Export implementations generally duplicate selected source data, make linked/visual instances real where needed, apply selected modifiers, convert supported objects to meshes, apply export transforms, temporarily rename objects/sockets, remove materials from collision meshes, export only the staged selection, then delete temporary data and restore names/settings.

This staging is highly context-sensitive. Active object, mode, selection, visibility, local view, parents, constraints, modifiers, unit scale, action, and frame range all affect it. Integrations should configure properties before invoking the top-level operator and should not attempt to modify scene state concurrently.

## File formats and procedures

The common file types are `FBX`, `GLTF` (`.glb`), `Alembic` (`.abc`), and `JSON`.

- Standard FBX calls `bpy.ops.export_scene.fbx`.
- Custom FBX calls the bundled `fbxio` implementation and adds BFU features such as UE mannequin bone alignment, right-side bone mirroring, animation-only export, and free-scale handling.
- glTF calls `bpy.ops.export_scene.gltf`.
- Alembic is used for Alembic animation/groom workflows.

Static and skeletal export procedure properties select the concrete path. Do not assume every BFU property applies to every procedure; several custom FBX options are consumed only by the bundled exporter.

Typical FBX settings used by BFU include selection-only export, face smoothing, no leaf bones for skeletal assets, explicit axis conversion, no embedded textures, and normals/tangents preserved for Unreal import.

## Default output and destination paths

Paths are blend-relative by default, so the `.blend` must be saved:

| Content | Default disk directory |
|---|---|
| Static meshes | `//ExportedAssets/StaticMesh/` |
| Skeletal meshes | `//ExportedAssets/SkeletalMesh/` |
| Skeletal animations | `//ExportedAssets/SkeletalAnimation/` |
| Alembic | `//ExportedAssets/Alembic/` |
| Groom | `//ExportedAssets/Groom/` |
| Cameras/sequencer | `//ExportedAssets/Sequencer/` |
| Splines | `//ExportedAssets/Spline/` |
| Logs, JSON, scripts | `//ExportedAssets/` |

The Unreal destination is constructed as:

```text
<scene.bfu_unreal_import_module>/<scene.bfu_unreal_import_location>/<object-or-collection bfu_export_folder_name>
```

Defaults are module `Game` and location `ImportedBlenderAssets`. `Game` corresponds to the project Content root. Values are passed through BFU's folder-name sanitizer.

Default filename prefixes include `SM_`, `SKM_`, `SK_`, `Anim_`, `Pose_`, `Cam_`, `Spline_`, and `GS_`. Per-object custom export naming can override normal prefix/suffix construction.

## Generated import contract

With generated scripts enabled and the relevant scene switches on, BFU writes:

- `ImportAssetData.json`: authoritative list of exported assets and files.
- `ImportAssetScript.py`: copied Unreal-side loader for asset import.
- `ImportSequencerData.json`: camera/sequencer tracks.
- `ImportSequencerScript.py`: copied Unreal-side sequencer loader.
- `ExportLog.txt`: results and timing.
- `<asset>_additional_data.json`: optional per-asset settings.

Each main JSON asset entry includes at least:

```text
scene_unit_scale
asset_name
asset_type
asset_import_name
asset_import_path
files[] -> type, content_type, file_path
```

Depending on type it also includes skeleton references, skeletal-mesh references, frame range, animation metadata, collision/material/lightmap/LOD/Nanite/vertex-color settings, and the path of additional data.

The generated script can be run from Unreal's Python console. BFU also exposes copy-command operators:

```python
bpy.ops.object.copy_importassetscript_command()
bpy.ops.object.copy_importsequencerscript_command()
```

The standalone bundled runner accepts `--type assets|sequencer`, `--data_filepath`, and optional `--show_finished_popup`.

## Unreal-side importer

The generated script loads `bfu_import_module` and creates `unreal.AssetImportTask` instances. It supports legacy `FbxImportUI` and, when available, `InterchangeGenericAssetsPipeline`; `config.force_use_interchange` defaults to false. Automated tasks, save-after-import, and select-after-import default to enabled.

Import order is intentional:

1. Alembic, groom, spline, and camera.
2. Static meshes and collection static meshes.
3. Skeletal meshes.
4. Skeletal animations.

This lets animations resolve previously imported skeleton/mesh references. After import it applies feature modules for vertex colors, LODs, materials, lightmaps, and Nanite, saves assets, and selects imported objects in the Content Browser.

The importer requires Unreal Editor's Python API. Feature-specific Unreal plugins/APIs may also be necessary, notably Alembic and sequencer support.

## Lightmap contract

BFU's lightmap properties are import settings for Unreal static meshes; they do not represent PsychoVertexMaster's baked EXR workflow.

Relevant object properties:

| Property | Meaning |
|---|---|
| `bfu_generate_light_map_uvs` | Ask Unreal import/build settings to generate lightmap UVs |
| `bfu_static_mesh_light_map_mode` | `Default`, `CustomMap`, or `SurfaceArea` |
| `bfu_static_mesh_custom_light_map_res` | Explicit resolution for `CustomMap` |
| `computedStaticMeshLightMapRes` | Cached source surface area used by `SurfaceArea` |
| `bfu_static_mesh_light_map_surface_scale` | Multiplier used by the surface-area formula |
| `bfu_static_mesh_light_map_round_power_of_two` | Round calculated resolution to nearest power of two |
| `bfu_use_static_mesh_light_map_world_scale` | Include average absolute object scale in calculation |

For `SurfaceArea`, 4.4.3 computes approximately:

```text
resolution = sqrt(computed surface area)
             * optional average absolute object scale
             * scene unit scale
             * surface scale / 2
```

It optionally rounds this result to the nearest power of two. The calculated value is serialized as `light_map_resolution`; non-default modes also set `use_custom_light_map_resolution = true`.

On Unreal import, BFU:

- Applies `generate_light_map_uvs` to the FBX or Interchange import pipeline.
- Applies custom `light_map_resolution` to the imported `StaticMesh` and LOD 0 minimum lightmap resolution.
- Updates import/build settings for lightmap UV generation.

For meshes whose UV channel is already prepared by PsychoVertexMaster, `bfu_generate_light_map_uvs` should normally be `False` so Unreal does not regenerate the UVs. BFU 4.4.3 does not expose a source/destination lightmap UV channel through the inspected lightmap module, so channel ordering remains significant.

## Current PsychoVertexMaster integration

`HandyUtils/BlenderToUnreal.py` directly writes BFU properties. It defines four operators:

| Operator | Effect |
|---|---|
| `object.btus_setup` | Configures selected objects for recursive export, destination folder, transform, Nanite, collision, material search, and lightmap resolution |
| `object.btus_export` | Sets selected objects to `export_recursive` |
| `object.btus_dontexport` | Sets selected objects to `dont_export` |
| `object.btus_updatepath` | Rebuilds selected objects' `bfu_export_folder_name` from collection ancestry |

`object.btus_setup` currently sets:

```python
obj.bfu_export_type = "export_recursive"
obj.bfu_export_folder_name = "/".join(collection_path)
obj.bfu_rotate_to_zero_for_export = True
obj.bfu_build_nanite_mode = "build_nanite_false"
obj.bfu_collision_trace_flag = <operator choice>
obj.bfu_auto_generate_collision = True
obj.bfu_material_search_location = "AllAssets"
```

Unless the object is already in `CustomMap` mode, it also sets:

```python
obj.bfu_generate_light_map_uvs = False
obj.bfu_static_mesh_light_map_mode = "SurfaceArea"
obj.bfu_use_static_mesh_light_map_world_scale = True
obj.bfu_static_mesh_light_map_round_power_of_two = <operator choice>
obj.bfu_static_mesh_light_map_surface_scale = <operator value, default 64>
```

### Current integration risks

- There is no check that the BFU extension is installed/enabled before accessing its dynamic properties. Calling these operators without BFU registered will raise `AttributeError`.
- `btus_setup` skips objects already marked `dont_export`; `btus_export` does not.
- Every selected object is made an `export_recursive` root. For parent/child selections this may be broader than intended.
- The path builder uses only `users_collection[0]`; objects linked to multiple collections are ambiguous.
- Parent collection lookup compares child names and chooses the first match. Collection names are unique in a `.blend`, but a collection can be linked beneath multiple parents, so ancestry can still be ambiguous.
- The generated folder includes the full collection ancestry visible through `bpy.data.collections`, but the scene master collection is not part of that lookup.
- Existing `CustomMap` settings are deliberately preserved; all other modes are overwritten with `SurfaceArea` behavior.
- Disabling BFU lightmap UV generation preserves authored UVs, but neither current integration nor BFU's inspected lightmap module explicitly selects the Unreal lightmap coordinate index.
- Property strings are an external API dependency. BFU upgrades can rename properties or enum values without a Python import failure in PsychoVertexMaster.
- The commented examples `bfu_export_axis_forward` and `bfu_export_axis_up` in `HandyUtils/BlenderToUnreal.py` are not registered BFU 4.4.3 properties. The actual names are `bfu_fbx_export_axis_forward` and `bfu_fbx_export_axis_up`, and they take effect as overrides only with `bfu_override_procedure_preset = True`.

## Recommended integration rules

When adding deeper support:

1. Detect BFU by extension/module availability and by required RNA properties, not only a hard-coded display name.
2. Treat version `4.4.3` as the currently documented schema. Gate future behavior when a required property or enum item is absent.
3. Centralize BFU property names and enum values in one compatibility module.
4. Separate “configure selected objects” from “invoke full export”; exporting changes global Blender context temporarily.
5. Choose one asset root per intended Unreal asset and let descendants remain `auto` unless there is a deliberate nested asset.
6. Resolve multi-collection and multi-parent collection cases explicitly instead of silently using the first link.
7. Preserve authored `LightMap` UVs by keeping `bfu_generate_light_map_uvs = False`; verify UV channel order in Unreal after import.
8. Keep PsychoVertexMaster baked-lightmap material/EXR handling separate from BFU's static-mesh lightmap-resolution metadata.
9. If writing import JSON or invoking BFU internals directly, use its `AssetToExport`/`AssetPackage` interfaces rather than duplicating their schema.
10. Test source scene restoration after export cancellation or exceptions; BFU's workflow mutates broad scene visibility and temporary datablocks.

## Coding against BFU

BFU properties are registered dynamically on Blender RNA types. PsychoVertexMaster must not assume that importing its own modules means BFU is enabled. Test the exact capability before reading or writing it:

```python
def has_bfu_object_api(obj):
    required = (
        "bfu_export_type",
        "bfu_export_folder_name",
        "bfu_static_export_procedure",
    )
    return all(hasattr(obj, name) for name in required)
```

For enum assignments, validate the identifier against RNA rather than relying on a UI label:

```python
def enum_identifiers(data, property_name):
    prop = data.bl_rna.properties.get(property_name)
    return {item.identifier for item in prop.enum_items} if prop else set()

if "export_recursive" in enum_identifiers(obj, "bfu_export_type"):
    obj.bfu_export_type = "export_recursive"
```

Recommended adapter behavior:

- Put BFU detection, reads, writes, enum validation, and version-specific fallbacks in one PVM compatibility module.
- Return a clear `self.report({'ERROR'}, ...)` and `{'CANCELLED'}` when required BFU capabilities are absent.
- Use `getattr()` only when a meaningful fallback exists. Do not silently skip a property required for correct export.
- Configure data properties directly, but invoke the public top-level operator for a complete export. Calling package internals bypasses scene save/restore and generated metadata.
- Do not cache Blender objects across BFU export. BFU duplicates, renames, removes, and restores datablocks during staging.
- Do not write BFU's runtime cache/log properties.
- Treat identifiers as case-sensitive. Several names intentionally preserve historical casing or spelling, such as `computedStaticMeshLightMapRes`, `bfu_use_socket_custom_Name`, and `ADITIONAL_DATA_ONLY` in the Python enum class.

### Public operators relevant to integration

| Operator ID | Purpose |
|---|---|
| `object.exportforunreal` | Discover and export all assets allowed by current filters |
| `object.copy_importassetscript_command` | Copy the Unreal asset-import command to the clipboard |
| `object.copy_importsequencerscript_command` | Copy the Unreal sequencer-import command |
| `object.comput_lightmap` | Recalculate surface area for the active object's BFU lightmap resolution |
| `object.comput_all_lightmap` | Recalculate surface area for eligible recursive-export static meshes |

### Critical enum identifiers

Use identifiers on the left; labels shown in Blender are not API values.

| Property | Accepted identifiers in 4.4.3 |
|---|---|
| `Object.bfu_export_type` | `auto`, `export_recursive`, `dont_export` |
| `Scene.bfu_export_selection_filter` | `default`, `only_object`, `only_object_and_active` |
| `Object.bfu_static_export_procedure` | `custom_fbx_export`, `standard_fbx`, `standard_gltf` |
| `Object.bfu_skeleton_export_procedure` | `custom_fbx_export`, `standard_fbx`, `standard_gltf` |
| `Object.bfu_alembic_export_procedure` | `standard_alembic` |
| `Object.bfu_groom_export_procedure` | `standard_alembic` |
| `Object.bfu_camera_export_procedure` | `additional_data_only`, `standard_fbx`, `standard_gltf` |
| `Object.bfu_static_mesh_light_map_mode` | `Default`, `CustomMap`, `SurfaceArea` |
| `Object.bfu_build_nanite_mode` | `auto`, `build_nanite_true`, `build_nanite_false` |
| `Object.bfu_collision_trace_flag` | `CTF_UseDefault`, `CTF_UseSimpleAndComplex`, `CTF_UseSimpleAsComplex`, `CTF_UseComplexAsSimple` |
| `Object.bfu_material_search_location` | `Local`, `UnderParent`, `UnderRoot`, `AllAssets` |
| `Object.bfu_vertex_color_import_option` | `IGNORE`, `OVERRIDE`, `REPLACE` |
| `Object.bfu_vertex_color_to_use` | `FirstIndex`, `LastIndex`, `ActiveIndex`, `CustomIndex` |
| `Object.bfu_vertex_color_type` | `SRGB`, `LINEAR` |
| FBX axis properties | `X`, `Y`, `Z`, `-X`, `-Y`, `-Z` |

The static and skeletal default procedure is `standard_fbx`. Camera defaults to `additional_data_only`. BFU's custom static FBX preset uses forward `-Z`, up `Y`; the custom skeletal preset additionally uses primary bone axis `X` and secondary `-Z`. Standard skeletal FBX uses primary `Y` and secondary `X`.

## BFU 4.4.3 RNA property catalog

This catalog lists the installed extension's integration-facing domain properties. Type names are included so code can validate both presence and expected shape. UI accordion state, error-display collections, asset caches, and timing-log state are listed separately because PVM should not write them.

### Asset identity and location

| Owner | Property | Type | Role |
|---|---|---|---|
| Object | `bfu_export_type` | Enum | Root/child/excluded export ownership |
| Object | `bfu_export_folder_name` | String | Relative Unreal destination subfolder |
| Object | `bfu_use_custom_export_name` | Boolean | Enables explicit filename/import name |
| Object | `bfu_custom_export_name` | String | Explicit export name |
| Collection | `bfu_export_folder_name` | String | Collection asset destination subfolder |
| Collection | `bfu_collection_export_procedure` | Enum | Collection static-mesh procedure |

### Scene export filters and generated files

All properties below belong to `Scene`:

- Category booleans: `bfu_use_static_export`, `bfu_use_static_collection_export`, `bfu_use_skeletal_export`, `bfu_use_animation_export`, `bfu_use_alembic_export`, `bfu_use_groom_simulation_export`, `bfu_use_camera_export`, `bfu_use_spline_export`.
- Generated-output booleans: `bfu_use_text_export_log`, `bfu_use_text_import_asset_script`, `bfu_use_text_import_sequence_script`, `bfu_use_text_additional_data`.
- Final-list filter enum: `bfu_export_selection_filter`.

### Scene naming and paths

All properties below belong to `Scene` and are strings:

- Prefixes: `bfu_static_mesh_prefix_export_name`, `bfu_skeletal_mesh_prefix_export_name`, `bfu_skeleton_prefix_export_name`, `bfu_alembic_animation_prefix_export_name`, `bfu_groom_simulation_prefix_export_name`, `bfu_anim_prefix_export_name`, `bfu_pose_prefix_export_name`, `bfu_camera_prefix_export_name`, `bfu_spline_prefix_export_name`.
- Animation folder: `bfu_anim_subfolder_name`.
- Unreal destination: `bfu_unreal_import_module`, `bfu_unreal_import_location`.
- Disk directories: `bfu_export_static_mesh_file_path`, `bfu_export_skeletal_mesh_file_path`, `bfu_export_skeletal_animation_file_path`, `bfu_export_alembic_file_path`, `bfu_export_groom_file_path`, `bfu_export_camera_file_path`, `bfu_export_spline_file_path`, `bfu_export_other_file_path`.
- Generated filenames: `bfu_file_export_log_name`, `bfu_file_import_asset_script_name`, `bfu_file_import_sequencer_script_name`.

### Transform and FBX procedure controls

All properties below belong to `Object`:

| Property | Type |
|---|---|
| `bfu_move_to_center_for_export` | Boolean |
| `bfu_rotate_to_zero_for_export` | Boolean |
| `bfu_additional_location_for_export` | Float vector |
| `bfu_additional_rotation_for_export` | Float vector |
| `bfu_export_global_scale` | Float |
| `bfu_override_procedure_preset` | Boolean |
| `bfu_fbx_export_use_space_transform` | Boolean |
| `bfu_fbx_export_axis_forward` | Enum |
| `bfu_fbx_export_axis_up` | Enum |
| `bfu_fbx_export_primary_bone_axis` | Enum |
| `bfu_fbx_export_secondary_bone_axis` | Enum |
| `bfu_export_with_meta_data` | Boolean |
| `bfu_fbx_export_with_custom_props` | Boolean |
| `bfu_do_not_import_curve_with_zero` | Boolean |

Procedure selectors on `Object` are `bfu_static_export_procedure`, `bfu_skeleton_export_procedure`, `bfu_alembic_export_procedure`, `bfu_groom_export_procedure`, `bfu_camera_export_procedure`, and `bfu_spline_export_procedure`.

### Static mesh, collision, lightmap, Nanite, and UV

All properties below belong to `Object` unless marked `Scene`:

- Collision: `bfu_auto_generate_collision` (Boolean), `bfu_collision_trace_flag` (Enum), `bfu_create_physics_asset` (Boolean), `bfu_enable_skeletal_mesh_per_poly_collision` (Boolean).
- Collision tool scene settings: `Scene.bfu_keep_original_geometry_for_collision`, `Scene.bfu_use_world_space_for_collision`, `Scene.bfu_use_fast_bounding_box_approximation` (Booleans).
- Lightmap: `bfu_generate_light_map_uvs` (Boolean), `bfu_static_mesh_light_map_mode` (Enum), `bfu_static_mesh_custom_light_map_res` (Integer), `computedStaticMeshLightMapRes` (Float), `bfu_static_mesh_light_map_surface_scale` (Float), `bfu_static_mesh_light_map_round_power_of_two` (Boolean), `bfu_use_static_mesh_light_map_world_scale` (Boolean).
- Nanite: `bfu_build_nanite_mode` (Enum).
- UV conversion/correction: `bfu_convert_geometry_node_attribute_to_uv` (Boolean), `bfu_convert_geometry_node_attribute_to_uv_name` (String), `bfu_use_correct_extrem_uv_scale` (Boolean), `bfu_correct_extrem_uv_scale_step_scale` (Integer), `bfu_correct_extrem_uv_scale_use_absolute` (Boolean).

### Materials and vertex colors

All properties below belong to `Object`:

- Material booleans: `bfu_export_materials`, `bfu_export_textures`, `bfu_import_materials`, `bfu_import_textures`, `bfu_flip_normal_map_green_channel`, `bfu_reorder_material_to_fbx_order`.
- Material lookup enum: `bfu_material_search_location`.
- Vertex color: `bfu_vertex_color_import_option` (Enum), `bfu_vertex_color_override_color` (Float vector), `bfu_vertex_color_to_use` (Enum), `bfu_vertex_color_index_to_use` (Integer), `bfu_vertex_color_type` (Enum).

### LODs

All properties below belong to `Object`:

- `bfu_export_as_lod_mesh` (Boolean).
- `bfu_use_static_mesh_lod_group` (Boolean).
- `bfu_static_mesh_lod_group` (String).
- `bfu_lod_target1` through `bfu_lod_target5` (Object pointers).

### Skeletal mesh and asset references

All properties below belong to `Object`:

- Skeletal export: `bfu_export_deform_only`, `bfu_export_skeletal_mesh_as_static_mesh`, `bfu_create_sub_folder_with_skeletal_mesh_name`, `bfu_mirror_symmetry_right_side_bones`, `bfu_use_ue_mannequin_bone_alignment` (Booleans).
- Animation base: `bfu_disable_free_scale_animation`, `bfu_export_animation_without_materials`, `bfu_export_animation_without_mesh`, `bfu_export_animation_without_textures` (Booleans), `bfu_sample_anim_for_export`, `bfu_simplify_anim_for_export` (Floats).
- Skeleton reference: `bfu_engine_ref_skeleton_search_mode` (Enum), `bfu_engine_ref_skeleton_custom_name`, `bfu_engine_ref_skeleton_custom_path`, `bfu_engine_ref_skeleton_custom_ref` (Strings).
- Skeletal mesh reference: `bfu_engine_ref_skeletal_mesh_search_mode` (Enum), `bfu_engine_ref_skeletal_mesh_custom_name`, `bfu_engine_ref_skeletal_mesh_custom_path`, `bfu_engine_ref_skeletal_mesh_custom_ref` (Strings).
- Modular skeletal mesh: `bfu_modular_skeletal_mesh_mode` (Enum), `bfu_modular_skeletal_mesh_every_meshs_separate` (String), `bfu_modular_skeletal_specified_parts_meshs_template` (Pointer).

### Action and NLA animation

All properties below belong to the armature `Object`:

- Action selection/naming: `bfu_action_asset_list` (Collection), `bfu_active_action_asset_list` (Integer), `bfu_anim_action_export_enum` (Enum), `bfu_anim_naming_type` (Enum), `bfu_anim_naming_custom` (String), `bfu_prefix_name_to_export` (String).
- Action frame range: `bfu_anim_action_start_end_time_enum` (Enum), `bfu_anim_action_start_frame_offset`, `bfu_anim_action_end_frame_offset`, `bfu_anim_action_custom_start_frame`, `bfu_anim_action_custom_end_frame` (Integers).
- Action transforms: `bfu_move_action_to_center_for_export`, `bfu_rotate_action_to_zero_for_export` (Booleans).
- NLA: `bfu_anim_nla_use` (Boolean), `bfu_anim_nla_export_name` (String), `bfu_anim_nla_start_end_time_enum` (Enum), `bfu_anim_nla_start_frame_offset`, `bfu_anim_nla_end_frame_offset`, `bfu_anim_nla_custom_start_frame`, `bfu_anim_nla_custom_end_frame` (Integers).
- NLA transforms: `bfu_move_nla_to_center_for_export`, `bfu_rotate_nla_to_zero_for_export` (Booleans).

### Alembic, groom, camera, spline, and sockets

All properties below belong to `Object` unless marked `Scene`:

- Alembic: `bfu_export_as_alembic_animation`, `bfu_create_sub_folder_with_alembic_name` (Booleans).
- Groom: `bfu_export_as_groom_simulation`, `bfu_create_sub_folder_with_groom_alembic_name` (Booleans).
- Camera: `bfu_desired_camera_type` (Enum), `bfu_custom_camera_actor`, `bfu_custom_camera_component`, `bfu_custom_camera_default_actor` (Strings), `bfu_fix_axis_flippings` (Boolean), `bfu_fix_axis_flippings_warp_target` (Float vector).
- Spline: `bfu_desired_spline_type` (Enum), `bfu_spline_resample_resolution` (Integer), `bfu_custom_spline_component` (String), `bfu_export_spline_as_static_mesh` (Boolean), `Scene.bfu_spline_vector_scale` (Float vector).
- Socket: `bfu_use_socket_custom_Name` (Boolean), `bfu_socket_custom_Name` (String). The capital `N` is part of the API name.

### Internal/UI state: do not write from PVM

These are registered RNA properties but are not configuration contracts for export integrations:

- UI state: `Scene.bfu_active_tab`, `Scene.bfu_active_object_tab`, and the many `*_properties_expanded` accordion properties.
- Cached discovery: `Scene.final_asset_cache`, `Scene.bfu_collection_asset_list`, `Scene.bfu_active_collection_asset_list`.
- Diagnostics: `Scene.bfu_export_potential_errors`.
- Timing state: `Scene.bfu_export_process_current_sub_step`, `Scene.bfu_export_process_faster_time`, `Scene.bfu_export_process_slower_time`, `Scene.bfu_export_process_time_logs`.

## Generated JSON keys used by the Unreal importer

When changing integration behavior, distinguish Blender RNA names from serialized JSON names. The main asset document uses `unreal_import_location` and an `assets` list. Per-asset core keys are `scene_unit_scale`, `asset_name`, `asset_type`, `asset_import_name`, `asset_import_path`, and `files`. File entries use `type`, `content_type`, and `file_path`.

Common optional keys include:

- References: `target_skeleton_search_ref`, `target_skeletal_mesh_search_ref`, `target_skeleton_import_ref`.
- Lightmaps: `generate_light_map_uvs`, `use_custom_light_map_resolution`, `light_map_resolution`.
- Collision: `auto_generate_collision`, `collision_trace_flag`, `enable_skeletal_mesh_per_poly_collision`.
- Feature payloads: `Sockets` plus material, LOD, vertex-color, Nanite, camera, and spline fields produced by their modules.

Do not invent JSON fields in PVM without adding a matching Unreal-side consumer. BFU's generated importer ignores unknown fields, while missing expected fields may change import defaults or cause a type-specific failure.

## Upgrade audit procedure

When BFU is upgraded:

1. Record the new `blender_manifest.toml` version and Blender minimum.
2. Diff every property PVM reads or writes against the new registration modules.
3. Re-check enum identifiers, not labels.
4. Trace `BFU_OT_ExportForUnrealEngineButton`, `process_export`, package construction, and `write_main_assets_data`.
5. Diff the Unreal importer, especially `asset_import.py` and feature post-treatment modules.
6. Update this file before adapting PVM code.
7. Ask the user to validate in Blender and Unreal; static source inspection cannot validate context-sensitive export or Unreal API behavior.

## Source map for future work

Use these installed modules as the primary references:

| Question | Module |
|---|---|
| Registration and feature order | `__init__.py` |
| Asset model and supported type enum | `bfu_assets_manager/bfu_asset_manager_type.py` |
| Asset discovery/cache | `bfu_assets_manager/bfu_asset_manager_utils.py`, `bfu_cached_assets/` |
| Object export flags | `bfu_export_control/` |
| Top-level export operator | `bfu_export_process/bfu_export_process_operators.py` |
| Scene save/stage/restore | `bfu_export/bfu_export_asset.py` |
| Per-package dispatch | `bfu_export/bfu_export_single_generic.py` |
| Temporary duplication and transform utilities | `bfu_export/bfu_export_utils.py` |
| FBX and glTF calls | `bfu_export/bfu_fbx_export.py`, `bfu_export/bfu_gltf_export.py` |
| Paths, prefixes, Unreal destination | `bfu_export_nomenclature/` |
| Main import JSON schema | `bfu_export_text_files/bfu_export_text_files_asset_data.py` |
| Script/JSON generation | `bfu_export_text_files/bfu_export_text_files_process.py` |
| Unreal entry point | `run_unreal_import_script.py`, `bfu_import_module/__init__.py` |
| Unreal import tasks | `bfu_import_module/asset_import.py`, `import_module_tasks_class.py` |
| Blender lightmap metadata | `bfu_light_map/` |
| Unreal lightmap application | `bfu_import_module/bfu_import_light_map/` |
| Current PVM bridge | `HandyUtils/BlenderToUnreal.py` |

## Validation checklist for future support

- BFU missing, installed but disabled, and enabled.
- Single static-mesh root with children set to `auto`.
- Parent and child both selected during PVM setup.
- Object linked to multiple collections.
- Authored `LightMap` UV as first and non-first UV channel.
- `Default`, `CustomMap`, and `SurfaceArea` resolution modes.
- Standard FBX, custom FBX, and glTF procedures where supported.
- Collision helpers and `bfu_auto_generate_collision` combinations.
- Existing Unreal asset reimport and material search behavior.
- Legacy FBX importer versus Interchange importer.
- Saved/unsaved `.blend`, NLA tweak mode, local view, hidden objects, and export failure restoration.
- Import into `/Game` and into a named Unreal plugin module.
- BFU upgrade: compare manifest version and audit all PVM-written `bfu_*` properties.
