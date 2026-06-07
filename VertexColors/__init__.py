import bpy
from . import SelectSameColor, VertexColorHSVPaint, CopyColor


def register():
    SelectSameColor.register()
    VertexColorHSVPaint.register()
    CopyColor.register()

def unregister():
    SelectSameColor.unregister()
    VertexColorHSVPaint.unregister()
    CopyColor.unregister()