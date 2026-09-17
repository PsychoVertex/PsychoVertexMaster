# AGENTS.md

## Project

PsychoVertexMaster is a Blender add-on package. The package entry point is `__init__.py`; each feature folder exposes `register()` and `unregister()`. The current priority is the custom lightmapping workflow in `Lightmapping/__init__.py`.

Keep changes small and compatible with Blender's Python API. Do not build or launch Blender automatically. Ask the user to install/reload the add-on and test Blender-dependent behavior.

## Repository Map

- `Lightmapping/__init__.py`: UV scaling/packing, collection realization and batching, baking, denoising, material integration, lighting preview, and cleanup.
- `Pipeline.py`: modal task runner used by `UnpackCollections` and `BakeBatch`.
- `HandyMenu/__init__.py`: exposes lightmapping operators in edit/object mode menus.
- `UvTools/__init__.py`: general UV-layer utilities; related but separate from the lightmapping packer.
- `HandyUtils/BlenderToUnreal.py`: configures Unreal/BFU lightmap export settings; do not confuse this with the custom baked-lightmap workflow.
- Root `__init__.py`: module registration order and add-on metadata.

## Lightmapping Workflow

The expected user flow is:

1. Prepare a top-level collection named `SOURCE`. Each direct child collection is one bake batch.
2. Instance objects inside those batch collections. Meshes intended to receive baked lighting need a UV layer named `LightMap`.
3. Mark materials in the **Light Baking** material panel:
   - `light_baked`: receives the baked lightmap.
   - `passthrough`: temporarily becomes a vertex-color-driven Ray Portal plus Transparent shader.
   - `matfulltransparent`: temporarily becomes fully transparent and does not participate normally.
4. In Edit Mode, use `lightmap.set_scale` to write a per-face float layer named `lightmap_scale`; default/generated value is `1.0`, and `0` effectively removes a face from useful lightmap area.
5. Run `lightmap.unpack_collections`. It realizes instances, builds `EXPORT_STUFF`, joins meshes into `BatchN` and optional `BatchN_NoShadows`, applies scale, preserves collision children, and packs `LightMap` UVs.
6. Select a generated batch and run `lightmap.bake_batch`. It bakes to a float `NoisyLightmap`, runs the scene compositor, writes `//Lightmaps/LM_BN.exr`, scales it to final resolution, and reconnects it to material inputs named `LightMap`.
7. Use `Scene.display_lighting` to toggle node inputs named `LightingMode` on generated batch materials.
8. Run `lightmap.clear_lightmapping_stuff` to reveal `SOURCE`, remove generated collections/temp data, detach the noisy compositor image, and purge orphans.

## Hard Contracts and Dependencies

Treat these names and assumptions as a file-format/API contract. If changing one, update every producer and consumer plus this document.

- Collections: `SOURCE`, `EXPORT_STUFF`, `TEMP_EXPORT_STUFF`.
- Generated objects: `BatchN`, `BatchN_NoShadows`; bake output numbering depends on slicing `Batch` from the active object's name.
- Mesh data: UV layer `LightMap`; BMesh face float layer `lightmap_scale`.
- Vertex colors: passthrough materials read color attribute `Color`.
- Material node: image node `LightMapImageNode`.
- Shader group/interface inputs: `LightMap` and `LightingMode`.
- Compositor image node: `Noisy Lightmap Slot`.
- Temporary datablocks: object `TempObject`, mesh `TempMesh`, image `NoisyLightmap`, materials `Passthrough Material` and `Fully Transparent Material`.
- Output path: blend-relative `//Lightmaps/LM_BN.exr`; therefore the `.blend` should be saved and the directory must be writable.
- UV packing requires UVPackmaster 3: `scene.uvpm3_props` and `bpy.ops.uvpackmaster3.*`.
- Baking requires Cycles settings and the scene's configured `cycles.bake_type`.
- Denoising is not self-contained: it expects a valid compositor node tree centered on `Noisy Lightmap Slot` and renders with compositing enabled.
- `ShaderNodeBsdfRayPortal` requires a Blender version that provides the Ray Portal node, despite the older minimum version in `bl_info`.

