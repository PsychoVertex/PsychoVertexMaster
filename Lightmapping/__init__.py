from typing import cast
import bpy
import bmesh
from mathutils import Vector
from bpy.types import Context, Operator, Object, Collection, LayerCollection, ShaderNodeTexImage, CompositorNodeImage, Material, Image, ShaderNodeVertexColor, ShaderNodeBsdfRayPortal, ShaderNodeBsdfTransparent, ShaderNodeAddShader
from bpy.props import BoolProperty, FloatProperty, StringProperty, IntProperty, EnumProperty
from bpy.app.handlers import persistent
from ..Pipeline import PipelineOperator, PipelineTask

EPS = 1e-6


def centroid(objs):
    return sum((o.matrix_world.translation for o in objs), Vector()) / len(objs)


def GetTempActiveObj(collection: Collection):
    obj = bpy.data.objects.new("TempObject", bpy.data.meshes.new("TempMesh"))
    collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


def ReleaseTempActiveObjByName():
    obj = bpy.data.objects.get("TempObject")
    if obj:
        ReleaseTempActiveObj(obj)


def ReleaseTempActiveObj(obj):
    bpy.data.objects.remove(obj, do_unlink=True)


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


def DeleteCollection(name):
    col = bpy.data.collections.get(name)
    if col:
        for obj in col.objects:
            for scene in bpy.data.scenes:
                if obj.name in scene.collection.objects:
                    scene.collection.objects.unlink(obj)
            bpy.data.objects.remove(obj)
        bpy.data.collections.remove(col)


def GetLayerCollection(collection: str, current_layer_collection: LayerCollection):
    if current_layer_collection.collection.name == collection:
        return current_layer_collection
    for child in current_layer_collection.children:
        found = GetLayerCollection(collection, child)
        if found:
            return found
    return None


def UVPack_Normal(context: Context, normalize_scale: bool, heuristic: bool, pixel_margin: int, texture_size: int):
    context.scene.uvpm3_props.normalize_scale = normalize_scale
    context.scene.uvpm3_props.heuristic_enable = heuristic
    context.scene.uvpm3_props.heuristic_search_time = 1
    context.scene.uvpm3_props.advanced_heuristic = True
    context.scene.uvpm3_props.pixel_margin_enable = True
    context.scene.uvpm3_props.pixel_margin = pixel_margin
    context.scene.uvpm3_props.pixel_border_margin = pixel_margin
    context.scene.uvpm3_props.pixel_margin_tex_size = texture_size
    context.scene.tool_settings.use_uv_select_sync = True
    bpy.ops.mesh.reveal()
    bpy.ops.uv.select_all(action='SELECT')
    bpy.ops.uvpackmaster3.select_mode(mode_id="pack.single_tile")
    bpy.ops.uvpackmaster3.pack(mode_id="pack.single_tile", pack_op_type='1')


def UVPack_Scaled(context: Context, operator: Operator, objs: list[Object], heuristic: bool, pixel_margin: int, texture_size: int):
    for obj in objs:
        ls_check = obj.data.uv_layers.get("LightMap")
        if ls_check is None:
            operator.report({"ERROR"}, "Mesh should have a UVMap Attribute called `LightMap`")
            return {'CANCELLED'}
        obj.data.uv_layers.active = ls_check
    UVPack_Normal(
        context,
        normalize_scale=True,
        heuristic=False,
        pixel_margin=pixel_margin,
        texture_size=texture_size
    )

    # scale uvs and move to top right corner, adjust margin, and pin it
    for obj in objs:
        mat_slots = obj.material_slots
        bm = bmesh.from_edit_mesh(obj.data)
        ls_layer = bm.faces.layers.float.get("lightmap_scale")
        uv_layer = bm.loops.layers.uv.get("LightMap")
        for face in bm.faces:
            for loop in face.loops:
                mat = mat_slots[face.material_index].material
                lightmap_scale = face[ls_layer] if mat and mat.light_baked else 0
                uv = loop[uv_layer].uv * lightmap_scale
                if abs(uv.x) < EPS and abs(uv.y) < EPS:
                    loop[uv_layer].pin_uv = True
                    offset = 1 - (3 / 2048)
                    uv += Vector((offset, offset))
                loop[uv_layer].uv = uv
        bmesh.update_edit_mesh(obj.data)

    UVPack_Normal(
        context,
        normalize_scale=False,
        heuristic=heuristic,
        pixel_margin=pixel_margin,
        texture_size=texture_size
    )


