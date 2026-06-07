import bpy


class SetOriginToSelectionOperator(bpy.types.Operator):
    bl_idname = "mesh.set_origin_to_selection"
    bl_label = "Set Origin to Selection"
    bl_options = {"UNDO", 'UNDO_GROUPED'}

    @classmethod
    def poll(cls, context):
        ao = context.active_object
        so = context.selected_objects
        return ao is not None and len(so) == 1 and so[0] == ao

    def execute(self, context):
        cursor = bpy.context.scene.cursor
        curloc = (cursor.location[0], cursor.location[1], cursor.location[2])
        bpy.ops.view3d.snap_cursor_to_selected()
        bpy.ops.object.editmode_toggle()
        bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
        bpy.ops.object.editmode_toggle()
        cursor.location = curloc
        return {'FINISHED'}


def register():
    bpy.utils.register_class(SetOriginToSelectionOperator)


def unregister():
    bpy.utils.unregister_class(SetOriginToSelectionOperator)
