"""Pure helpers for the baked-scene Unreal export contract."""

from __future__ import annotations

import hashlib
import math
import os
import re


SCHEMA = "pvm.unreal_scene"
SCHEMA_VERSION = 1
COLLISION_PREFIXES = ("UBX_", "UCX_", "UCP_", "USP_")
_BLENDER_SUFFIX = re.compile(r"\.\d{3}$")
_INVALID_NAME = re.compile(r"[^A-Za-z0-9_]+")


def sanitize_name(value: str) -> str:
    name = _INVALID_NAME.sub("_", value.strip()).strip("_")
    if not name:
        raise ValueError("name has no Unreal-compatible characters")
    if name[0].isdigit():
        name = "A_" + name
    return name


def static_mesh_name(asset_id: str) -> str:
    name = sanitize_name(asset_id)
    return name if name.startswith("SM_") else "SM_" + name


def collision_prefix(name: str) -> str | None:
    return next((prefix for prefix in COLLISION_PREFIXES if name.startswith(prefix)), None)


def strip_blender_suffix(name: str) -> str:
    """Only a utility: callers decide whether identity evidence allows stripping."""
    return _BLENDER_SUFFIX.sub("", name)


def matrix_key(values, digits=6):
    return tuple(round(float(value), digits) for row in values for value in row)


def cluster_matrices(records, digits=6):
    groups = {}
    for record in records:
        groups.setdefault(matrix_key(record["matrix"], digits), []).append(record)
    return list(groups.values())


def validate_affine_matrix(values, tolerance=1e-6):
    """Reject perspective, zero scale, and shear using a plain 4x4 matrix."""
    rows = [[float(value) for value in row] for row in values]
    if len(rows) != 4 or any(len(row) != 4 for row in rows):
        raise ValueError("transform must be a 4x4 matrix")
    if not all(math.isfinite(value) for row in rows for value in row):
        raise ValueError("transform contains a non-finite value")
    if any(abs(rows[3][index]) > tolerance for index in range(3)) or abs(rows[3][3] - 1.0) > tolerance:
        raise ValueError("transform contains a perspective component")
    columns = [tuple(rows[row][column] for row in range(3)) for column in range(3)]
    lengths = [math.sqrt(sum(value * value for value in column)) for column in columns]
    if min(lengths) <= tolerance:
        raise ValueError("transform has a zero scale axis")
    for first, second in ((0, 1), (0, 2), (1, 2)):
        dot = sum(columns[first][index] * columns[second][index] for index in range(3))
        if abs(dot) > tolerance * lengths[first] * lengths[second]:
            raise ValueError("transform contains shear that Unreal FTransform cannot reproduce")
    return True


def blender_to_unreal_transform(location, quaternion_xyzw, scale, unit_scale_meters=1.0):
    values = (*location, *quaternion_xyzw, *scale, unit_scale_meters)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("transform contains a non-finite value")
    if any(abs(float(value)) <= 1e-8 for value in scale):
        raise ValueError("transform has a zero scale axis")
    x, y, z = (float(value) for value in location)
    qx, qy, qz, qw = (float(value) for value in quaternion_xyzw)
    sx, sy, sz = (float(value) for value in scale)
    distance = 100.0 * float(unit_scale_meters)
    return {
        "location_cm": [distance * x, -distance * y, distance * z],
        "rotation_xyzw": [-qx, qy, -qz, qw],
        "scale": [sx, sy, sz],
    }


def reconstruction_id(blend_path: str) -> str:
    normalized = os.path.normcase(os.path.abspath(blend_path)).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def occurrence_id(
        reconstruction: str, asset_id: str, kind: str, batch: int,
        matrix_values, source_name: str = "") -> str:
    payload = "|".join((
        reconstruction, asset_id, kind, str(batch), source_name,
        repr(matrix_key(matrix_values)),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def make_document(*, addon_version, scene_name, blend_path, unit_scale_meters, assets, placements):
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "generator": {
            "addon": "PsychoVertexMaster",
            "addon_version": ".".join(str(part) for part in addon_version),
        },
        "scene": {
            "name": scene_name,
            "reconstruction_id": reconstruction_id(blend_path),
            "blender_unit_scale_meters": float(unit_scale_meters),
        },
        "assets": assets,
        "placements": placements,
    }
