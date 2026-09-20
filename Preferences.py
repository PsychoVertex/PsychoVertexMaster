from typing import cast, Literal
import json
import bpy
from bpy.types import AddonPreferences
from bpy.props import EnumProperty, StringProperty


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

    oidn_path: StringProperty(
        name="Open Image Denoise",
        description="Path to oidnDenoise.exe used by the Lightmap OIDN backend",
        subtype='FILE_PATH',
        default="",
    )

    optix_path: StringProperty(
        name="OptiX Denoiser",
        description="Path to Denoiser.exe used by the OptiX backend",
        subtype='FILE_PATH',
        default="",
    )

    dialog_settings: StringProperty(default="{}", options={'HIDDEN'})

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "mode")
        denoise = layout.box()
        denoise.label(text="Lightmap Denoising", icon='IMAGE_DATA')
        denoise.prop(self, "oidn_path")
        denoise.label(text="Expected command: oidnDenoise.exe -f RTLightmap -hdr ...", icon='INFO')
        denoise.prop(self, "optix_path")
        denoise.label(text="Expected command: Denoiser.exe -i ... -o ...", icon='INFO')


def get():
    addon_name = str(__package__)
    prefs = bpy.context.preferences.addons.get(addon_name)
    if prefs is None:
        return None
    return cast(PV_Preferences, prefs.preferences)


def get_mode() -> 'ModeTypes | None':
    prefs = get()
    return prefs.mode if prefs else None


def load_dialog_settings(operator, property_names):
    prefs = get()
    if prefs is None:
        return
    try:
        settings = json.loads(prefs.dialog_settings).get(operator.bl_idname, {})
    except (TypeError, ValueError):
        return
    for name in property_names:
        if name in settings:
            try:
                setattr(operator, name, settings[name])
            except (AttributeError, TypeError, ValueError):
                pass


def save_dialog_settings(operator, property_names):
    prefs = get()
    if prefs is None:
        return
    try:
        settings = json.loads(prefs.dialog_settings)
    except (TypeError, ValueError):
        settings = {}
    settings[operator.bl_idname] = {
        name: getattr(operator, name) for name in property_names
    }
    prefs.dialog_settings = json.dumps(settings, separators=(",", ":"))


def register():
    bpy.utils.register_class(PV_Preferences)


def unregister():
    bpy.utils.unregister_class(PV_Preferences)
