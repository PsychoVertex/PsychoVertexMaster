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
from . import Updater

bl_info = {
    "name": "PsychoVertexMaster",
    "category": "3D View",
    "author": "Mohammad Zamanian",
    "location": "3D View > 'W' and 'D' keymaps",
    "version": (1, 3, 0),
    "blender": (3, 0, 0),
}

keymaps = []

REGISTER_MODULES = (
    Preferences, Updater, Pipeline, HandyUtils, UvTools, VertexColors, Lightmapping,
    AssetBrowser, Collisions, HandyMenu, ChildControl, ModifiersMenu,
    AddMaterialToSelectedFaces, CreateEmptyParent,
)


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
    registered_modules = []
    operator_registered = False
    try:
        for module in REGISTER_MODULES:
            module.register()
            registered_modules.append(module)
        bpy.utils.register_class(PVM_OT_OpenMenu)
        operator_registered = True

        wm = bpy.context.window_manager
        kc = wm.keyconfigs.addon
        if kc:
            km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
            kmi = km.keymap_items.new('pv.open_menu', 'D', 'PRESS', ctrl=False, shift=False, alt=False)
            keymaps.append((km, kmi))
            kmi = km.keymap_items.new('wm.call_menu', 'W', 'PRESS', ctrl=False, shift=False, alt=False)
            kmi.properties.name = ModifiersMenu.MZageModifiersMenu.bl_idname
            keymaps.append((km, kmi))
    except Exception:
        for km, kmi in reversed(keymaps):
            try:
                km.keymap_items.remove(kmi)
            except (ReferenceError, RuntimeError):
                pass
        keymaps.clear()
        if operator_registered:
            bpy.utils.unregister_class(PVM_OT_OpenMenu)
        for module in reversed(registered_modules):
            try:
                module.unregister()
            except Exception:
                pass
        raise

def unregister():
    bpy.utils.unregister_class(PVM_OT_OpenMenu)
    for km, kmi in keymaps:
        km.keymap_items.remove(kmi)
    keymaps.clear()
    for module in reversed(REGISTER_MODULES):
        module.unregister()


if __name__ == "__main__":
    register()
