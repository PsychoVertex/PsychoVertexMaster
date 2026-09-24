# PsychoVertex Scene Reconstructor (UE 5.6)

1. Copy `PsychoVertexSceneReconstructor` into the Unreal project's `Plugins` directory.
2. Regenerate project files if required, build the Editor target, and enable the plugin.
3. Import the Blender-exported FBXs manually as Static Meshes. Keep their generated `SM_*` names, enable scene-unit conversion, import custom collision, and configure materials yourself.
4. Open the destination level and choose **Window → PsychoVertex Scene Reconstructor**.
5. Select the Content Browser folder containing the meshes, browse to `PVMScene.json`, validate, then reconstruct.

The asset search is recursive below the selected `/Game` folder. Reconstruction never imports or modifies assets. A repeated reconstruction replaces only actors carrying the same `PVMScene=<reconstruction_id>` tag and is recorded as one editor undo transaction.

**Delete PVM Folder Actors** removes every actor whose World Outliner folder is `PVM` or a child path such as `PVM/Batch2`. It asks for confirmation and records the deletion as one undoable editor transaction.