class ClearLightmappingStuff(Operator):
    bl_idname = "lightmap.clear_lightmapping_stuff"
    bl_label = "Clear Lightmapping Stuff"
    bl_options = {'REGISTER', "UNDO", "UNDO_GROUPED"}

    def execute(self, context: Context):
        if bpy.data.collections.get('SOURCE'):
            GetLayerCollection("SOURCE", context.view_layer.layer_collection).exclude = False
        DeleteCollection("EXPORT_STUFF")
        DeleteCollection("TEMP_EXPORT_STUFF")
        ReleaseTempActiveObjByName()

        if context.scene.node_tree:
            compositing_img_node = cast(CompositorNodeImage, context.scene.node_tree.nodes.get('Noisy Lightmap Slot'))
            if compositing_img_node:
                img = compositing_img_node.image
                compositing_img_node.image = None
                if img and img.name == "NoisyLightmap":
                    bpy.data.images.remove(img)

        # Purge Data
        bpy.ops.outliner.orphans_purge(do_linked_ids=True, do_local_ids=True, do_recursive=True)
        return {'FINISHED'}


class BakeBatch(PipelineOperator):
    bl_idname = "lightmap.bake_batch"
    bl_label = "Bake Batch"
    bl_options = {'REGISTER', "UNDO", "UNDO_GROUPED"}

    render_resolution: IntProperty(name="Render Resolution", default=4096)
    final_resolution: IntProperty(name="Final Resolution", default=1024)
    samples: IntProperty(name="Samples", default=64)
    margin: IntProperty(name="Margin", default=8)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def get_tasks(self):
        return [
            PipelineTask(self.make_export_collection),
            PipelineTask(self.get_batch),
            PipelineTask(self.set_render_settings),
            PipelineTask(self.set_quality_settings),
            PipelineTask(self.create_lightmap_image),
            PipelineTask(self.prepare_materials_for_baking),
            PipelineTask(self.set_normal_mode),
            PipelineTask(self.start_bake, poll=self.is_baking, timelog=True),
            PipelineTask(self.prepare_to_denoise_lightmap),
            PipelineTask(self.denoise, timelog=True),
            PipelineTask(self.finalize_lightmap_denoise),
            PipelineTask(self.restore_materials),
            PipelineTask(self.set_lighting_mode),
        ]

    _baking: bool

    def start_bake(self, context: Context):
        self._baking = True
        bpy.app.handlers.object_bake_complete.append(self.finished_bake)
        bpy.app.handlers.object_bake_cancel.append(self.finished_bake)
        self.report({'INFO'}, f"bake type: {context.scene.cycles.bake_type}")
        bpy.ops.object.bake('INVOKE_DEFAULT', type=context.scene.cycles.bake_type)

    def is_baking(self):
        return self._baking

    def finished_bake(self, obj, _):
        self._baking = False
        bpy.app.handlers.object_bake_complete.remove(self.finished_bake)
        bpy.app.handlers.object_bake_cancel.remove(self.finished_bake)

    def denoise(self):
        bpy.ops.render.render(write_still=True)

    def set_normal_mode(self, context: Context):
        context.scene.display_lighting = False

    def set_lighting_mode(self, context: Context):
        context.scene.display_lighting = True

    export_collection: Collection

    def make_export_collection(self):
        self.export_collection = bpy.data.collections.get('EXPORT_STUFF')
        if not self.export_collection:
            return {'CANCELLED'}

    batch: Object
    bake_objects: list[Object]

    def get_batch(self, context: Context):
        if context.active_object.users_collection[0] != self.export_collection:
            self.report({'INFO'}, "No Batch is Active")
            return {'CANCELLED'}
        self.batch = context.active_object
        batch_noshadows = self.export_collection.objects.get(f"{self.batch.name}_NoShadows")
        self.bake_objects = [self.batch]
        if batch_noshadows is not None:
            self.bake_objects.append(batch_noshadows)

        bpy.ops.object.select_all(action='DESELECT')
        for obj in self.bake_objects:
            obj.select_set(True)
        context.view_layer.objects.active = self.batch

    def set_render_settings(self, context: Context):
        context.scene.render.use_compositing = True
        context.scene.render.resolution_x = self.render_resolution
        context.scene.render.resolution_y = self.render_resolution
        context.scene.render.resolution_percentage = 100
        context.scene.render.pixel_aspect_x = 1
        context.scene.render.pixel_aspect_y = 1
        context.scene.render.image_settings.file_format = "OPEN_EXR"
        context.scene.render.image_settings.color_mode = "RGBA"
        context.scene.render.image_settings.color_depth = "32"
        context.scene.render.image_settings.exr_codec = "NONE"
        context.scene.render.image_settings.color_management = "OVERRIDE"
        context.scene.render.image_settings.linear_colorspace_settings.name = 'Non-Color'
        context.scene.update_render_engine()

    def set_quality_settings(self, context: Context):
        context.scene.render.bake.margin_type = 'EXTEND' if self.margin == 0 else 'ADJACENT_FACES'
        context.scene.render.bake.margin = self.margin
        context.scene.cycles.samples = self.samples

    lightmap_image: Image

    def create_lightmap_image(self):
        self.lightmap_image = bpy.data.images.get("NoisyLightmap")
        if self.lightmap_image:
            bpy.data.images.remove(self.lightmap_image)
        self.lightmap_image = bpy.data.images.new(
            name="NoisyLightmap",
            width=self.render_resolution,
            height=self.render_resolution,
            alpha=True,
            is_data=True,
            float_buffer=True
        )
        self.lightmap_image.colorspace_settings.name = 'Non-Color'
        self.lightmap_image.update()

    img_nodes: list[ShaderNodeTexImage]
    original_materials: dict[Object, list[Material]]
    passthrough_mat: Material
    fulltransparent_mat: Material

    def prepare_materials_for_baking(self):
        self.img_nodes = []
        self.original_materials = {}

        # Create Fully Transparent Material
        self.fulltransparent_mat = bpy.data.materials.new("Fully Transparent Material")
        self.fulltransparent_mat.use_nodes = True
        fulltransparent_nodes = self.fulltransparent_mat.node_tree.nodes
        fulltransparent_links = self.fulltransparent_mat.node_tree.links
        fulltransparent_nodes.remove(fulltransparent_nodes["Principled BSDF"])

        transparent_shader = cast(ShaderNodeBsdfTransparent, fulltransparent_nodes.new(type="ShaderNodeBsdfTransparent"))
        transparent_shader.name = "Transparent BSDF"
        transparent_shader.location = (-200, 200)

        fulltransparent_links.new(transparent_shader.outputs[0], fulltransparent_nodes["Material Output"].inputs[0])

        # Create Passthrough Material
        self.passthrough_mat = bpy.data.materials.new("Passthrough Material")
        self.passthrough_mat.use_nodes = True
        passthrough_nodes = self.passthrough_mat.node_tree.nodes
        passthrough_links = self.passthrough_mat.node_tree.links
        passthrough_nodes.remove(passthrough_nodes["Principled BSDF"])

        vc_node = cast(ShaderNodeVertexColor, passthrough_nodes.new(type="ShaderNodeVertexColor"))
        vc_node.name = "Vertex Color"
        vc_node.location = (-400, 100)
        vc_node.layer_name = "Color"

        rayportal_shader = cast(ShaderNodeBsdfRayPortal, passthrough_nodes.new(type="ShaderNodeBsdfRayPortal"))
        rayportal_shader.name = "Ray Portal BSDF"
        rayportal_shader.location = (-200, 100)

        transparent_shader = cast(ShaderNodeBsdfTransparent, passthrough_nodes.new(type="ShaderNodeBsdfTransparent"))
        transparent_shader.name = "Transparent BSDF"
        transparent_shader.location = (-200, 200)
        passthrough_links.new(vc_node.outputs[0], rayportal_shader.inputs[0])
        passthrough_links.new(vc_node.outputs[0], transparent_shader.inputs[0])

        add_shader = cast(ShaderNodeAddShader, passthrough_nodes.new(type="ShaderNodeAddShader"))
        add_shader.name = "Add Shader"
        add_shader.location = (-25, 100)
        passthrough_links.new(transparent_shader.outputs[0], add_shader.inputs[0])
        passthrough_links.new(rayportal_shader.outputs[0], add_shader.inputs[1])

        passthrough_links.new(add_shader.outputs[0], passthrough_nodes["Material Output"].inputs[0])

        # Preparation
        for bake_object in self.bake_objects:
            self.original_materials[bake_object] = []
            for slot in bake_object.material_slots:
                if slot.material and not slot.material.library:
                    # If Already Duplicated Material, Just Select The Image Node
                    mat = slot.material
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links
                    for node in nodes:
                        node.select = False
                    img_node = cast(ShaderNodeTexImage, nodes.get("LightMapImageNode"))
                    if img_node:
                        img_node.image = self.lightmap_image
                        img_node.select = True
                        nodes.active = img_node
                        self.img_nodes.append(img_node)
                    for name, link in links.items():
                        if link.from_node == img_node:
                            links.remove(link)
                    nodes.active = img_node
                    self.original_materials[bake_object].append(mat)
                elif slot.material and slot.material.light_baked:
                    # If It's First Time And Its Light Baked, Duplicate Material And Prepare
                    slot.material = mat = slot.material.copy()
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links
                    uv_node = nodes.new(type="ShaderNodeUVMap")
                    uv_node.location = (-750, 350)
                    uv_node.uv_map = "LightMap"
                    img_node = cast(ShaderNodeTexImage, nodes.new(type="ShaderNodeTexImage"))
                    img_node.name = img_node.label = "LightMapImageNode"
                    img_node.image = self.lightmap_image
                    img_node.location = (-500, 500)
                    links.new(uv_node.outputs["UV"], img_node.inputs["Vector"])
                    for node in nodes:
                        node.select = False
                    img_node.select = True
                    nodes.active = img_node
                    self.img_nodes.append(img_node)
                    self.original_materials[bake_object].append(mat)
                elif slot.material and slot.material.passthrough:
                    self.original_materials[bake_object].append(slot.material)
                    slot.material = self.passthrough_mat
                elif slot.material and slot.material.matfulltransparent:
                    self.original_materials[bake_object].append(slot.material)
                    slot.material = self.fulltransparent_mat
                else:
                    self.original_materials[bake_object].append(slot.material)
                    slot.material = None

    def prepare_to_denoise_lightmap(self, context):
        i = self.batch.name[5:]
        compositing_img_node = cast(CompositorNodeImage, context.scene.node_tree.nodes.get('Noisy Lightmap Slot'))
        if compositing_img_node:
            compositing_img_node.image = self.lightmap_image
        filepath = f"//Lightmaps/LM_B{i}.exr"
        context.scene.render.filepath = filepath

    def finalize_lightmap_denoise(self):
        i = self.batch.name[5:]
        filepath = f"//Lightmaps/LM_B{i}.exr"
        denoised_image = bpy.data.images.load(filepath, check_existing=True)
        denoised_image.reload()
        denoised_image.colorspace_settings.name = 'Non-Color'
        for img_node in self.img_nodes:
            img_node.image = denoised_image
        denoised_image.scale(self.final_resolution, self.final_resolution)
        denoised_image.save()

    def restore_materials(self):
        for bake_object in self.bake_objects:
            for i, slot in enumerate(bake_object.material_slots):
                mat = self.original_materials[bake_object][i]
                if not mat:
                    continue
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                img_node = nodes.get("LightMapImageNode")
                if not img_node:
                    slot.material = mat
                    continue
                for node in nodes:
                    lightmap_input = node.inputs.get("LightMap")
                    if lightmap_input:
                        links.new(img_node.outputs["Color"], lightmap_input)
                slot.material = mat
        bpy.data.materials.remove(self.passthrough_mat)
        bpy.data.materials.remove(self.fulltransparent_mat)


