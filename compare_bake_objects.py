"""Run from Blender's Text Editor to compare one good bake object with bad ones."""

import json
import math

import bpy


GOOD_OBJECT = "Classroom"
BAD_OBJECTS = [
    "Exterior",
    "ClassRoom_Exterior_NoClass.033",
    "Closet",
    "TeacherDesk",
    "ClassroomDoor",
]
OUTPUT_TEXT = "Bake Object Comparison"


def safe_value(value, depth=0):
    if depth > 5:
        return "<max depth>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return round(value, 9)
    if isinstance(value, bpy.types.ID):
        return {
            "type": value.bl_rna.identifier,
            "name": value.name,
            "library": value.library.filepath if value.library else None,
        }
    if isinstance(value, (list, tuple)) or hasattr(value, "to_list"):
        try:
            return [safe_value(item, depth + 1) for item in value]
        except (TypeError, ValueError):
            pass
    try:
        return safe_value(value[:], depth + 1)
    except (TypeError, ValueError, AttributeError):
        return str(value)


def custom_properties(owner):
    return {
        key: safe_value(owner[key])
        for key in sorted(owner.keys())
        if key != "_RNA_UI"
    }


def rna_properties(owner):
    result = {}
    ignored = {"rna_type", "name", "name_full", "original"}
    for prop in owner.bl_rna.properties:
        if prop.identifier in ignored or prop.is_readonly or prop.type in {'COLLECTION', 'POINTER'}:
            continue
        try:
            result[prop.identifier] = safe_value(getattr(owner, prop.identifier))
        except Exception as error:
            result[prop.identifier] = f"<unreadable: {error}>"
    return result


def attribute_snapshot(attribute):
    values = []
    for item in attribute.data:
        value = None
        for field in ("value", "color", "vector", "uv"):
            if hasattr(item, field):
                value = safe_value(getattr(item, field))
                break
        values.append(value)
    numeric = [v for v in values if isinstance(v, (int, float))]
    summary = {
        "domain": attribute.domain,
        "data_type": attribute.data_type,
        "count": len(values),
    }
    if numeric:
        summary.update(min=min(numeric), max=max(numeric), unique=sorted(set(numeric))[:32])
    else:
        summary["sample"] = values[:8]
    return summary


def uv_snapshot(mesh, layer):
    coords = [(item.uv.x, item.uv.y) for item in layer.data]
    if coords:
        bounds = [
            min(v[0] for v in coords), min(v[1] for v in coords),
            max(v[0] for v in coords), max(v[1] for v in coords),
        ]
    else:
        bounds = None
    return {
        "active": layer == mesh.uv_layers.active,
        "active_render": layer.active_render,
        "active_clone": layer.active_clone,
        "bounds": safe_value(bounds),
        "loop_count": len(coords),
    }


def material_snapshot(material):
    if material is None:
        return None
    result = {
        "identity": safe_value(material),
        "custom": custom_properties(material),
        "rna": rna_properties(material),
        "nodes": [],
    }
    if material.use_nodes and material.node_tree:
        for node in material.node_tree.nodes:
            node_info = {
                "type": node.bl_idname,
                "name": node.name,
                "label": node.label,
                "mute": node.mute,
            }
            if node.type == 'TEX_IMAGE':
                node_info["image"] = safe_value(node.image)
                node_info["interpolation"] = node.interpolation
                node_info["extension"] = node.extension
            if node.type == 'UVMAP':
                node_info["uv_map"] = node.uv_map
            result["nodes"].append(node_info)
        result["nodes"].sort(key=lambda item: (item["type"], item["name"]))
    return result


def object_snapshot(obj):
    mesh = obj.data if obj.type == 'MESH' else None
    result = {
        "identity": safe_value(obj),
        "object_custom": custom_properties(obj),
        "object_rna": rna_properties(obj),
        "matrix_world": safe_value([list(row) for row in obj.matrix_world]),
        "parent": safe_value(obj.parent),
        "parent_type": obj.parent_type,
        "collections": sorted(collection.name for collection in obj.users_collection),
        "hide_get": obj.hide_get(view_layer=bpy.context.view_layer),
        "visible_get": obj.visible_get(view_layer=bpy.context.view_layer),
        "select_get": obj.select_get(view_layer=bpy.context.view_layer),
        "modifiers": [],
        "constraints": [],
    }
    for modifier in obj.modifiers:
        result["modifiers"].append({
            "name": modifier.name,
            "type": modifier.type,
            "custom": custom_properties(modifier),
            "rna": rna_properties(modifier),
        })
    for constraint in obj.constraints:
        result["constraints"].append({
            "name": constraint.name,
            "type": constraint.type,
            "custom": custom_properties(constraint),
            "rna": rna_properties(constraint),
        })
    if mesh:
        mesh.calc_loop_triangles()
        result["mesh"] = {
            "identity": safe_value(mesh),
            "custom": custom_properties(mesh),
            "rna": rna_properties(mesh),
            "counts": {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
                "loops": len(mesh.loops),
                "loop_triangles": len(mesh.loop_triangles),
            },
            "material_indices": sorted(set(poly.material_index for poly in mesh.polygons)),
            "uv_layers": {
                layer.name: uv_snapshot(mesh, layer) for layer in mesh.uv_layers
            },
            "attributes": {
                attribute.name: attribute_snapshot(attribute)
                for attribute in mesh.attributes
            },
            "materials": [material_snapshot(material) for material in mesh.materials],
        }
    return result


def differences(good, bad, path=""):
    output = []
    if type(good) is not type(bad):
        return [{"path": path, "good": good, "bad": bad}]
    if isinstance(good, dict):
        for key in sorted(set(good) | set(bad)):
            child_path = f"{path}.{key}" if path else key
            if key not in good:
                output.append({"path": child_path, "good": "<missing>", "bad": bad[key]})
            elif key not in bad:
                output.append({"path": child_path, "good": good[key], "bad": "<missing>"})
            else:
                output.extend(differences(good[key], bad[key], child_path))
        return output
    if isinstance(good, list):
        if good != bad:
            output.append({"path": path, "good": good, "bad": bad})
        return output
    if good != bad:
        output.append({"path": path, "good": good, "bad": bad})
    return output


good_object = bpy.data.objects.get(GOOD_OBJECT)
if good_object is None:
    raise RuntimeError(f"Good object '{GOOD_OBJECT}' was not found")

good_snapshot = object_snapshot(good_object)
report = {"good_object": GOOD_OBJECT, "comparisons": {}}
for name in BAD_OBJECTS:
    bad_object = bpy.data.objects.get(name)
    if bad_object is None:
        report["comparisons"][name] = {"error": "object not found"}
        continue
    report["comparisons"][name] = differences(good_snapshot, object_snapshot(bad_object))

rendered = json.dumps(report, indent=2, ensure_ascii=False)
text = bpy.data.texts.get(OUTPUT_TEXT) or bpy.data.texts.new(OUTPUT_TEXT)
text.clear()
text.write(rendered)
print(rendered)
print(f"\nFull report written to Blender text block: {OUTPUT_TEXT}")
