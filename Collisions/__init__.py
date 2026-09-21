import bpy
import bmesh
import json
import numpy as np
from mathutils import Vector, Matrix
from bpy.app.handlers import persistent


def _current_collision_source_points(context):
    is_edit_mode = context.mode == 'EDIT_MESH'
    source = context.edit_object if is_edit_mode else context.active_object
    if source is None or source.type != 'MESH':
        return None, [], is_edit_mode

    if is_edit_mode:
        source_bm = bmesh.from_edit_mesh(source.data)
        points = [source.matrix_world @ vert.co for vert in source_bm.verts if vert.select]
    else:
        points = [source.matrix_world @ vert.co for vert in source.data.vertices]
    return source, points, is_edit_mode


def _capture_collision_input(operator, context):
    source, points, is_edit_mode = _current_collision_source_points(context)
    operator.source_name = source.name if source is not None else ""
    operator.source_points = json.dumps([list(point) for point in points])
    operator.started_in_edit_mode = is_edit_mode


def _collision_source_points(operator, context):
    if not operator.source_name:
        _capture_collision_input(operator, context)
    source = bpy.data.objects.get(operator.source_name)
    points = [Vector(point) for point in json.loads(operator.source_points)]
    return source, points, operator.started_in_edit_mode


def _restore_collision_source(context, source, edit_mode):
    bpy.ops.object.select_all(action='DESELECT')
    source.select_set(True)
    context.view_layer.objects.active = source
    if edit_mode:
        bpy.ops.object.mode_set(mode='EDIT')


def _collision_poll(context):
    source = context.edit_object if context.mode == 'EDIT_MESH' else context.active_object
    return context.mode in {'OBJECT', 'EDIT_MESH'} and source is not None and source.type == 'MESH'


class AddBoxCollisionToSelectedOperator(bpy.types.Operator):
    bl_idname = "collision.add_box_collision_to_selected"
    bl_label = "Add Box Collision"
    bl_description = (
        "Create an oriented box collision mesh fitted to the selected vertices "
        "using PCA, with a small configurable offset. The collision box is created "
        "as a separate object and the original mesh selection and edit mode are restored."
    )
    bl_options = {'REGISTER', 'UNDO'}

    source_name: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    source_points: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    started_in_edit_mode: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    offset: bpy.props.FloatProperty(
        name="Offset",
        description="Extra padding added to each side of the collision box",
        default=0.02
    )

    @classmethod
    def poll(cls, context):
        return _collision_poll(context)

    def invoke(self, context, event):
        _capture_collision_input(self, context)
        return self.execute(context)

    def execute(self, context):
        offset = Vector((self.offset, self.offset, self.offset))

        obj, verts, was_edit_mode = _collision_source_points(self, context)
        if len(verts) < 3:
            self.report({'ERROR'}, "Select at least 3 vertices, or use a mesh with at least 3 vertices")
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

        if was_edit_mode:
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

        _restore_collision_source(context, obj, was_edit_mode)

        return {'FINISHED'}


class AddSphereCollisionToSelectedOperator(bpy.types.Operator):
    bl_idname = "collision.add_sphere_collision_to_selected"
    bl_label = "Add Sphere Collision"
    bl_description = (
        "Create a spherical UE5 USP collision mesh fitted to the selected vertices "
        "with a configurable offset, then restore the original mesh selection and Edit Mode"
    )
    bl_options = {'REGISTER', 'UNDO'}

    source_name: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    source_points: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    started_in_edit_mode: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    offset: bpy.props.FloatProperty(
        name="Offset",
        description="Extra padding added to the collision sphere radius",
        default=0.02,
        min=0.0,
    )

    @classmethod
    def poll(cls, context):
        return _collision_poll(context)

    def invoke(self, context, event):
        _capture_collision_input(self, context)
        return self.execute(context)

    def execute(self, context):
        source, points, was_edit_mode = _collision_source_points(self, context)
        if len(points) < 2:
            self.report({'ERROR'}, "Select at least 2 vertices, or use a mesh with at least 2 vertices")
            return {'CANCELLED'}

        center = sum(points, Vector((0.0, 0.0, 0.0))) / len(points)
        radius = max((point - center).length for point in points) + self.offset
        if radius <= 0.0:
            self.report({'ERROR'}, "Selected vertices do not define a valid sphere")
            return {'CANCELLED'}

        if was_edit_mode:
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=1.0, location=center)
        sphere = context.active_object
        sphere.name = source.name + "_Collision"
        sphere.scale = Vector((radius, radius, radius))

        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        sphere.users_collection[0].objects.unlink(sphere)
        source.users_collection[0].objects.link(sphere)

        source.select_set(True)
        context.view_layer.objects.active = source
        bpy.ops.object.converttospherecollision()

        show_collisions = context.scene.display_collisions
        sphere.display_type = 'SOLID' if show_collisions else 'WIRE'
        sphere.show_wire = show_collisions
        sphere.data.materials.clear()
        material = bpy.data.materials.get("MI_Collision")
        if material is not None:
            sphere.data.materials.append(material)

        _restore_collision_source(context, source, was_edit_mode)
        return {'FINISHED'}