class UnpackCollections(PipelineOperator):
    bl_idname = "lightmap.unpack_collections"
    bl_label = "Unpack Collections ➕ Batch"
    bl_options = {'REGISTER', "UNDO", "UNDO_GROUPED"}

    uvMargin: IntProperty(name="UV Margin (in 1024)", default=4)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def get_tasks(self):
        return [
            PipelineTask(self.get_source),
            PipelineTask(self.realize_instances),
            PipelineTask(self.merge_batched_instances),
            PipelineTask(self.pack_uvs, timelog=True),
        ]

    source_collection: Collection

    def get_source(self):
        self.source_collection = bpy.data.collections.get('SOURCE')
        if not self.source_collection:
            return {'CANCELLED'}

    export_collection: Collection
    batches: list[list[Object]]

    def realize_instances(self, context: Context):
        # Go to object mode
        obj = GetTempActiveObj(self.source_collection)
        bpy.ops.object.mode_set(mode="OBJECT")
        ReleaseTempActiveObj(obj)

        # Create `EXPORT_STUFF` collection
        self.export_collection = GetOrCreateCollection("EXPORT_STUFF")

        # Extract batches
        self.batches = []
        for collection in self.source_collection.children:
            batch: list[Object] = []
            temp_export_collection = GetOrCreateCollection("TEMP_EXPORT_STUFF")

            for obj in collection.objects:
                print("Processing: ", obj.name)
                if obj.instance_collection is not None:
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    bpy.ops.object.duplicate()

                    for obj in context.scene.objects:
                        if obj.select_get():
                            obj.users_collection[0].objects.unlink(obj)
                            temp_export_collection.objects.link(obj)
                            bpy.ops.object.duplicates_make_real(use_base_parent=True, use_hierarchy=True)
                            bpy.ops.object.make_local(type='SELECT_OBDATA')

                    bpy.ops.object.select_all(action='DESELECT')
                    for obj in temp_export_collection.objects:
                        if obj.type == "EMPTY":
                            for child in obj.children:
                                child.name = child.name + "_EXPORT"
                                bpy.context.view_layer.objects.active = child
                                child.select_set(True)
                                bpy.ops.object.parent_clear(type='CLEAR_KEEP_TRANSFORM')
                            bpy.data.objects.remove(obj, do_unlink=True)

            for obj in temp_export_collection.objects:
                temp_export_collection.objects.unlink(obj)
                self.export_collection.objects.link(obj)
                if obj.parent is None:
                    batch.append(obj)

            DeleteCollection("TEMP_EXPORT_STUFF")
            self.batches.append(batch)

        for batch in self.batches:
            print(len(batch))

        # Exclude `SOURCE` collection
        GetLayerCollection("SOURCE", context.view_layer.layer_collection).exclude = True

    merged_batches: list[tuple[Object, Object | None]]

    def merge_batched_instances(self, context: Context):
        self.merged_batches = []
        for i, batch in enumerate(self.batches):
            children = []
            for o in batch:
                for c in o.children:
                    children.append((c, c.matrix_world.copy()))

            # make sure all objects have the lightmap_scale attribute
            batch_objs: list[Object] = []
            light_objs: list[Object] = []
            no_shadows_objs: list[Object] = []
            bpy.ops.object.select_all(action='DESELECT')
            for o in batch:
                if o.type == "LIGHT":
                    light_objs.append(o)
                elif o.type == "MESH":
                    context.view_layer.objects.active = o
                    o.select_set(True)
                    bm = bmesh.new()
                    bm.from_mesh(o.data)
                    layer = bm.faces.layers.float.get("lightmap_scale")
                    if not layer:
                        layer = bm.faces.layers.float.new("lightmap_scale")
                        for face in bm.faces:
                            face[layer] = 1.0
                    bm.to_mesh(o.data)
                    bm.free()
                    if o.visible_shadow:
                        batch_objs.append(o)
                    else:
                        no_shadows_objs.append(o)
                        o.select_set(False)
            for o in batch_objs:
                if o.data.users > 1:
                    o.data = o.data.copy()

            context.view_layer.objects.active = batch_objs[0]
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            bpy.ops.object.join()

            merged = context.active_object
            merged.name = f"Batch{i}"

            if len(no_shadows_objs) > 0:
                bpy.ops.object.select_all(action='DESELECT')
                for o in no_shadows_objs:
                    o.select_set(True)
                context.view_layer.objects.active = no_shadows_objs[0]
                bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
                bpy.ops.object.join()
                no_shadows_merged = context.active_object
                no_shadows_merged.name = f"Batch{i}_NoShadows"

                self.merged_batches.append((merged, no_shadows_merged))
            else:
                self.merged_batches.append((merged, None))

            inv = merged.matrix_world.inverted()
            c: Object
            for c, world_mtx in children:
                if c.name.startswith("UBX_") or c.name.startswith("UCX_") or c.name.startswith("UCP_") or c.name.startswith("USP_"):
                    c.hide_render = c.hide_viewport = True
                c.parent = merged
                c.matrix_parent_inverse = inv
                c.matrix_world = world_mtx

    def pack_uvs(self, context: Context):
        bpy.ops.object.select_all(action='DESELECT')
        for batch_merged, no_shadows_merged in self.merged_batches:
            context.view_layer.objects.active = batch_merged
            batch_merged.select_set(True)
            if no_shadows_merged:
                no_shadows_merged.select_set(True)
            bpy.ops.object.mode_set(mode="EDIT")
            UVPack_Scaled(context, self, [batch_merged, no_shadows_merged] if no_shadows_merged else [batch_merged], True, self.uvMargin, 1024)
            bpy.ops.object.mode_set(mode="OBJECT")
            if no_shadows_merged:
                no_shadows_merged.select_set(False)
            batch_merged.select_set(False)


