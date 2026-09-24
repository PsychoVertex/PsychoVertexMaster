import importlib.util
import math
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "pvm_scene_export_model", Path(__file__).parents[1] / "SceneExport" / "model.py")
MODEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL)


class SceneExportModelTests(unittest.TestCase):
    def test_sanitize_and_static_mesh_name(self):
        self.assertEqual(MODEL.sanitize_name("Bench A.01"), "Bench_A_01")
        self.assertEqual(MODEL.static_mesh_name("Bench A"), "SM_Bench_A")
        self.assertEqual(MODEL.static_mesh_name("SM_Bench"), "SM_Bench")
        self.assertEqual(MODEL.sanitize_name("12 Steps"), "A_12_Steps")
        with self.assertRaises(ValueError):
            MODEL.sanitize_name("...")

    def test_collision_prefixes(self):
        for prefix in MODEL.COLLISION_PREFIXES:
            self.assertEqual(MODEL.collision_prefix(prefix + "Bench"), prefix)
        self.assertIsNone(MODEL.collision_prefix("Bench"))

    def test_suffix_stripping_is_explicit(self):
        self.assertEqual(MODEL.strip_blender_suffix("Bench.001"), "Bench")
        self.assertEqual(MODEL.strip_blender_suffix("Bench.01"), "Bench.01")

    def test_matrix_clustering(self):
        identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        close = [[1, 0, 0, 0.0000001], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        moved = [[1, 0, 0, 2], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        groups = MODEL.cluster_matrices([{"matrix": identity}, {"matrix": close}, {"matrix": moved}])
        self.assertEqual(sorted(map(len, groups)), [1, 2])

    def test_blender_to_unreal_transform(self):
        result = MODEL.blender_to_unreal_transform(
            (1, 2, 3), (0.1, 0.2, 0.3, 0.9), (2, 3, 4), 1.0)
        self.assertEqual(result["location_cm"], [100, -200, 300])
        self.assertEqual(result["rotation_xyzw"], [-0.1, 0.2, -0.3, 0.9])
        self.assertEqual(result["scale"], [2, 3, 4])
        with self.assertRaises(ValueError):
            MODEL.blender_to_unreal_transform((0, 0, 0), (0, 0, 0, 1), (1, 0, 1))
        with self.assertRaises(ValueError):
            MODEL.blender_to_unreal_transform((math.inf, 0, 0), (0, 0, 0, 1), (1, 1, 1))

    def test_affine_validation_rejects_shear_and_perspective(self):
        identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        self.assertTrue(MODEL.validate_affine_matrix(identity))
        shear = [[1, 0.25, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        with self.assertRaisesRegex(ValueError, "shear"):
            MODEL.validate_affine_matrix(shear)
        perspective = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0.1, 0, 0, 1]]
        with self.assertRaisesRegex(ValueError, "perspective"):
            MODEL.validate_affine_matrix(perspective)

    def test_document_contract_and_stable_identity(self):
        first = MODEL.make_document(
            addon_version=(1, 3, 0), scene_name="Scene", blend_path="C:/work/test.blend",
            unit_scale_meters=1, assets=[], placements=[])
        second = MODEL.make_document(
            addon_version=(1, 3, 0), scene_name="Scene", blend_path="C:/work/test.blend",
            unit_scale_meters=1, assets=[], placements=[])
        self.assertEqual(first["schema"], "pvm.unreal_scene")
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["scene"]["reconstruction_id"], second["scene"]["reconstruction_id"])


if __name__ == "__main__":
    unittest.main()