class AddCapsuleCollisionToSelectedOperator(bpy.types.Operator):
    bl_idname = "collision.add_capsule_collision_to_selected"
    bl_label = "Add Capsule Collision"
    bl_description = (
        "Create a PCA-aligned UE5 UCP capsule fitted to the selected vertices, "
        "or to the entire active mesh in Object Mode"
    )
    bl_options = {'REGISTER', 'UNDO'}

    source_name: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    source_points: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    started_in_edit_mode: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    offset: bpy.props.FloatProperty(
        name="Offset",
        description="Extra padding added around the collision capsule",
        default=0.02,
        min=0.0,
    )

    @classmethod
    def poll(cls, context):
        return _collision_poll(context)

    def invoke(self, context, event):
        _capture_collision_input(self, context)
        return self.execute(context)

    def execute(self, context):
        source, points, was_edit_mode = _collision_source_points(self, context)
        if len(points) < 2:
            self.report({'ERROR'}, "Select at least 2 vertices, or use a mesh with at least 2 vertices")
            return {'CANCELLED'}

        point_array = np.array([point[:] for point in points])
        centroid = point_array.mean(axis=0)
        centered = point_array - centroid
        covariance = np.cov(centered, rowvar=False)
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        axis = Vector(eigenvectors[:, np.argmax(eigenvalues)]).normalized()

        projections = [Vector(point - centroid).dot(axis) for point in point_array]
        projection_min = min(projections)
        projection_max = max(projections)
        center = Vector(centroid) + axis * ((projection_min + projection_max) * 0.5)
        radial_radius = max(
            (Vector(point) - center - axis * (Vector(point) - center).dot(axis)).length
            for point in point_array
        )
        radius = radial_radius + self.offset
        axial_half = (projection_max - projection_min) * 0.5 + self.offset
        cylinder_half = max(0.0, axial_half - radius)
        if radius <= 0.0:
            self.report({'ERROR'}, "Selected vertices do not define a valid capsule")
            return {'CANCELLED'}

        capsule_bm = bmesh.new()
        try:
            result = bmesh.ops.create_uvsphere(
                capsule_bm,
                u_segments=16,
                v_segments=8,
                radius=radius,
                matrix=Matrix.Identity(4),
            )
            rotation = Vector((0.0, 0.0, 1.0)).rotation_difference(axis)
            for vert in result['verts']:
                if cylinder_half > 0.0:
                    vert.co.z += cylinder_half if vert.co.z >= 0.0 else -cylinder_half
                vert.co = center + rotation @ vert.co

            if was_edit_mode:
                bpy.ops.object.mode_set(mode='OBJECT')

            capsule_mesh = bpy.data.meshes.new(source.name + "_CapsuleCollision")
            capsule_bm.to_mesh(capsule_mesh)
            capsule_mesh.update()
            capsule = bpy.data.objects.new(source.name + "_Collision", capsule_mesh)
            source.users_collection[0].objects.link(capsule)

            bpy.ops.object.select_all(action='DESELECT')
            capsule.select_set(True)
            source.select_set(True)
            context.view_layer.objects.active = source
            bpy.ops.object.converttocapsulecollision()

            show_collisions = context.scene.display_collisions
            capsule.display_type = 'SOLID' if show_collisions else 'WIRE'
            capsule.show_wire = show_collisions
            capsule.data.materials.clear()
            material = bpy.data.materials.get("MI_Collision")
            if material is not None:
                capsule.data.materials.append(material)

            _restore_collision_source(context, source, was_edit_mode)
            return {'FINISHED'}
        finally:
            capsule_bm.free()


