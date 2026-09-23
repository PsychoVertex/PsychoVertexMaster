# PsychoVertexMaster Lightmapping Guide

This guide explains the complete custom lightmapping workflow: preparing source collections, generating export-ready batches, packing `LightMap` UVs, baking and denoising shared lightmaps, replacing filler instances, previewing results, and cleaning generated data.

## What the workflow produces

Each direct child collection of `SOURCE` becomes one numbered collection below `EXPORT_STUFF`:

```text
SOURCE
├── Building
└── Props

EXPORT_STUFF
├── Batch1 (Building)
└── Batch2 (Props)
```

Every generated batch contains prepared but separate objects that share one packed `LightMap` UV space and one lightmap image. The batch number remains stable and drives the image names:

```text
//Lightmaps/LM_B1_Noisy.exr
//Lightmaps/LM_B1.exr
```

The noisy EXR is the persistent full-resolution bake source. The second EXR is the processed image assigned to batch materials.

## Requirements

Before starting, confirm the following:

- Use Blender 4.2 or newer. Passthrough materials use the Cycles Ray Portal BSDF introduced in Blender 4.2.
- Install and enable UVPackmaster 3. Packing uses its Blender operators and scene settings.
- Save the `.blend` file. Output paths are relative to it, and the `Lightmaps` directory must be writable.
- Use the Cycles render engine and configure the scene's Cycles bake type as required.
- Give every receiving mesh a UV layer named exactly `LightMap`.
- Configure each relevant material's light-baking role.
- Install an external denoiser only if you intend to use that backend.

Collision objects beginning with `UBX_`, `UCX_`, `UCP_`, or `USP_` are preserved but excluded from packing and baking. Objects hidden from rendering are also excluded.

## Finding the tools

Press <kbd>D</kbd> or <kbd>W</kbd> in the 3D View and open **Lightmapping**:

- Edit Mode contains **Set Lightmap Scale**, **Select Small Mesh Islands**, and **Scaled UV Packing**.
- Object Mode and the no-active-object menu contain batch generation, repacking, baking, denoising, filler replacement/restoration, and cleanup.
- Material roles are under **Material Properties → Light Baking**.
- Existing batches are targeted through the active generated collection in the Outliner; selecting a contained object is insufficient.

## 1. Organize source collections

Create one top-level collection named exactly `SOURCE`. Each direct child is treated as one bake batch. Keep objects and collection instances for an asset inside the appropriate child collection.

For example, `SOURCE/Building` and `SOURCE/Props` become different lightmaps. Nested collections are allowed, but only direct children of `SOURCE` define batch boundaries.

The workflow can realize collection instances, duplicate ordinary object hierarchies, preserve useful parent empties and lights, retain collision helpers, apply generated-object modifiers, normalize geometry scale, preserve light transforms without applying unsupported scale operations to light datablocks, and correct mirrored mesh normals. The original source hierarchy remains available and is excluded after successful generation.

## 2. Prepare receiver meshes

Every mesh that receives baked lighting needs a UV layer named `LightMap`. Other UV layers may remain on the object.

**Select Small Mesh Islands** selects every connected mesh island whose world-space surface area is below the supplied square-metre limit. It replaces the current face selection, including in multi-object Edit Mode. Use it before **Set Lightmap Scale** to raise the UV density of small islands.

In Edit Mode, use **Set Lightmap Scale** to write the per-face float value `lightmap_scale`:

| Value | Effect |
| ---: | --- |
| `1.0` | Normal lightmap density. This is also the default when no value exists. |
| Between `0` and `1` | Reduces the face's relative lightmap area. |
| Above `1` | Increases its relative lightmap area. |
| `0` | Removes the face from useful lightmap area and parks it in the reserved corner region. |

Only faces using eligible light-baked materials receive useful packed area. Faces with missing or non-light-baked materials are scaled to zero and pinned near the upper-right corner.

## 3. Configure material roles

Use the **Light Baking** material panel to identify how each material participates:

- **Light Baked**: the material receives the generated lightmap through inputs named `LightMap`.
- **Passthrough**: temporarily becomes a vertex-color-driven Ray Portal plus Transparent shader. It reads the `Color` attribute.
- **Fully Transparent**: temporarily becomes transparent and does not participate normally in the bake.

The preparation and restoration process preserves material-slot order. Empty slots, linked materials, local materials, passthrough materials, and transparent materials are handled separately.

## 4. Generate and pack batches

Make a direct child of `SOURCE` active in the Outliner, then run **Unpack Active**. It creates or replaces only that source collection's batch while preserving unrelated batches.

Important controls include:

