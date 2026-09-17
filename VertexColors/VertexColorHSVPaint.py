import colorsys
import bpy
from bpy.types import Operator, Object
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

        # Save original active object
        original_active: Object | None = context.view_layer.objects.active
        if original_active:
            original_active.select_set(True)

        ts = context.tool_settings
        selected_objs = [obj for obj in context.selected_objects if obj.type == 'MESH' and obj.mode == 'EDIT']

        obj: Object
        for obj in selected_objs:
            context.view_layer.objects.active = obj  # Make this object active
            bpy.ops.object.mode_set(mode="OBJECT")

            mesh = obj.data
            color_attribute = get_active_color_attribute(obj)
            if not color_attribute:
                color_attribute = obj.data.color_attributes.new('Color', 'BYTE_COLOR', 'CORNER')
            print(color_attribute.domain, ts.mesh_select_mode)
            # POINT domain
            if color_attribute.domain == "POINT":
                for v in mesh.vertices:
                    if v.select:
                        color_attribute.data[v.index].color_srgb = new_color
            # CORNER domain
            elif color_attribute.domain == "CORNER":
                select_mode = ts.mesh_select_mode
                loops = mesh.loops
                affected_loops = set()
                if select_mode[2]:  # Face select
                    for poly in mesh.polygons:
                        if poly.select:
                            affected_loops.update(poly.loop_indices)
                elif select_mode[1]:  # Edge select
                    selected_verts = {v for e in mesh.edges if e.select for v in e.vertices}
                    for poly in mesh.polygons:
                        for li in poly.loop_indices:
                            if loops[li].vertex_index in selected_verts:
                                affected_loops.add(li)
                elif select_mode[0]:  # Vertex select
                    selected_verts = {v.index for v in mesh.vertices if v.select}
                    for poly in mesh.polygons:
                        for li in poly.loop_indices:
                            if loops[li].vertex_index in selected_verts:
                                affected_loops.add(li)
                data = color_attribute.data
                for li in affected_loops:
                    if li < len(data):  # safety check
                        data[li].color_srgb = new_color

            bpy.ops.object.mode_set(mode="EDIT")

        # Restore original active object
        context.view_layer.objects.active = original_active


def register():
    bpy.utils.register_class(VertexColorHSVPaintOperator)


def unregister():
    bpy.utils.unregister_class(VertexColorHSVPaintOperator)