class AddConvexCollisionToSelectedOperator(bpy.types.Operator):
    bl_idname = "collision.add_convex_collision_to_selected"
    bl_label = "Add Convex Collision"
    bl_description = (
        "Create a strictly convex UE5 UCX collision mesh from the selected vertices "
        "and restore the original mesh selection and Edit Mode"
    )
    bl_options = {'REGISTER', 'UNDO'}

    source_name: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    source_points: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    started_in_edit_mode: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    offset: bpy.props.FloatProperty(
        name="Offset",
        description="Extra radial padding added around the convex collision hull",
        default=0.02,
        min=0.0,
    )

    @classmethod
    def poll(cls, context):
        return _collision_poll(context)

    def invoke(self, context, event):
        _capture_collision_input(self, context)
        return self.execute(context)

    def execute(self, context):
        source, points, was_edit_mode = _collision_source_points(self, context)
        if len(points) < 4:
            self.report({'ERROR'}, "Select at least 4 vertices enclosing a 3D volume")
            return {'CANCELLED'}

        if self.offset > 0.0:
            center = sum(points, Vector((0.0, 0.0, 0.0))) / len(points)
            padded_points = []
            for point in points:
                direction = point - center
                if direction.length_squared > 1e-12:
                    point = point + direction.normalized() * self.offset
                padded_points.append(point)
            points = padded_points

        hull_bm = bmesh.new()
        try:
            hull_verts = [hull_bm.verts.new(point) for point in points]
            bmesh.ops.remove_doubles(hull_bm, verts=hull_verts, dist=1e-6)
            if len(hull_bm.verts) < 4:
                self.report({'ERROR'}, "The selection needs at least 4 unique vertices")
                return {'CANCELLED'}

            result = bmesh.ops.convex_hull(
                hull_bm,
                input=list(hull_bm.verts),
                use_existing_faces=False,
            )
            discard = set(result.get("geom_interior", ()))
            discard.update(result.get("geom_unused", ()))
            discard.update(result.get("geom_holes", ()))
            if discard:
                bmesh.ops.delete(hull_bm, geom=list(discard), context='VERTS')

            if len(hull_bm.faces) < 4 or abs(hull_bm.calc_volume(signed=True)) <= 1e-12:
                self.report({'ERROR'}, "Selected vertices are coplanar or do not enclose a valid volume")
                return {'CANCELLED'}

            bmesh.ops.recalc_face_normals(hull_bm, faces=list(hull_bm.faces))

            index = 0
            while True:
                collision_name = f"UCX_{source.name}_{index:02d}"
                if bpy.data.objects.get(collision_name) is None:
                    break
                index += 1

            if was_edit_mode:
                bpy.ops.object.mode_set(mode='OBJECT')
            collision_mesh = bpy.data.meshes.new(collision_name)
            hull_bm.to_mesh(collision_mesh)
            collision_mesh.update()
            collision = bpy.data.objects.new(collision_name, collision_mesh)
            context.collection.objects.link(collision)
            collision.users_collection[0].objects.unlink(collision)
            source.users_collection[0].objects.link(collision)
            collision.parent = source
            collision.matrix_parent_inverse = source.matrix_world.inverted()

            show_collisions = context.scene.display_collisions
            collision.display_type = 'SOLID' if show_collisions else 'WIRE'
            collision.show_wire = show_collisions
            collision_mesh.materials.clear()
            material = bpy.data.materials.get("MI_Collision")
            if material is not None:
                collision_mesh.materials.append(material)

            _restore_collision_source(context, source, was_edit_mode)
            return {'FINISHED'}
        finally:
            hull_bm.free()


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
    bpy.utils.register_class(AddSphereCollisionToSelectedOperator)
    bpy.utils.register_class(AddCapsuleCollisionToSelectedOperator)
    bpy.utils.register_class(AddConvexCollisionToSelectedOperator)

    bpy.types.Scene.display_collisions = bpy.props.BoolProperty(
        name="Display Collisions",
        default=False,
        update=OnDisplayCollisionsChanged
    )
    bpy.app.handlers.load_post.append(InitDisplayCollisions)


def unregister():
    bpy.utils.unregister_class(AddConvexCollisionToSelectedOperator)
    bpy.utils.unregister_class(AddCapsuleCollisionToSelectedOperator)
    bpy.utils.unregister_class(AddSphereCollisionToSelectedOperator)
    bpy.utils.unregister_class(AddBoxCollisionToSelectedOperator)

    del bpy.types.Scene.display_collisions
    bpy.app.handlers.load_post.remove(InitDisplayCollisions)
