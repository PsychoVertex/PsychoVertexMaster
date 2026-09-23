import copy
import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "pvm_menu_model", Path(__file__).parents[1] / "HandyMenu" / "model.py")
MODEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL)


class MenuModelTests(unittest.TestCase):
    def test_safe_property_paths(self):
        valid = (
            "context.scene.display_lighting",
            "context.object.display_type",
            'bpy.data.materials["Material"].use_nodes',
            'context.object["my_property"]',
        )
        for path in valid:
            self.assertEqual(MODEL.parse_property_path(path), path)

    def test_unsafe_property_paths_are_rejected(self):
        invalid = (
            "bpy.context.scene.render.engine",
            "context.object.hide_set(True)",
            "context.object.__class__",
            "context.object[name]",
            "some_global.value",
        )
        for path in invalid:
            with self.assertRaises(MODEL.MenuConfigError):
                MODEL.parse_property_path(path)

    def test_shipped_default_is_valid_and_ids_are_unique(self):
        config = MODEL.load_default()
        identifiers = []
        for menu, _parents in MODEL.walk_menus(config):
            identifiers.append(menu["id"])
            identifiers.extend(item["id"] for item in menu["items"])
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_native_default_entries_use_custom_item_types(self):
        config = MODEL.load_default()
        items = {
            item["id"]: item
            for menu, _parents in MODEL.walk_menus(config)
            for item in menu["items"]
        }
        self.assertEqual(items["uv-clear-seam"]["type"], "custom_operator")
        self.assertEqual(items["normal-flip"]["type"], "custom_operator")
        self.assertEqual(items["export-fbx"]["type"], "custom_operator")
        self.assertEqual(items["object-display"]["type"], "custom_property")
        self.assertEqual(items["overlay-all"]["type"], "custom_property")

    def test_eight_item_limit(self):
        config = MODEL.clone_default()
        config["roots"]["edit"]["items"].append(
            {"id": "ninth", "type": "separator", "enabled": True})
        with self.assertRaisesRegex(MODEL.MenuConfigError, "at most 8 enabled"):
            MODEL.validate_config(config)

    def test_disabled_overflow_item_is_valid(self):
        config = MODEL.clone_default()
        config["roots"]["edit"]["items"].append(
            {"id": "disabled-ninth", "type": "separator", "enabled": False})
        MODEL.validate_config(config)

    def test_duplicate_nested_id_is_rejected(self):
        config = MODEL.clone_default()
        config["roots"]["object"]["id"] = config["roots"]["edit"]["id"]
        with self.assertRaisesRegex(MODEL.MenuConfigError, "duplicate stable id"):
            MODEL.validate_config(config)

    def test_invalid_input_does_not_mutate_existing_configuration(self):
        current = MODEL.clone_default()
        before = copy.deepcopy(current)
        with self.assertRaises(MODEL.MenuConfigError):
            MODEL.loads('{"schema_version": 2, "roots": {}}')
        self.assertEqual(current, before)

    def test_operator_parser_accepts_identifier_and_literal_keywords(self):
        self.assertEqual(MODEL.parse_operator_command("mesh.mark_seam"),
                         ("mesh.mark_seam", {}))
        self.assertEqual(
            MODEL.parse_operator_command("bpy.ops.mesh.mark_seam(clear=True)"),
            ("mesh.mark_seam", {"clear": True}))

    def test_operator_parser_rejects_executable_python(self):
        invalid = (
            "__import__('os').system('x')",
            "bpy.ops.mesh.test(value=get_value())",
            "bpy.ops.mesh.test(1)",
            "bpy.ops.mesh.test(**values)",
            "bpy.ops.mesh.test(); bpy.ops.wm.quit_blender()",
        )
        for command in invalid:
            with self.subTest(command=command):
                with self.assertRaises(MODEL.MenuConfigError):
                    MODEL.parse_operator_command(command)

    def test_wrong_schema_version_is_rejected(self):
        config = MODEL.clone_default()
        config["schema_version"] = 99
        with self.assertRaisesRegex(MODEL.MenuConfigError, "schema_version"):
            MODEL.validate_config(config)

    def test_v1_migration_removes_retired_unpack_all_action(self):
        config = MODEL.clone_default()
        config["schema_version"] = 1
        menu, _parents = MODEL.find_menu(config, "menu-lightmap-object")
        menu["items"].append({
            "id": "custom-old-unpack",
            "type": "plugin_action",
            "enabled": True,
            "action": "lightmap.unpack",
        })
        migrated = MODEL.loads(MODEL.dumps({**config, "schema_version": 2}).replace(
            '"schema_version": 2', '"schema_version": 1', 1))
        actions = [
            item.get("action")
            for menu, _parents in MODEL.walk_menus(migrated)
            for item in menu["items"]
        ]
        self.assertNotIn("lightmap.unpack", actions)



if __name__ == "__main__":
    unittest.main()
