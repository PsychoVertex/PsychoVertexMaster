import bpy
from mathutils.kdtree import KDTree


def build_kdtree_from_verts(verts):
    kd = KDTree(len(verts))
    for i, vtx in enumerate(verts):
        if not vtx.hide:
            kd.insert(vtx.co, i)
    kd.balance()
    return kd


def getDuplicateVertices(verts, threshold, selectAll):
    kd = build_kdtree_from_verts(verts)

    vtx_selection = set()
    vtx_seen = set()

    for vtx in verts:
        vtx_group = kd.find_range(vtx.co, threshold)
        if len(vtx_group) > 1:
            if not selectAll and vtx.index not in vtx_seen:
                vtx_selection.add(vtx.index)
            for (co, index, dist) in vtx_group:
                if selectAll:
                    vtx_selection.add(index)
                else:
                    vtx_seen.add(index)

    return list(vtx_selection)


def main(ctx, threshold, selectAll):
    obj = ctx.object

    bpy.ops.object.mode_set(mode='EDIT')
    bpy.context.tool_settings.mesh_select_mode[0] = True
    bpy.context.tool_settings.mesh_select_mode[1] = False
    bpy.context.tool_settings.mesh_select_mode[2] = False
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')

    verts = obj.data.vertices
    dupvs = getDuplicateVertices(verts, threshold, selectAll)

    bpy.ops.object.mode_set(mode='OBJECT')
    for d in dupvs:
        verts[d].select = True
    bpy.ops.object.mode_set(mode='EDIT')


class SelectOverlappingVerticesOperator(bpy.types.Operator):
    """Tooltip"""
    bl_idname = "mesh.select_overlapping_vertices"
    bl_label = "Overlapping Vertices"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    threshold: bpy.props.FloatProperty(
        name="Distance Factor",
        description="The distance between points to be considered overlapping",
        min=0.0000001, max=100.0,
        default=0.0001,
        subtype="DISTANCE",
        unit="LENGTH"
    )
    selectAll: bpy.props.BoolProperty(
        name="Select All Overlapping",
        default=True
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        main(context, self.threshold, self.selectAll)
        return {'FINISHED'}


def add_menu_item(self, context):
    layout = self.layout
    layout.separator()
    layout.operator("mesh.select_overlapping_vertices")


def register():
    bpy.utils.register_class(SelectOverlappingVerticesOperator)
    bpy.types.VIEW3D_MT_edit_mesh_select_similar.append(add_menu_item)


def unregister():
    bpy.utils.unregister_class(SelectOverlappingVerticesOperator)
    bpy.types.VIEW3D_MT_edit_mesh_select_similar.remove(add_menu_item)
