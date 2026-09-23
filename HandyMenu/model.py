"""Pure-Python menu configuration, validation, and safe operator parsing."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path


SCHEMA_VERSION = 2


def _migrate_v1_to_v2(config):
    """Remove the retired Unpack All action from customized menus."""
    def remove_from(menu):
        retained = []
        for item in menu.get("items", []):
            if item.get("type") == "plugin_action" and item.get("action") == "lightmap.unpack":
                continue
            if item.get("type") == "submenu" and isinstance(item.get("menu"), dict):
                remove_from(item["menu"])
            retained.append(item)
        menu["items"] = retained

    for root in config.get("roots", {}).values():
        if isinstance(root, dict):
            remove_from(root)
    config["schema_version"] = 2
    return config


MIGRATIONS = {1: _migrate_v1_to_v2}
ROOT_KEYS = ("edit", "object", "no_active")
ITEM_TYPES = ("separator", "submenu", "plugin_action", "custom_operator", "custom_property")


class MenuConfigError(ValueError):
    pass


def _fail(path, message):
    raise MenuConfigError(f"{path}: {message}")


def parse_operator_command(command):
    """Return (operator_id, literal keyword properties) without evaluating code."""
    text = command.strip()
    if not text:
        raise MenuConfigError("operator command is empty")
    if text.count(".") == 1 and "(" not in text:
        parts = text.split(".")
        if all(part.isidentifier() for part in parts):
            return text, {}
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise MenuConfigError(f"invalid operator command: {exc.msg}") from None
    call = tree.body
    if not isinstance(call, ast.Call) or call.args:
        raise MenuConfigError("expected one bpy.ops.category.operator(...) call with keyword arguments")
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Attribute)
        and isinstance(func.value.value, ast.Attribute)
        and isinstance(func.value.value.value, ast.Name)
        and func.value.value.value.id == "bpy"
        and func.value.value.attr == "ops"
    ):
        raise MenuConfigError("only bpy.ops.category.operator(...) is allowed")
    operator_id = f"{func.value.attr}.{func.attr}"
    properties = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise MenuConfigError("expanded keyword arguments are not allowed")
        try:
            value = ast.literal_eval(keyword.value)
        except (ValueError, TypeError, SyntaxError):
            raise MenuConfigError(f"property '{keyword.arg}' must be a literal value") from None
        if not _is_json_value(value):
            raise MenuConfigError(f"property '{keyword.arg}' must be JSON-compatible")
        properties[keyword.arg] = value
    return operator_id, properties


def parse_property_path(path):
    """Validate a safe Blender data path without resolving or evaluating it."""
    text = path.strip()
    if not text:
        raise MenuConfigError("property path is empty")
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError as exc:
        raise MenuConfigError(f"invalid property path: {exc.msg}") from None

    def validate(part):
        if isinstance(part, ast.Name):
            if part.id != "context":
                raise MenuConfigError("property path must start with context or bpy.data")
            return
        if isinstance(part, ast.Attribute):
            if part.attr.startswith("_"):
                raise MenuConfigError("private attributes are not allowed")
            if isinstance(part.value, ast.Name) and part.value.id == "bpy":
                if part.attr != "data":
                    raise MenuConfigError("only bpy.data is allowed")
                return
            validate(part.value)
            return
        if isinstance(part, ast.Subscript):
            validate(part.value)
            key = part.slice
            if not isinstance(key, ast.Constant) or not isinstance(key.value, (str, int)):
                raise MenuConfigError("collection keys must be literal strings or integers")
            return
        raise MenuConfigError("only attribute access and literal collection keys are allowed")

    validate(node)
    if isinstance(node, ast.Name) or (isinstance(node, ast.Attribute)
                                     and isinstance(node.value, ast.Name)
                                     and node.value.id == "bpy"):
        raise MenuConfigError("property path must end with a property")
    return text


def _is_json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def validate_config(data):
    if not isinstance(data, dict):
        _fail("$", "configuration must be an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        _fail("$.schema_version", f"expected {SCHEMA_VERSION}")
    roots = data.get("roots")
    if not isinstance(roots, dict) or set(roots) != set(ROOT_KEYS):
        _fail("$.roots", f"must contain exactly {', '.join(ROOT_KEYS)}")
    seen = set()
    for key in ROOT_KEYS:
        _validate_menu(roots[key], f"$.roots.{key}", seen, 0)
    return data


def migrate_config(data):
    """Apply sequential migrations; add version-to-version functions to MIGRATIONS."""
    if not isinstance(data, dict):
        _fail("$", "configuration must be an object")
    version = data.get("schema_version")
    if not isinstance(version, int):
        _fail("$.schema_version", "integer required")
    if version > SCHEMA_VERSION:
        _fail("$.schema_version", f"version {version} is newer than supported version {SCHEMA_VERSION}")
    migrated = copy.deepcopy(data)
    while version < SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            _fail("$.schema_version", f"no migration is available from version {version}")
        migrated = migration(migrated)
        version = migrated.get("schema_version")
    return migrated


def _validate_menu(menu, path, seen, depth):
    if depth > 64:
        _fail(path, "nesting exceeds the practical limit of 64")
    if not isinstance(menu, dict):
        _fail(path, "menu must be an object")
    menu_id = menu.get("id")
    if not isinstance(menu_id, str) or not menu_id:
        _fail(path + ".id", "non-empty string required")
    if menu_id in seen:
        _fail(path + ".id", f"duplicate stable id '{menu_id}'")
    seen.add(menu_id)
    if not isinstance(menu.get("label"), str):
        _fail(path + ".label", "string required")
    items = menu.get("items")
    if not isinstance(items, list):
        _fail(path + ".items", "array required")
    if sum(bool(item.get("enabled", True)) for item in items if isinstance(item, dict)) > 8:
        _fail(path + ".items", "menus may contain at most 8 enabled items")
    for index, item in enumerate(items):
        item_path = f"{path}.items[{index}]"
        if not isinstance(item, dict):
            _fail(item_path, "item must be an object")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            _fail(item_path + ".id", "non-empty string required")
        if item_id in seen:
            _fail(item_path + ".id", f"duplicate stable id '{item_id}'")
        seen.add(item_id)
        item_type = item.get("type")
        if item_type not in ITEM_TYPES:
            _fail(item_path + ".type", f"expected one of {', '.join(ITEM_TYPES)}")
        if not isinstance(item.get("enabled", True), bool):
            _fail(item_path + ".enabled", "boolean required")
        for field in ("label", "icon"):
            if field in item and not isinstance(item[field], str):
                _fail(item_path + "." + field, "string required")
        if item_type == "submenu":
            _validate_menu(item.get("menu"), item_path + ".menu", seen, depth + 1)
        elif item_type == "plugin_action":
            if not isinstance(item.get("action"), str) or not item["action"]:
                _fail(item_path + ".action", "non-empty string required")
        elif item_type == "custom_operator":
            operator_id = item.get("operator_id")
            properties = item.get("properties", {})
            if not isinstance(operator_id, str) or operator_id.count(".") != 1:
                _fail(item_path + ".operator_id", "expected category.operator")
            if not isinstance(properties, dict) or not _is_json_value(properties):
                _fail(item_path + ".properties", "JSON-compatible object required")
        elif item_type == "custom_property":
            property_path = item.get("property_path")
            if not isinstance(property_path, str):
                _fail(item_path + ".property_path", "string required")
            try:
                parse_property_path(property_path)
            except MenuConfigError as exc:
                _fail(item_path + ".property_path", str(exc))


def loads(text):
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MenuConfigError(f"invalid JSON: {exc}") from None
    data = migrate_config(data)
    validate_config(data)
    return data


def dumps(data):
    validate_config(data)
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)


def load_default():
    path = Path(__file__).with_name("default_menu.json")
    return loads(path.read_text(encoding="utf-8"))


def clone_default():
    return copy.deepcopy(load_default())


def walk_menus(config):
    def visit(menu, parents):
        yield menu, parents
        for item in menu["items"]:
            if item["type"] == "submenu":
                yield from visit(item["menu"], parents + [menu])
    for root in ROOT_KEYS:
        yield from visit(config["roots"][root], [])


def find_menu(config, menu_id):
    for menu, parents in walk_menus(config):
        if menu["id"] == menu_id:
            return menu, parents
    return None, []
