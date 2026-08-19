import bpy
import bmesh


class RemoveCheckerOperator(bpy.types.Operator):
    bl_idname = "mesh.remove_checker"
    bl_label = "Remove Checker"
    bl_options = {"UNDO", 'UNDO_GROUPED'}

    skip: bpy.props.IntProperty(name="Skip", default=1)
    nth: bpy.props.IntProperty(name="Nth", default=1)
    offset: bpy.props.IntProperty(name="Offset", default=0)

    def get_selection_groups_by_mesh_island(self, bm):
        visited = set()
        groups = []

        for edge in bm.edges:
            if edge in visited:
                continue

            # Find complete mesh island
            island_edges = set()
            stack = [edge]

            while stack:
                e = stack.pop()

                if e in visited:
                    continue

                visited.add(e)
                island_edges.add(e)

                for v in e.verts:
                    stack.extend(
                        linked
                        for linked in v.link_edges
                        if linked not in visited
                    )

            # Collect only selected edges from this island
            selected_in_island = [e for e in island_edges if e.select]

            if selected_in_island:
                groups.append(selected_in_island)

        return groups

    def execute(self, context):
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}

        bm = bmesh.from_edit_mesh(obj.data)
        islands = self.get_selection_groups_by_mesh_island(bm)
        bpy.ops.mesh.select_all(action='DESELECT')

        for island in islands:
            for e in bm.edges:
                e.select = False
            for e in island:
                e.select = True
            bmesh.update_edit_mesh(obj.data)

            bpy.ops.mesh.select_nth(
                skip=self.skip,
                nth=self.nth,
                offset=self.offset
            )

            bpy.ops.mesh.loop_multi_select(ring=False)
            bpy.ops.mesh.dissolve_edges()

            bm = bmesh.from_edit_mesh(obj.data)
        return {'FINISHED'}


def register():
    bpy.utils.register_class(RemoveCheckerOperator)


def unregister():
    bpy.utils.unregister_class(RemoveCheckerOperator)
