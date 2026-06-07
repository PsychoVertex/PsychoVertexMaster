import bpy


def main(context, x, y, z):
    ao = context.active_object
    so = context.selected_objects[0]
    mesh = ao.data
    verts = []
    for (i, v) in enumerate(mesh.vertices):
        if v.select:
            verts.append(i)
    vg = ao.vertex_groups.new(name='_SNAP_TO_SURFACE_')

    so.select_set(False)
    bpy.ops.object.mode_set(mode='OBJECT')
    vg.add(verts, 1.0, "ADD")
    bpy.ops.object.mode_set(mode='EDIT')
    so.select_set(True)

    mod = ao.modifiers.new("_SNAP_TO_SURFACE_", "SHRINKWRAP")
    mod.wrap_method = "PROJECT"
    mod.use_project_x = x
    mod.use_project_y = y
    mod.use_project_z = z
    mod.use_negative_direction = True
    mod.use_positive_direction = True
    mod.target = so
    mod.vertex_group = vg.name

    so.select_set(False)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.ops.object.mode_set(mode='EDIT')
    so.select_set(True)

    ao.vertex_groups.remove(vg)


class SnapVerticesToSurfaceOperator(bpy.types.Operator):
    bl_idname = "mesh.snap_vertices_to_surface"
    bl_label = "Snap Vertices To Surface"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    x_axis: bpy.props.BoolProperty(name="X", default=False)
    y_axis: bpy.props.BoolProperty(name="Y", default=False)
    z_axis: bpy.props.BoolProperty(name="Z", default=False)

    @classmethod
    def poll(cls, context):
        ao = context.active_object
        so = context.selected_objects
        so = [o for o in so if o != ao]
        validation1 = ao and ao.type == 'MESH' and ao.mode == 'EDIT'
        validation2 = len(so) == 1 and so[0].type == 'MESH' and so[0].mode == 'OBJECT'
        return validation1 and validation2

    def execute(self, context):
        main(context, self.x_axis, self.y_axis, self.z_axis)
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.label(text="Global")
        row.prop(self, "x_axis", toggle=True)
        row.prop(self, "y_axis", toggle=True)
        row.prop(self, "z_axis", toggle=True)


def add_menu_item(self, context):
    layout = self.layout
    layout.separator()
    layout.operator("mesh.snap_vertices_to_surface")


def register():
    bpy.utils.register_class(SnapVerticesToSurfaceOperator)
    bpy.types.VIEW3D_MT_edit_mesh_vertices.append(add_menu_item)


def unregister():
    bpy.utils.unregister_class(SnapVerticesToSurfaceOperator)
    bpy.types.VIEW3D_MT_edit_mesh_vertices.remove(add_menu_item)