## Implementation Rules for Lightmapping

- Preserve Blender operator context explicitly: active object, selection, mode, view layer, and collection linkage matter to nearly every `bpy.ops` call here.
- `UVPack_Scaled` expects mesh objects already selected in multi-object Edit Mode. Validate object type, `LightMap`, `lightmap_scale`, material slots, and material indices before indexing them.
- The packer intentionally performs two passes: normalized packing, per-face scale application/pinning, then non-normalized packing. Do not collapse this without verifying texel-density behavior.
- Faces whose material is absent or not `light_baked` are scaled to zero and pinned near the upper-right corner. The current offset is hard-coded for 2048 (`3 / 2048`); account for `texture_size` if revising this behavior.
- `visible_shadow == False` meshes are split into the `_NoShadows` object but baked alongside the main batch using the same image/UV space.
- Preserve material-slot order across preparation and restoration. Linked materials, local copied materials, passthrough materials, transparent materials, and empty slots follow different branches.
- Guard `material.use_nodes`/`node_tree` before accessing nodes. Avoid mutating node-link collections while iterating them directly; iterate over a copied list.
- Any pipeline step that creates handlers, materials, images, collections, or changes scene settings needs cleanup for success, cancellation, and exceptions. The base `PipelineOperator.finish()` currently only removes its timer/progress UI.
- Bake handlers must be removed exactly once. Consider cancellation/error paths as well as `object_bake_complete`.
- Store and restore scene values changed by baking when practical: render resolution/output/color settings, bake margin, samples, filepath, compositing, selection/mode, and `display_lighting`.
- Do not add destructive cleanup casually. `ClearLightmappingStuff` calls recursive orphan purge and deletes generated objects/collections; changes here require special care and clear user-facing behavior.
- Use `self.report()` and return `{'CANCELLED'}` for unmet prerequisites. A helper returning cancellation is not enough unless its caller propagates it.
- Keep registration symmetrical. Every class, property, and persistent handler added in `register()` must be safely removed in `unregister()`.

## Known Fragile Areas to Address When Touched

- `prepare_materials_for_baking()` checks `not material.library` before `material.light_baked`; this makes the intended “first-time light-baked material” branch unreachable for ordinary local materials and can leave `img_node` as `None`.
- `ScaledUVPacking.execute()` ignores the cancellation result from `UVPack_Scaled()` and always reports success.
- Empty batches or batches containing only lights/no-shadow meshes can fail at `batch_objs[0]`.
- `GetLayerCollection(...)` can return `None`, but callers access `.exclude` without a guard.
- Pipeline cancellation does not roll back temporary state or remove bake handlers/materials.
- `finalize_lightmap_denoise()` assumes the compositor wrote the expected EXR successfully.
- Cleanup and material removal are not protected against partial preparation or shared/reused temporary datablocks.
- Several operations assume the active area supports `header_text_set` and the required Blender operator context.

Do not opportunistically fix these while doing unrelated work. When changing the affected path, either make it safe or state the remaining limitation.

## Validation

Static checks can catch syntax and registration mistakes, but Blender behavior must be tested in Blender. For lightmapping changes, ask the user to verify the smallest relevant scenario and report console errors:

- one `SOURCE` child with one instanced mesh;
- a `LightMap` UV and `lightmap_scale` values including `0`, fractional, and `1`;
- baked, passthrough, fully transparent, linked, and empty material slots as relevant;
- both normal and `visible_shadow == False` meshes;
- unpack, UV packing, bake completion/cancellation, denoise output, lighting toggle, and cleanup;
- add-on disable/re-enable to verify unregister/register symmetry.

Do not claim Blender runtime validation unless the user performed it. Avoid broad formatting or refactors in the large lightmapping module unless requested.
