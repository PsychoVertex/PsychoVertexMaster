import bpy


class SelectedOriginsToActiveOperator(bpy.types.Operator):
    bl_idname = "object.selected_origins_to_active"
    bl_label = "Selected Origins To Active"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    def execute(self, context):
        ctx = bpy.context
        ao = ctx.active_object
        sos = [so for so in ctx.selected_objects if so != ao]

        if ao is not None:
            cursor = bpy.context.scene.cursor
            cursor.location = ao.location
            cursor.rotation_euler = ao.rotation_euler

        bpy.ops.object.origin_set(type="ORIGIN_CURSOR")

        self.report({'INFO'}, f"Origins Are Set")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(SelectedOriginsToActiveOperator)


def unregister():
    bpy.utils.unregister_class(SelectedOriginsToActiveOperator)
