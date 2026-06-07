import bpy


class SetOriginToSelectionAndRotateOperator(bpy.types.Operator):
    bl_idname = "mesh.set_origin_to_selection_and_rotate"
    bl_label = "Set Origin to Selection And Rotate"
    bl_options = {"UNDO", 'UNDO_GROUPED'}

    @classmethod
    def poll(cls, context):
        ao = context.active_object
        ao.update_from_editmode()
        so = context.selected_objects
        selected_faces = [p for p in ao.data.polygons if p.select == True]
        return ao is not None and len(so) == 1 and so[0] == ao and len(selected_faces) == 1

    def execute(self, context):
        ao = context.active_object
        ao.update_from_editmode()

        normal = [p for p in ao.data.polygons if p.select == True][0].normal
        quat = normal.to_track_quat('Z', 'Y')
        quat.invert()
        ao.rotation_mode = "QUATERNION"
        ao.rotation_quaternion.rotate(quat)
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        bpy.ops.object.mode_set(mode='EDIT')
        ao.rotation_mode = "XYZ"
        return {'FINISHED'}


def register():
    bpy.utils.register_class(SetOriginToSelectionAndRotateOperator)


def unregister():
    bpy.utils.unregister_class(SetOriginToSelectionAndRotateOperator)
