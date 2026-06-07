from typing import cast, Literal
import bpy
from bpy.types import AddonPreferences
from bpy.props import EnumProperty


ModeTypes = Literal['PIE_MENUS', 'NORMAL_MENUS']


class PV_Preferences(AddonPreferences):
    bl_idname = str(__package__)

    mode: EnumProperty(
        name="Mode",
        items=[
            ('PIE_MENUS', "Pie Menus", ""),
            ('NORMAL_MENUS', "Normal Menus", ""),
        ],
        default='PIE_MENUS',
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "mode")


def get():
    addon_name = str(__package__)
    prefs = bpy.context.preferences.addons.get(addon_name)
    if prefs is None:
        return None
    return cast(PV_Preferences, prefs.preferences)


def get_mode() -> 'ModeTypes | None':
    prefs = get()
    return prefs.mode if prefs else None


def register():
    bpy.utils.register_class(PV_Preferences)


def unregister():
    bpy.utils.unregister_class(PV_Preferences)
