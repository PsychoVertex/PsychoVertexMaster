import colorsys
import bmesh
import bpy
import gpu
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader

PICKER_SIZE = 220
HUE_WIDTH = 24
PICKER_GAP = 12
PICKER_PADDING = 14


class VertexColorHSVPaintOperator(Operator):
    """Choose hue, saturation, and value directly under the mouse"""

    bl_idname = "mesh.vertex_color_hsv_paint"
    bl_label = "HSV Mouse Paint"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED', 'BLOCKING'}
    _draw_handle = None

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.area and context.area.type == 'VIEW_3D' and obj
                and obj.type == 'MESH' and obj.mode == 'EDIT')

    def invoke(self, context, event):
        self._area = context.area
        self._targets = []
        self._last_color = None
        self._finished = False
        self._active_control = None
        if not self._cache_targets(context):
            self.report({'WARNING'}, "No selected geometry with an editable color attribute")
            return {'CANCELLED'}

        initial = self._targets[0][4][0][1]
        self.hue, self.saturation, self.value = colorsys.rgb_to_hsv(*initial[:3])
        self._place_picker(context, event)
        self._build_static_batches()
        self._build_dynamic_batches()

        space = context.space_data
        self._shading = (space.shading.type, space.shading.light, space.shading.color_type)
        self._show_overlays = space.overlay.show_overlays
        space.shading.type = 'MATERIAL'
        space.shading.color_type = 'VERTEX'
        space.shading.light = 'FLAT'
        space.overlay.show_overlays = False
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_picker, (), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('CROSSHAIR')
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _cache_targets(self, context):
        select_mode = context.tool_settings.mesh_select_mode
        for obj in getattr(context, "objects_in_mode_unique_data", ()):
            if obj.type != 'MESH':
                continue
            bm = bmesh.from_edit_mesh(obj.data)
            attribute = obj.data.color_attributes.active_color
            if attribute and attribute.domain == 'POINT':
                data_type = attribute.data_type
                name = attribute.name
                layers = bm.verts.layers.float_color if data_type == 'FLOAT_COLOR' else bm.verts.layers.color
                layer = layers.get(attribute.name)
                bm.verts.ensure_lookup_table()
                bm.verts.index_update()
                keys = [vert.index for vert in bm.verts if vert.select]
                originals = [(key, tuple(bm.verts[key][layer])) for key in keys] if layer else []
                domain = 'POINT'
            else:
                if attribute and attribute.domain != 'CORNER':
                    continue
                name = attribute.name if attribute else 'Color'
                data_type = attribute.data_type if attribute else 'BYTE_COLOR'
                layers = bm.loops.layers.float_color if data_type == 'FLOAT_COLOR' else bm.loops.layers.color
                layer = layers.get(name) or layers.new(name)
                bm.faces.ensure_lookup_table()
                bm.faces.index_update()
                if select_mode[2]:
                    faces = [face for face in bm.faces if face.select]
                    selected_verts = None
                elif select_mode[1]:
                    selected = {vert for edge in bm.edges if edge.select for vert in edge.verts}
                    faces = bm.faces
                    selected_verts = selected
                else:
                    faces = bm.faces
                    selected_verts = {vert for vert in bm.verts if vert.select}
                originals = [
                    ((face.index, loop_index), tuple(loop[layer]))
                    for face in faces
                    for loop_index, loop in enumerate(face.loops)
                    if selected_verts is None or loop.vert in selected_verts
                ]
                domain = 'CORNER'
            if originals:
                # Store stable indices, never BMesh elements or layer handles. Blender may
                # rebuild edit-mode BMesh wrappers between modal mouse events.
                self._targets.append((obj.data, domain, data_type, name, originals))
        return bool(self._targets)

    @staticmethod
    def _resolve_target(mesh, domain, data_type, name):
        bm = bmesh.from_edit_mesh(mesh)
        if domain == 'POINT':
            bm.verts.ensure_lookup_table()
            layers = bm.verts.layers.float_color if data_type == 'FLOAT_COLOR' else bm.verts.layers.color
        else:
            bm.faces.ensure_lookup_table()
            layers = bm.loops.layers.float_color if data_type == 'FLOAT_COLOR' else bm.loops.layers.color
        return bm, layers.get(name)

    def _place_picker(self, context, event):
        total_width = PICKER_SIZE + PICKER_GAP + HUE_WIDTH
        max_x = max(PICKER_PADDING, context.region.width - total_width - PICKER_PADDING)
        max_y = max(PICKER_PADDING, context.region.height - PICKER_SIZE - PICKER_PADDING)
        self._x = min(max(PICKER_PADDING, event.mouse_region_x - total_width * .5), max_x)
        self._y = min(max(PICKER_PADDING, event.mouse_region_y - PICKER_SIZE * .5), max_y)
        self._hue_x = self._x + PICKER_SIZE + PICKER_GAP

    def _build_static_batches(self):
        self._color_shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        positions, colors, indices = [], [], []
        for index in range(6):
            y0 = self._y + PICKER_SIZE * index / 6
            y1 = self._y + PICKER_SIZE * (index + 1) / 6
            c0 = (*colorsys.hsv_to_rgb(index / 6, 1, 1), 1)
            c1 = (*colorsys.hsv_to_rgb((index + 1) / 6, 1, 1), 1)
            base = len(positions)
            positions.extend(((self._hue_x, y0), (self._hue_x + HUE_WIDTH, y0),
                              (self._hue_x, y1), (self._hue_x + HUE_WIDTH, y1)))
            colors.extend((c0, c0, c1, c1))
            indices.extend(((base, base + 1, base + 2), (base + 1, base + 3, base + 2)))
        self._hue_batch = batch_for_shader(
            self._color_shader, 'TRIS', {"pos": positions, "color": colors}, indices=indices)
        self._marker_shader = gpu.shader.from_builtin('UNIFORM_COLOR')

    def _build_dynamic_batches(self):
        hue_color = colorsys.hsv_to_rgb(self.hue, 1, 1)
        positions = ((self._x, self._y), (self._x + PICKER_SIZE, self._y),
                     (self._x, self._y + PICKER_SIZE),
                     (self._x + PICKER_SIZE, self._y + PICKER_SIZE))
        colors = ((0, 0, 0, 1), (0, 0, 0, 1), (1, 1, 1, 1), (*hue_color, 1))
        self._sv_batch = batch_for_shader(
            self._color_shader, 'TRIS', {"pos": positions, "color": colors},
            indices=((0, 1, 2), (1, 3, 2)))
        sx = self._x + self.saturation * PICKER_SIZE
        sy = self._y + self.value * PICKER_SIZE
        hy = self._y + self.hue * PICKER_SIZE
        markers = ((sx - 7, sy), (sx + 7, sy), (sx, sy - 7), (sx, sy + 7),
                   (self._hue_x - 3, hy), (self._hue_x + HUE_WIDTH + 3, hy))
        self._marker_batch = batch_for_shader(self._marker_shader, 'LINES', {"pos": markers})

    def _draw_picker(self):
        try:
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            self._color_shader.bind()
            self._sv_batch.draw(self._color_shader)
            self._hue_batch.draw(self._color_shader)
            self._marker_shader.bind()
            self._marker_shader.uniform_float("color", (0, 0, 0, 1))
            gpu.state.line_width_set(3)
            self._marker_batch.draw(self._marker_shader)
            self._marker_shader.uniform_float("color", (1, 1, 1, 1))
            gpu.state.line_width_set(1)
            self._marker_batch.draw(self._marker_shader)
        finally:
            gpu.state.line_width_set(1)
            gpu.state.blend_set('NONE')

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self._restore_originals()
            self._finish(context)
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            control = self._control_at(event.mouse_region_x, event.mouse_region_y)
            if control:
                self._active_control = control
                self._update_from_mouse(event.mouse_region_x, event.mouse_region_y, control)
                self._apply_color()
                self._build_dynamic_batches()
                self._area.tag_redraw()
                return {'RUNNING_MODAL'}
            self._finish(context)
            return {'FINISHED'}

        if event.type == 'MOUSEMOVE' and self._active_control:
            self._update_from_mouse(
                event.mouse_region_x, event.mouse_region_y, self._active_control)
            self._apply_color()
            self._build_dynamic_batches()
            self._area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self._active_control = None
            return {'RUNNING_MODAL'}

        return {'RUNNING_MODAL'}

    def _control_at(self, x, y):
        inside_y = self._y <= y <= self._y + PICKER_SIZE
        if self._x <= x <= self._x + PICKER_SIZE and inside_y:
            return 'SV'
        if self._hue_x <= x <= self._hue_x + HUE_WIDTH and inside_y:
            return 'HUE'
        return None

    def _update_from_mouse(self, x, y, control):
        if control == 'SV':
            self.saturation = min(1, max(0, (x - self._x) / PICKER_SIZE))
            self.value = min(1, max(0, (y - self._y) / PICKER_SIZE))
        else:
            self.hue = min(1, max(0, (y - self._y) / PICKER_SIZE))

    def _apply_color(self):
        color = (*colorsys.hsv_to_rgb(self.hue, self.saturation, self.value), 1)
        if color == self._last_color:
            return
        for mesh, domain, data_type, name, originals in self._targets:
            bm, layer = self._resolve_target(mesh, domain, data_type, name)
            if not layer:
                continue
            for key, _original in originals:
                element = bm.verts[key] if domain == 'POINT' else bm.faces[key[0]].loops[key[1]]
                element[layer] = color
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        self._last_color = color

    def _restore_originals(self):
        for mesh, domain, data_type, name, originals in self._targets:
            bm, layer = self._resolve_target(mesh, domain, data_type, name)
            if not layer:
                continue
            for key, original in originals:
                element = bm.verts[key] if domain == 'POINT' else bm.faces[key[0]].loops[key[1]]
                element[layer] = original
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)

    def _finish(self, context):
        if self._finished:
            return
        self._finished = True
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        space = self._area.spaces.active
        space.shading.type, space.shading.light, space.shading.color_type = self._shading
        space.overlay.show_overlays = self._show_overlays
        context.window.cursor_modal_restore()
        self._area.tag_redraw()


def register():
    bpy.utils.register_class(VertexColorHSVPaintOperator)


def unregister():
    bpy.utils.unregister_class(VertexColorHSVPaintOperator)
