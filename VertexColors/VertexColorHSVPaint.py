import colorsys
import bpy
import bmesh
from bpy.types import Operator
import gpu
from gpu_extras.batch import batch_for_shader
from gpu.state import depth_test_set, blend_set

SELECTOR_SIZE = 200


def get_active_color_attribute(obj):
    return obj.data.color_attributes.active_color


def remap(value, old_min, old_max, new_min, new_max, clamp=True):
    result = new_min + (value - old_min) * (new_max - new_min) / (old_max - old_min)
    if clamp:
        result = min(new_max, max(new_min, result))
    return result


class VertexColorHSVPaintOperator(Operator):
    """Paint active vertex/face color using mouse HSV"""
    bl_idname = "mesh.vertex_color_hsv_paint"
    bl_label = "HSV Mouse Paint"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    start_x = 0.0
    start_y = 0.0
    mouse_x = 0.0
    mouse_y = 0.0
    value = 1.0
    show_overlays = False
    _hsv_drawer_handle = None
    shading_color_type = 'MATERIAL'
    shading_light = 'STUDIO'
    shading_type = 'MATERIAL_PREVIEW'

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and obj.mode == 'EDIT'

    def invoke(self, context, event):
        self.start_x = event.mouse_region_x
        self.start_y = event.mouse_region_y
        self.mouse_x = event.mouse_region_x
        self.mouse_y = event.mouse_region_y
        self.value = 1.0
        if context.area.type == 'VIEW_3D':
            space = context.area.spaces[0]
            self.shading_type = space.shading.type
            self.shading_light = space.shading.light
            self.shading_color_type = space.shading.color_type
            space.shading.type = 'MATERIAL'
            space.shading.color_type = 'VERTEX'
            space.shading.light = 'FLAT'
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('PAINT_BRUSH')
        self.show_overlays = context.area.spaces.active.overlay.show_overlays
        context.area.spaces.active.overlay.show_overlays = False
        self._hsv_drawer_handle = bpy.types.SpaceView3D.draw_handler_add(self.draw_hsv_palette, (context,), 'WINDOW', 'POST_PIXEL')
        return {'RUNNING_MODAL'}

    def finish(self, context):
        if context.area.type == 'VIEW_3D':
            space = context.area.spaces[0]
            space.shading.type = self.shading_type
            space.shading.light = self.shading_light
            space.shading.color_type = self.shading_color_type
        context.area.spaces.active.overlay.show_overlays = self.show_overlays
        context.window.cursor_modal_restore()
        if self._hsv_drawer_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._hsv_drawer_handle, 'WINDOW')
            del self._hsv_drawer_handle

    def draw_hsv_palette(self, context):
        step = 5
        vertices = []
        colors = []
        indices = []
        quad_index = 0
        for x in range(0, SELECTOR_SIZE, step):
            for y in range(0, SELECTOR_SIZE, step):
                hue = x / SELECTOR_SIZE
                sat = y / SELECTOR_SIZE
                r, g, b = colorsys.hsv_to_rgb(hue, sat, self.value)

                x0 = self.start_x + x - SELECTOR_SIZE/2
                y0 = self.start_y + y - SELECTOR_SIZE/2
                x1 = x0 + step
                y1 = y0 + step

                # Add vertices for the quad
                vertices.extend([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
                colors.extend([(r, g, b, 1.0)]*4)

                # Add indices for two triangles
                indices.extend([
                    (quad_index*4 + 0, quad_index*4 + 1, quad_index*4 + 2),
                    (quad_index*4 + 2, quad_index*4 + 3, quad_index*4 + 0)
                ])
                quad_index += 1

        shader = gpu.shader.from_builtin('FLAT_COLOR')
        batch = batch_for_shader(shader, 'TRIS', {"pos": vertices, "color": colors}, indices=indices)

        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        shader.bind()
        batch.draw(shader)
        gpu.state.blend_set('NONE')

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self.finish(context)
            return {'CANCELLED'}

        if event.type == 'MOUSEMOVE':
            self.mouse_x = event.mouse_region_x
            self.mouse_y = event.mouse_region_y
            self.apply_color(context)

        elif event.type == 'WHEELUPMOUSE':
            self.value = min(1.0, self.value + 0.05)
            self.apply_color(context)

        elif event.type == 'WHEELDOWNMOUSE':
            self.value = max(0.0, self.value - 0.05)
            self.apply_color(context)

        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self.finish(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def apply_color(self, context):
        hue = remap(self.mouse_x, self.start_x - SELECTOR_SIZE / 2, self.start_x + SELECTOR_SIZE / 2, 0, 1)
        sat = remap(self.mouse_y, self.start_y - SELECTOR_SIZE / 2, self.start_y + SELECTOR_SIZE / 2, 0, 1)
        r, g, b = colorsys.hsv_to_rgb(hue, sat, self.value)
        new_color = (r, g, b, 1.0)

        ts = context.tool_settings
        selected_objs = [obj for obj in getattr(context, "objects_in_mode_unique_data", ()) if obj.type == 'MESH']

        for obj in selected_objs:
            bm = bmesh.from_edit_mesh(obj.data)
            color_attribute = get_active_color_attribute(obj)
            if not color_attribute:
                color_layer = bm.loops.layers.color.get('Color') or bm.loops.layers.color.new('Color')
                domain = "CORNER"
            else:
                domain = color_attribute.domain

            if domain == "POINT":
                layers = bm.verts.layers.float_color if color_attribute.data_type == 'FLOAT_COLOR' else bm.verts.layers.color
                color_layer = layers.get(color_attribute.name)
                if not color_layer:
                    continue
                for v in bm.verts:
                    if v.select:
                        v[color_layer] = new_color
            elif domain == "CORNER":
                if color_attribute:
                    layers = bm.loops.layers.float_color if color_attribute.data_type == 'FLOAT_COLOR' else bm.loops.layers.color
                    color_layer = layers.get(color_attribute.name)
                    if not color_layer:
                        continue
                select_mode = ts.mesh_select_mode
                affected_loops = set()
                if select_mode[2]:
                    affected_loops.update(loop for face in bm.faces if face.select for loop in face.loops)
                elif select_mode[1]:
                    selected_verts = {vert for edge in bm.edges if edge.select for vert in edge.verts}
                    affected_loops.update(loop for face in bm.faces for loop in face.loops if loop.vert in selected_verts)
                elif select_mode[0]:
                    affected_loops.update(loop for face in bm.faces for loop in face.loops if loop.vert.select)
                for loop in affected_loops:
                    loop[color_layer] = new_color

            bmesh.update_edit_mesh(obj.data, loop_triangles=False)


def register():
    bpy.utils.register_class(VertexColorHSVPaintOperator)


def unregister():
    bpy.utils.unregister_class(VertexColorHSVPaintOperator)