class ScaledUVPacking(Operator):
    bl_idname = "lightmap.scaled_uv_packing"
    bl_label = "Scaled UV Packing"
    bl_options = {'REGISTER', "UNDO", "UNDO_GROUPED"}

    heuristic: bpy.props.BoolProperty(name="Heuristic", default=True)
    pixel_margin: bpy.props.IntProperty(name="Pixel Margin", default=6, min=1, max=256)
    texture_size: bpy.props.IntProperty(name="Texture Size", default=2048, min=32, max=4096)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context: Context):
        UVPack_Scaled(context, self, context.selected_objects, self.heuristic, self.pixel_margin, self.texture_size)
        return {'FINISHED'}


class SetLightmapScaleOperator(Operator):
    bl_idname = "lightmap.set_scale"
    bl_label = "Set Lightmap Scale"
    bl_description = "Sets the lightmap scale after normalization for the currently selected faces"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED'}

    scale: bpy.props.FloatProperty(name="Size", min=0, max=1, default=1)

    @classmethod
    def poll(cls, context: Context) -> bool:
        ao = context.active_object
        return ao is not None and ao.mode == "EDIT" and ao.type == "MESH"

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        for obj in context.selected_objects:
            if obj.type != "MESH" or obj.mode != "EDIT":
                continue
            mesh = obj.data
            bm = bmesh.from_edit_mesh(mesh)
            # Get or create a BMesh face layer for the lightmap scale
            layer = bm.faces.layers.float.get("lightmap_scale")
            if not layer:
                layer = bm.faces.layers.float.new("lightmap_scale")
                for face in bm.faces:
                    face[layer] = 1.0
            # Assign the scale to selected faces
            for face in bm.faces:
                if face.select:
                    face[layer] = self.scale
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        return {'FINISHED'}


