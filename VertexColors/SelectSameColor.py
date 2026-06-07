import bpy
import bmesh
from bpy.types import Operator


class SelectSameColorOperator(Operator):
    bl_idname = "mesh.select_same_vertex_color"
    bl_label = "Select Matching"
    bl_description = "Select all faces with the same vertex color as the active face"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and obj.mode == 'EDIT'

    def color_to_byte_tuple(self, col):
        # Convert float [0-1] → int [0-255]
        return (
            int(col[0] * 255 + 0.5),
            int(col[1] * 255 + 0.5),
            int(col[2] * 255 + 0.5),
            int(col[3] * 255 + 0.5),
        )

    def execute(self, context):
        obj = context.active_object
        mesh = bmesh.from_edit_mesh(obj.data)
        color_layer = mesh.loops.layers.color.active
        active_face = mesh.faces.active

        if not active_face or not color_layer:
            self.report({'WARNING'}, "No active face or no vertex color layer found")
            return {'CANCELLED'}

        # reference color (converted to byte space)
        active_color = self.color_to_byte_tuple(
            active_face.loops[0][color_layer]
        )

        # clear selection
        for f in mesh.faces:
            f.select = False

        # compare using byte values
        for face in mesh.faces:
            col = self.color_to_byte_tuple(face.loops[0][color_layer])
            if col == active_color:
                face.select = True

        bmesh.update_edit_mesh(obj.data, loop_triangles=False)
        return {'FINISHED'}


def register():
    bpy.utils.register_class(SelectSameColorOperator)


def unregister():
    bpy.utils.unregister_class(SelectSameColorOperator)
