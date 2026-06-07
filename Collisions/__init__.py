import bpy
import bmesh
import numpy as np
from mathutils import Vector, Matrix
from bpy.app.handlers import persistent


class AddBoxCollisionToSelectedOperator(bpy.types.Operator):
    bl_idname = "collision.add_box_collision_to_selected"
    bl_label = "Add Box Collision"
    bl_description = (
        "Create an oriented box collision mesh fitted to the selected vertices "
        "using PCA, with a small configurable offset. The collision box is created "
        "as a separate object and the original mesh selection and edit mode are restored."
    )
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    offset: bpy.props.FloatProperty(
        name="Offset",
        description="Extra padding added to each side of the collision box",
        default=0.02
    )

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'EDIT_MESH'
            and context.edit_object is not None
        )

    def execute(self, context):
        offset = Vector((self.offset, self.offset, self.offset))

        obj = context.edit_object
        mesh = obj.data

        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()

        verts = [obj.matrix_world @ v.co for v in bm.verts if v.select]
        if len(verts) < 3:
            self.report({'ERROR'}, "Select at least 3 vertices")
            return {'CANCELLED'}

        # Convert to numpy
        points = np.array([v[:] for v in verts])
        centroid = points.mean(axis=0)
        centered = points - centroid

        # ---------- AABB (object-space) ----------
        inv_mtx = obj.matrix_world.inverted()
        local_points = np.array([(inv_mtx @ v)[:] for v in verts])

        aabb_min = local_points.min(axis=0)
        aabb_max = local_points.max(axis=0)
        aabb_extent = aabb_max - aabb_min
        aabb_volume = np.prod(aabb_extent)

        aabb_center_local = (aabb_min + aabb_max) * 0.5
        aabb_center_world = obj.matrix_world @ Vector(aabb_center_local)

        # PCA
        cov = np.cov(centered, rowvar=False)
        eigvals, eigvecs = np.linalg.eigh(cov)

        # Sort by descending eigenvalue (longest axis first)
        order = np.argsort(eigvals)[::-1]
        axes = eigvecs[:, order]

        # Transform points into PCA space
        pca_space = centered @ axes

        min_p = pca_space.min(axis=0)
        max_p = pca_space.max(axis=0)

        box_center_pca = (min_p + max_p) * 0.5
        box_size_pca = (max_p - min_p) + (np.array(offset) * 2.0)
        pca_volume = np.prod(max_p - min_p)

        # ---------- PCA confidence ----------
        alignment = np.abs(axes.T @ np.eye(3))
        confidence = alignment.max(axis=1).mean()

        ROTATION_GAIN = 0.9      # must reduce volume by 15%
        ROTATION_CONFIDENCE = 0.85
        print(confidence)

        use_pca = (
            pca_volume < aabb_volume * ROTATION_GAIN and
            confidence > ROTATION_CONFIDENCE
        )

        # Switch to Object Mode to create the box
        bpy.ops.object.mode_set(mode='OBJECT')

        bpy.ops.mesh.primitive_cube_add(size=1)
        box = context.active_object
        box.name = obj.name + "_Collision"

        if use_pca:
            # ---------- PCA OBB ----------
            box_center_world = Vector(centroid + axes @ box_center_pca)

            rot = Matrix((
                axes[:, 0],
                axes[:, 1],
                axes[:, 2],
            )).transposed().to_4x4()

            box.location = box_center_world
            box.matrix_world = Matrix.Translation(box.location) @ rot
            box.scale = Vector(box_size_pca)

        else:
            # ---------- AABB fallback ----------
            box.location = aabb_center_world
            box.rotation_euler = obj.rotation_euler
            box.scale = Vector(aabb_extent) + offset * 2.0

        # Apply scale only
        bpy.ops.object.select_all(action='DESELECT')
        box.select_set(True)
        context.view_layer.objects.active = box
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        box.users_collection[0].objects.unlink(box)
        obj.users_collection[0].objects.link(box)

        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.converttoboxcollision()
        val = context.scene.display_collisions
        box.display_type = 'SOLID' if val else 'WIRE'
        box.show_wire = val
        box.data.materials.clear()
        box.data.materials.append(bpy.data.materials.get("MI_Collision"))

        # Restore original selection and edit mode
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')

        return {'FINISHED'}


def OnDisplayCollisionsChanged(self, context: bpy.types.Context):
    val = self.display_collisions
    for name, obj in bpy.data.objects.items():
        if name.startswith("UBX_") or name.startswith("UCX_") or name.startswith("UCP_") or name.startswith("USP_"):
            obj.display_type = 'SOLID' if val else 'WIRE'
            obj.hide_viewport = not val
            obj.hide_render = not val
            obj.show_wire = val


@persistent
def InitDisplayCollisions(dummy):
    OnDisplayCollisionsChanged(bpy.context.scene, bpy.context)


def register():
    bpy.utils.register_class(AddBoxCollisionToSelectedOperator)

    bpy.types.Scene.display_collisions = bpy.props.BoolProperty(
        name="Display Collisions",
        default=False,
        update=OnDisplayCollisionsChanged
    )
    bpy.app.handlers.load_post.append(InitDisplayCollisions)


def unregister():
    bpy.utils.unregister_class(AddBoxCollisionToSelectedOperator)

    del bpy.types.Scene.display_collisions
    bpy.app.handlers.load_post.remove(InitDisplayCollisions)
