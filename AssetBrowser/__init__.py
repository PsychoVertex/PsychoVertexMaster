import bpy
from bpy.types import Operator, Context, Collection

def GetOrCreateCollection(name, parent=None) -> Collection:
    col = bpy.data.collections.get(name)
    if col:
        return col
    col = bpy.data.collections.new(name)
    if parent:
        parent.children.link(col)
    else:
        bpy.context.scene.collection.children.link(col)
    return col

class MakeCollectionAssetsOperation(Operator):
    bl_idname = "assetbrowser.make_collection_asset_from_selection"
    bl_label = "Make Collection Assets"
    bl_options = {"REGISTER", "UNDO_GROUPED"}

    def execute(self, context: Context):
        old_collections = []
        old_collection: Collection
        for obj in context.selected_objects:
            collection = GetOrCreateCollection(obj.name)
            old_collection = obj.users_collection[0]
            old_collection.objects.unlink(obj)
            collection.objects.link(obj)
            collection.asset_mark()
            collection.asset_generate_preview()
            if old_collection not in old_collections:
                old_collections.append(old_collection)

        for old_collection in old_collections:
            if len(old_collection.objects) == 0:
                bpy.data.collections.remove(old_collection)
        return {"FINISHED"}

def register():
    bpy.utils.register_class(MakeCollectionAssetsOperation)

def unregister():
    bpy.utils.unregister_class(MakeCollectionAssetsOperation)