- **Pack Lightmaps**: enabled by default. Disable it only when testing realization without UV packing.
- **Texture Resolution**: controls pixel-margin scaling and the final pixel-perfect alignment grid.
- **Pixel Perfect**: aligns the final packing pass to the target texture grid.
- **Heuristic Duration**: controls UVPackmaster's final-pass search time for each batch.

Packing deliberately uses two passes. The first normalizes the layout, then face scale values are applied and pinned, and the final pass preserves the requested relative density. This is required for the custom texel-density behavior.

### Updating one source collection

Make a direct child of `SOURCE` active and run **Unpack Active Collection**. It stages a replacement, reuses the existing batch number when possible, and swaps the result only after realization and optional packing succeed. Other generated batches remain untouched.

### Repacking an existing batch

Make a generated `BatchN` collection active and run **Repack Active Batch**. This repacks its current meshes without regenerating the collection.

### Preparation failures

A failed preparation normally cancels generation without replacing a valid prior batch. If failures are explicitly ignored, the batch is marked as not fully import-ready and cannot use Fast Bake.

## 5. Bake the active batch

Select the generated `BatchN` collection itself in the Outliner so it becomes the active layer collection. Selecting only one of its objects is not enough.

Run **Bake Batch**. The operator:

1. Finds eligible meshes recursively.
2. Temporarily applies the requested Cycles bake settings.
3. Bakes receivers into the shared float image `NoisyLightmap`.
4. Atomically writes the full-resolution `LM_BN_Noisy.exr`.
5. Assigns the noisy image to generated material inputs named `LightMap`.
6. Restores changed scene, material, visibility, selection, and mode state.

The dialog provides adaptive sampling, direct and indirect clamping, glossy filtering, bounce limits, weak-light sampling, and direct/indirect contribution controls.

### Fast Bake

Fast Bake is enabled by default. It copies already-prepared receivers, groups compatible objects by Cycles ray visibility, joins each temporary group, hides the originals during baking, and removes all temporary objects and meshes afterward.

If a temporary join fails, the bake cancels instead of silently switching methods. Disable Fast Bake to use sequential per-object baking. Batches marked incomplete by ignored preparation failures cannot use Fast Bake.

### Shadow visibility

Receivers whose `visible_shadow` property is false are temporarily hidden from diffuse, glossy, transmission, volume-scatter, and shadow rays while other receivers bake. This prevents them from darkening neighboring assets through direct shadows or indirect occlusion. Their original visibility is restored afterward.

## 6. Denoise and finalize

Keep the baked `BatchN` active and run **Denoise Batch**. The operator reloads the persistent noisy EXR rather than treating the currently assigned image as authoritative.

It then:

1. Rebuilds valid texel coverage from current `LightMap` UVs.
2. Removes gutters left by the original bake.
3. Applies the requested final-resolution margin.
4. Denoises and downsamples in the selected order.
5. Atomically writes `LM_BN.exr`.
6. Assigns the final image to every compatible batch material.

Downsampling uses premultiplied filtering to avoid color bleeding from transparent pixels.

### Processing order

- **Denoise before downsampling** retains the full-resolution noisy source during filtering, then produces the final resolution.
- **Downsample before denoising** reduces the image first and denoises the smaller result.

### Margin behavior

- **None (Bake Margins)** keeps the original Blender bake margins. It does not crop to UV coverage or apply dilation.
- **Exclude margin from denoising** denoises island texels first, then dilates the clean result. This is the default.
- **Include margin in denoising** dilates noisy pixels first so gutters participate in denoising.

The margin value is measured in final-image pixels.

### Denoiser backends

PsychoVertexMaster currently uses these denoisers only for this custom lightmapping workflow. They do not change Blender's regular render denoising and are not used by other add-on features. Additional uses may be offered in future versions.