class MZAGE_PT_MaterialMenu(bpy.types.Panel):
    bl_label = "Light Baking"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        obj = bpy.context.object
        mat = bpy.context.material
        layout.use_property_split = True
        layout.use_property_decorate = False
        if mat and obj.type == "MESH":
            layout.prop(mat, "light_baked")
            layout.prop(mat, "passthrough")
            layout.prop(mat, "matfulltransparent")


def OnDisplayLightingChanged(self, context: bpy.types.Context):
    val = self.display_lighting
    export_collection: Collection | None = bpy.data.collections.get("EXPORT_STUFF")
    if export_collection is None:
        return
    for name, batch in export_collection.objects.items():
        if batch.parent is not None:
            continue
        for slot in batch.material_slots:
            if slot.material is None:
                continue
            if slot.material.library:
                continue
            for node_name, node in slot.material.node_tree.nodes.items():
                lighting_mode_input = node.inputs.get("LightingMode")
                if lighting_mode_input:
                    lighting_mode_input.default_value = val


@persistent
def InitDisplayLighting(dummy):
    OnDisplayLightingChanged(bpy.context.scene, bpy.context)


def register():
    bpy.utils.register_class(SetLightmapScaleOperator)
    bpy.utils.register_class(ScaledUVPacking)
    bpy.utils.register_class(UnpackCollections)
    bpy.utils.register_class(BakeBatch)
    bpy.utils.register_class(ClearLightmappingStuff)
    bpy.utils.register_class(MZAGE_PT_MaterialMenu)

    bpy.types.Material.light_baked = bpy.props.BoolProperty(name="Light Baked", description="Whether it should be baked", default=False)
    bpy.types.Material.passthrough = bpy.props.BoolProperty(
        name="Glass (Ray Portal)", description="It provides a Ray Portal BSDF mixed with Transparent BSDF, Color controlled by Vertex Color", default=False)
    bpy.types.Material.matfulltransparent = bpy.props.BoolProperty(name="Passthrough", description="If material should not get affected and affect the lightmapping in ANY WAY", default=False)
    bpy.types.Scene.display_lighting = bpy.props.BoolProperty(name="Lighting Mode", default=False, update=OnDisplayLightingChanged)
    bpy.app.handlers.load_post.append(InitDisplayLighting)


def unregister():
    bpy.utils.unregister_class(SetLightmapScaleOperator)
    bpy.utils.unregister_class(ScaledUVPacking)
    bpy.utils.unregister_class(UnpackCollections)
    bpy.utils.unregister_class(BakeBatch)
    bpy.utils.unregister_class(ClearLightmappingStuff)
    bpy.utils.unregister_class(MZAGE_PT_MaterialMenu)

    del bpy.types.Material.light_baked
    del bpy.types.Material.passthrough
    del bpy.types.Material.matfulltransparent
    del bpy.types.Scene.display_lighting
    bpy.app.handlers.load_post.remove(InitDisplayLighting)
