import bpy
import bmesh
from bpy.types import Operator


class CopyColorOperator(Operator):
    bl_idname = "mesh.copy_vertex_color"
    bl_label = "Copy"
    bl_description = "Copies the vertex color assigned to the active face to the clipboard"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and obj.mode == 'EDIT'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data

        bm = bmesh.from_edit_mesh(mesh)
        color_layer = bm.loops.layers.color.active

        if not color_layer:
            self.report({'WARNING'}, "No active vertex color layer found")
            return {'CANCELLED'}

        # Get the active face
        active_face = bm.faces.active
        if not active_face:
            self.report({'WARNING'}, "No active face selected")
            return {'CANCELLED'}

        # Average the colors of the face's vertices
        r, g, b = 0.0, 0.0, 0.0
        for loop in active_face.loops:
            col = loop[color_layer]
            r += col[0]
            g += col[1]
            b += col[2]

        count = len(active_face.loops)
        r /= count
        g /= count
        b /= count

        # Convert to hex string
        hex_color = f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"

        # Store in clipboard
        context.window_manager.clipboard = hex_color

        self.report({'INFO'}, f"Copied color {hex_color} to clipboard")
        return {'FINISHED'}


class PasteColorOperator(Operator):
    bl_idname = "mesh.paste_vertex_color"
    bl_label = "Paste"
    bl_description = "Pastes the vertex color from the clipboard to all selected faces"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and obj.mode == 'EDIT'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data

        bm = bmesh.from_edit_mesh(mesh)
        color_layer = bm.loops.layers.color.active

        if not color_layer:
            self.report({'WARNING'}, "No active vertex color layer found")
            return {'CANCELLED'}

        selected_faces = [f for f in bm.faces if f.select]
        if not selected_faces:
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        # Get color from clipboard
        hex_color = context.window_manager.clipboard.strip()
        if not hex_color.startswith("#") or len(hex_color) != 7:
            if len(hex_color) == 6:
                hex_color = "#" + hex_color
            else:
                self.report({'WARNING'}, "Clipboard does not contain a valid hex color (#RRGGBB)")
                return {'CANCELLED'}

        # Convert hex to RGB
        r = int(hex_color[1:3], 16) / 255.0
        g = int(hex_color[3:5], 16) / 255.0
        b = int(hex_color[5:7], 16) / 255.0

        # Apply color to all selected faces
        for face in selected_faces:
            for loop in face.loops:
                loop[color_layer] = (r, g, b, 1.0)  # RGBA, alpha = 1

        bmesh.update_edit_mesh(mesh)
        self.report({'INFO'}, f"Pasted color {hex_color} to {len(selected_faces)} faces")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(CopyColorOperator)
    bpy.utils.register_class(PasteColorOperator)


def unregister():
    bpy.utils.unregister_class(CopyColorOperator)
    bpy.utils.unregister_class(PasteColorOperator)
