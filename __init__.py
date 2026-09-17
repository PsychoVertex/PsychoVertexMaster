import os
import bpy
from . import Pipeline
from . import HandyUtils
from . import UvTools
from . import VertexColors
from . import Lightmapping
from . import AssetBrowser
from . import Collisions
from . import HandyMenu
from . import ChildControl
from . import ModifiersMenu
from . import AddMaterialToSelectedFaces
from . import CreateEmptyParent
from . import Preferences

bl_info = {
    "name": "PsychoVertexMaster",
    "category": "3D View",
    "author": "Mohammad Zamanian",
    "location": "3D View > 'W' and 'D' keymaps",
    "version": (1, 2, 2),
    "blender": (3, 0, 0),
}

keymaps = []


class PVM_OT_OpenMenu(bpy.types.Operator):
    bl_idname = "pv.open_menu"
    bl_label = "Open Handy Menu"

    def execute(self, context):
        if Preferences.get_mode() == "PIE_MENUS":
            bpy.ops.wm.call_menu_pie(name=HandyMenu.MZageHandyMenu.bl_idname)
        else:
            bpy.ops.wm.call_menu(name=HandyMenu.MZageHandyMenu.bl_idname)
        return {'FINISHED'}


def register():
    Pipeline.register()
    HandyUtils.register()
    UvTools.register()
    VertexColors.register()
    Lightmapping.register()
    AssetBrowser.register()
    Collisions.register()
    HandyMenu.register()
    ChildControl.register()
    ModifiersMenu.register()
    AddMaterialToSelectedFaces.register()
    CreateEmptyParent.register()
    Preferences.register()
    bpy.utils.register_class(PVM_OT_OpenMenu)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')

        kmi = km.keymap_items.new('pv.open_menu', 'D', 'PRESS', ctrl=False, shift=False, alt=False)
        keymaps.append((km, kmi))

        kmi = km.keymap_items.new('wm.call_menu', 'W', 'PRESS', ctrl=False, shift=False, alt=False)
        kmi.properties.name = ModifiersMenu.MZageModifiersMenu.bl_idname
        keymaps.append((km, kmi))

    os.system('cls')

def unregister():
    Pipeline.unregister()
    HandyUtils.unregister()
    UvTools.unregister()
    VertexColors.unregister()
    Lightmapping.unregister()
    AssetBrowser.unregister()
    Collisions.unregister()
    HandyMenu.unregister()
    ChildControl.unregister()
    ModifiersMenu.unregister()
    AddMaterialToSelectedFaces.unregister()
    CreateEmptyParent.unregister()
    Preferences.unregister()
    bpy.utils.unregister_class(PVM_OT_OpenMenu)

    for km, kmi in keymaps:
        km.keymap_items.remove(kmi)
    keymaps.clear()


if __name__ == "__main__":
    register()
