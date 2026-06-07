import bpy


class BTUS_Setup_Operator(bpy.types.Operator):
    bl_idname = "object.btus_setup"
    bl_label = "Blender to Unreal Setup"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    lightmapSurfaceScale: bpy.props.IntProperty(
        name="Surface Scale",
        description="StaticMesh LightMap Surface Scale",
        default=64,
        min=1
    )
    lightmapRoundPowerOfTwo: bpy.props.BoolProperty(
        name="Round to POT",
        description="StaticMesh LightMap Round Power Of Two",
        default=False
    )

    collisionTraceFlag: bpy.props.EnumProperty(
        name="Collision Complexity",
        description="Collision Trace Flag",
        items=[
            ("CTF_UseDefault",
                "Project Default",
                "Create only complex shapes (per poly)." +
                " Use complex shapes for all scene queries" +
                " and collision tests." +
                " Can be used in simulation for" +
                " static shapes only" +
                " (i.e can be collided against but not moved" +
                " through forces or velocity.",
                1),
            ("CTF_UseSimpleAndComplex",
                "Use Simple And Complex",
                "Use project physics settings (DefaultShapeComplexity)",
                2),
            ("CTF_UseSimpleAsComplex",
                "Use Simple as Complex",
                "Create both simple and complex shapes." +
                " Simple shapes are used for regular scene queries" +
                " and collision tests. Complex shape (per poly)" +
                " is used for complex scene queries.",
                3),
            ("CTF_UseComplexAsSimple",
                "Use Complex as Simple",
                "Create only simple shapes." +
                " Use simple shapes for all scene" +
                " queries and collision tests.",
                4)
        ],
        default="CTF_UseDefault"
    )

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) > 0

    def execute(self, context):
        sos = context.selected_objects
        for so in sos:
            if so.bfu_export_type == "dont_export":
                continue

            collection_path = []
            current_collection = so.users_collection[0] if so.users_collection else None
            while current_collection:
                collection_path.insert(0, current_collection.name)
                parent_collection_candids = [col for col in bpy.data.collections if current_collection.name in col.children]
                current_collection = parent_collection_candids[0] if len(parent_collection_candids) > 0 else None

            so.bfu_export_type = "export_recursive"
            so.bfu_export_folder_name = "/".join(collection_path)

            so.bfu_rotate_to_zero_for_export = True

            so.bfu_build_nanite_mode = "build_nanite_false"

            so.bfu_collision_trace_flag = self.collisionTraceFlag
            so.bfu_auto_generate_collision = True
            so.bfu_material_search_location = "AllAssets"

            if so.bfu_static_mesh_light_map_mode != "CustomMap":
                so.bfu_generate_light_map_uvs = False
                so.bfu_static_mesh_light_map_mode = "SurfaceArea"
                so.bfu_use_static_mesh_light_map_world_scale = True
                so.bfu_static_mesh_light_map_round_power_of_two = self.lightmapRoundPowerOfTwo
                so.bfu_static_mesh_light_map_surface_scale = self.lightmapSurfaceScale

            # so.bfu_override_procedure_preset = True
            # so.bfu_export_axis_forward = "-Y"
            # so.bfu_export_axis_up = "Z"

        # self.report({'INFO'}, "Setup objects")
        return {'FINISHED'}


class BTUS_Export_Operator(bpy.types.Operator):
    bl_idname = "object.btus_export"
    bl_label = "Blender to Unreal Export"

    def execute(self, context):
        ctx = bpy.context
        sos = ctx.selected_objects
        for so in sos:
            so.bfu_export_type = "export_recursive"
        self.report({'INFO'}, "Export flag set")
        return {'FINISHED'}


class BTUS_DontExport_Operator(bpy.types.Operator):
    bl_idname = "object.btus_dontexport"
    bl_label = "Blender to Unreal Dont Export"

    def execute(self, context):
        ctx = bpy.context
        sos = ctx.selected_objects
        for so in sos:
            so.bfu_export_type = "dont_export"
        self.report({'INFO'}, "Export flag unset")
        return {'FINISHED'}


class BTUS_UpdatePath_Operator(bpy.types.Operator):
    bl_idname = "object.btus_updatepath"
    bl_label = "Blender to Unreal Export"

    def execute(self, context):
        ctx = bpy.context
        sos = ctx.selected_objects
        for so in sos:
            collection_path = []
            current_collection = so.users_collection[0] if so.users_collection else None
            while current_collection:
                collection_path.insert(0, current_collection.name)
                parent_collection_candids = [col for col in bpy.data.collections if current_collection.name in col.children]
                current_collection = parent_collection_candids[0] if len(parent_collection_candids) > 0 else None
            so.bfu_export_folder_name = "/".join(collection_path)
        self.report({'INFO'}, "Updated path")
        return {'FINISHED'}


classes = [
    BTUS_DontExport_Operator,
    BTUS_Export_Operator,
    BTUS_Setup_Operator,
    BTUS_UpdatePath_Operator,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
