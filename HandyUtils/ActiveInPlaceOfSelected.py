import bpy
from mathutils import Matrix
from .. import Preferences


DATA_COPY_ITEMS = (
    ('LINKED', "Linked Data", "Reuse mesh, curve, light, and other object data"),
    ('INDEPENDENT', "Independent Data", "Copy object data; materials remain shared"),
)

COLLECTION_ITEMS = (
    ('TARGET', "Target Collections", "Link each copy to its target's collections"),
    ('ACTIVE', "Active Collections", "Link each copy to the active source's collections"),
)

ACTIVE_IN_PLACE_DIALOG_SETTINGS = ("data_copy", "destination_collections")


def _descendants(root):
    result = []

    def visit(obj):
        result.append(obj)
        for child in obj.children:
            visit(child)

    visit(root)
    return result


def _selected_target_roots(context, source):
    source_hierarchy = set(_descendants(source))
    selected = set(context.selected_objects)
    candidates = selected - source_hierarchy
    roots = []

    for obj in context.selected_objects:
        if obj not in candidates:
            continue

        # An ancestor of the active object cannot safely be a target because
        # replacing it would also remove the source hierarchy.
        if source in _descendants(obj):
            continue

        ancestor = obj.parent
        while ancestor is not None and ancestor not in candidates:
            ancestor = ancestor.parent
        if ancestor is None:
            roots.append(obj)

    return roots


def _copy_data(obj):
    data = obj.data
    if data is not None and hasattr(data, "copy"):
        obj.data = data.copy()


def _duplicate_source(source, target_matrix, collections, data_copy):
    source_objects = [source] if source.instance_collection is not None else _descendants(source)
    source_root_inverse = source.matrix_world.inverted_safe()
    relative_matrices = {
        obj: source_root_inverse @ obj.matrix_world.copy()
        for obj in source_objects
    }
    copies = {}

    try:
        for source_obj in source_objects:
            copied_obj = source_obj.copy()
            copies[source_obj] = copied_obj
            if data_copy == 'INDEPENDENT':
                _copy_data(copied_obj)
            for collection in collections:
                collection.objects.link(copied_obj)
    except Exception:
        for copied_obj in reversed(list(copies.values())):
            copied_data = copied_obj.data
            bpy.data.objects.remove(copied_obj, do_unlink=True)
            if data_copy == 'INDEPENDENT' and copied_data is not None and copied_data.users == 0:
                bpy.data.batch_remove((copied_data,))
        raise

    for source_obj, copied_obj in copies.items():
        copied_obj.parent = copies.get(source_obj.parent)
        if copied_obj.parent is None:
            copied_obj.matrix_parent_inverse = Matrix.Identity(4)

    for source_obj in source_objects:
        copies[source_obj].matrix_world = target_matrix @ relative_matrices[source_obj]

    return copies[source], list(copies.values())


def _remove_hierarchy(root):
    for obj in reversed(_descendants(root)):
        bpy.data.objects.remove(obj, do_unlink=True)


class ActiveInPlaceBase:
    bl_options = {'REGISTER', 'UNDO'}
    replace_targets = False

    data_copy: bpy.props.EnumProperty(
        name="Data Copy",
        items=DATA_COPY_ITEMS,
        default='LINKED',
    )
    destination_collections: bpy.props.EnumProperty(
        name="Link Hierarchy To",
        items=COLLECTION_ITEMS,
        default='TARGET',
    )

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'OBJECT'
            and context.active_object is not None
            and len(context.selected_objects) > 1
        )

    def invoke(self, context, event):
        Preferences.load_dialog_settings(self, ACTIVE_IN_PLACE_DIALOG_SETTINGS)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "data_copy")
        layout.prop(self, "destination_collections")

    def execute(self, context):
        Preferences.save_dialog_settings(self, ACTIVE_IN_PLACE_DIALOG_SETTINGS)
        source = context.active_object
        targets = _selected_target_roots(context, source)
        if not targets:
            self.report({'ERROR'}, "Select at least one target hierarchy outside the active hierarchy")
            return {'CANCELLED'}

        active_collections = list(source.users_collection)
        if self.destination_collections == 'ACTIVE' and not active_collections:
            self.report({'ERROR'}, "The active source is not linked to a collection")
            return {'CANCELLED'}

        destinations = {}
        for target in targets:
            collections = (
                active_collections
                if self.destination_collections == 'ACTIVE'
                else list(target.users_collection)
            )
            if not collections:
                self.report({'ERROR'}, f"Target '{target.name}' is not linked to a collection")
                return {'CANCELLED'}
            destinations[target] = collections

        created_objects = []
        created_roots = []
        try:
            for target in targets:
                copied_root, copied_objects = _duplicate_source(
                    source,
                    target.matrix_world.copy(),
                    destinations[target],
                    self.data_copy,
                )
                created_roots.append(copied_root)
                created_objects.extend(copied_objects)
        except Exception as exc:
            for obj in reversed(created_objects):
                if obj.name in bpy.data.objects:
                    bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'ERROR'}, f"Could not duplicate active hierarchy: {exc}")
            return {'CANCELLED'}

        if self.replace_targets:
            for target in targets:
                _remove_hierarchy(target)

        for obj in context.selected_objects:
            obj.select_set(False)
        for copied_root in created_roots:
            copied_root.select_set(True)
        context.view_layer.objects.active = created_roots[-1]

        action = "Replaced" if self.replace_targets else "Added"
        self.report({'INFO'}, f"{action} active asset at {len(created_roots)} target(s)")
        return {'FINISHED'}


class AddActiveInPlaceOfSelectedOperator(ActiveInPlaceBase, bpy.types.Operator):
    bl_idname = "object.add_active_in_place_of_selected"
    bl_label = "Add Active in Place of Selected"
    bl_description = "Copy the active asset onto each selected target"


class ReplaceSelectedWithActiveOperator(ActiveInPlaceBase, bpy.types.Operator):
    bl_idname = "object.replace_selected_with_active"
    bl_label = "Replace Selected with Active"
    bl_description = "Replace each selected target hierarchy with the active asset"
    replace_targets = True


classes = (
    AddActiveInPlaceOfSelectedOperator,
    ReplaceSelectedWithActiveOperator,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