| Backend | Use |
| --- | --- |
| **Lightmap OIDN** | Default external backend using Intel Open Image Denoise's `RTLightmap` HDR filter. |
| **Integrated OIDN** | Uses Blender's built-in compositor denoiser in an isolated temporary scene. |
| **OptiX** | Uses [Declan Russell's](https://github.com/DeclanRussell/NvidiaAIDenoiser) legacy `Denoiser.exe` command line. |
| **None** | Keeps the image unfiltered while still applying shared resizing and output processing. |

**Lightmap OIDN is the preferred external backend** because Open Image Denoise provides the dedicated `RTLightmap` filter used by this pipeline.

In add-on Preferences, either browse to compatible executables or use the download buttons. The OIDN button installs the complete [Intel Open Image Denoise](https://github.com/RenderKit/oidn) 2.5.1 Windows distribution because the executable requires its adjacent libraries. The OptiX button installs the supported standalone executable.

External denoising is optional. Installing either OIDN or OptiX is sufficient, and neither is required when using **Integrated OIDN** or **None**. OIDN is the recommended external choice because this pipeline uses its dedicated `RTLightmap` filter.

The buttons download 64-bit Windows builds directly from third-party GitHub releases. OptiX 3.0 requires a compatible NVIDIA GPU and NVIDIA driver 565 or newer. Other platforms must provide a compatible OIDN executable manually where supported; the supplied OptiX adapter expects the Windows `Denoiser.exe` application.

These executables are neither developed nor bundled by PsychoVertexMaster. Intel Open Image Denoise uses the Apache 2.0 license, while Declan Russell's NVIDIA AI Denoiser uses the MIT License. Their upstream requirements and license terms apply.

If a custom preference path is blank, the add-on checks its local `denoisers` installation. External failure cancels the operation without replacing an existing valid final lightmap.

> NVIDIA's SDK sample named `optixDenoiser.exe` uses a different command line and is not interchangeable with the supported `Denoiser.exe` backend.

## 7. Replace repeated fillers

An optional top-level `FILLERS` collection may contain collection instances used to place repeated prepared assets. Organize its direct children by source batch: `F_X` corresponds to `S_X` and its generated `BatchN (S_X)`.

Make the generated `BatchN (S_X)` active, then run **Replace Fillers** before or after baking. The operator recursively matches instances in that batch's paired `F_X` collection by instanced collection name. It duplicates the matching prepared hierarchy into:

```text
EXPORT_STUFF/BATCH_FILLERS/FillersN
```

The filler instance's unapplied placement transform is preserved for later Unreal layout. Replacements remain outside `BatchN`, so future bakes do not include them. Only the paired `F_X` collection is hidden after its `FillersN` replacement succeeds; the `FILLERS` root, unrelated filler collections, and existing `BATCH_FILLERS/FillersN` collections for other batches remain unchanged. One source library collection may contain multiple objects. Unmatched filler instances are skipped, and a failed preflight changes nothing.

Run **Clear Filler Replacements** to remove `BATCH_FILLERS` and reveal the original `FILLERS` collection. Batches, UVs, materials, and baked lightmaps are preserved.

## 8. Preview lighting

Use **Display Lighting** to toggle generated material inputs named `LightingMode`. This lets you switch the prepared materials' lighting presentation without changing the baked files.

## 9. Cleanup

Run **Clear Lightmapping Stuff** carefully:

- If a generated `BatchN` is active, only that batch and its matching `FillersN` are removed, and its source collection is revealed.
- Otherwise, including when `SOURCE` is active, all generated lightmapping collections and temporary data are removed, `SOURCE` and `FILLERS` are revealed, the noisy compositor image is detached, and orphan data is purged.

The final case is intentionally broad and destructive. Save the file first if you may need the generated data.

## Troubleshooting

### No batch is accepted for baking or denoising

Activate the generated `BatchN` collection in the Outliner. Object selection alone does not identify a batch, and nested collections are not valid batch targets.

### Nothing can be packed or baked

Check that at least one visible-render mesh has a `LightMap` UV layer and a material marked **Light Baked**. Collision-prefixed and hidden-render meshes are intentionally excluded.

### UV packing fails

Confirm UVPackmaster 3 is installed and enabled. Verify that receiving meshes have `LightMap`, and do not remove the two-pass packing behavior when diagnosing density differences.

### Bake output cannot be written

Save the `.blend` and confirm the adjacent `Lightmaps` directory is writable. Output uses Blender-relative `//Lightmaps/...` paths.

### External denoiser is missing

Open PsychoVertexMaster preferences and download the relevant backend or browse to a compatible executable. A blank custom path uses the add-on-local installation only if it exists.

### Fast Bake is unavailable

Regenerate the batch without ignored preparation failures, or disable Fast Bake to use sequential baking.

### An operation was cancelled

Read the Blender status report and system console. The workflow is designed to restore temporary settings and preserve existing valid batch/lightmap output on recoverable failure, but Blender-dependent behavior should be tested on a small batch first.

## Recommended first test

Use one `SOURCE` child with one instanced mesh. Give it a `LightMap` UV layer and test faces with `lightmap_scale` values of `0`, a fractional value, and `1`. Include relevant baked, passthrough, transparent, linked, or empty material slots. Then test unpacking, baking, cancellation, repeated denoising, both processing orders, the intended denoiser backend, lighting preview, and cleanup before applying the workflow to production assets.
