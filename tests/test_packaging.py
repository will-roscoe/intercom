"""Guards on the files Home Assistant and HACS parse.

These mirror the checks hassfest runs in CI, so a bad selector key or a service
field with no translation is caught before it reaches a pull request.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import yaml

COMPONENT = Path(__file__).parent.parent / "custom_components" / "intercom"

# Key sets taken from script/hassfest/services.py, custom-integration variants.
SERVICE_KEYS = {"fields", "description", "name"}
FIELD_KEYS = {
    "example",
    "default",
    "required",
    "advanced",
    "selector",
    "description",
    "name",
}


def load(name):
    text = (COMPONENT / name).read_text()
    return yaml.safe_load(text) if name.endswith(".yaml") else json.loads(text)


def test_services_yaml_uses_only_keys_hassfest_allows():
    for service, schema in load("services.yaml").items():
        assert not set(schema) - SERVICE_KEYS, f"{service} has unknown keys"
        for name, field in (schema.get("fields") or {}).items():
            assert not set(field) - FIELD_KEYS, f"{service}.{name} has unknown keys"
            selector = field.get("selector")
            assert selector is None or isinstance(selector, dict)


def test_manifest_is_complete_and_versioned():
    manifest = load("manifest.json")
    for key in (
        "domain",
        "name",
        "documentation",
        "codeowners",
        "version",
        "iot_class",
    ):
        assert key in manifest, f"manifest is missing {key}"
    assert manifest["domain"] == "intercom"
    assert re.match(r"^\d+\.\d+\.\d+", manifest["version"])


def test_every_component_import_is_a_declared_dependency():
    """hassfest rejects importing a component that is not declared."""
    manifest = load("manifest.json")
    declared = set(manifest["dependencies"]) | set(
        manifest.get("after_dependencies", [])
    )
    imported = set()
    for path in COMPONENT.glob("*.py"):
        imported |= set(
            re.findall(r"homeassistant\.components\.([a-z_]+)", path.read_text())
        )
    assert not imported - declared, (
        f"undeclared component imports: {imported - declared}"
    )


def test_strings_and_translations_match():
    assert load("strings.json") == load("translations/en.json")


def test_every_service_field_has_a_translation():
    fields = set(load("services.yaml")["broadcast"]["fields"])
    translated = set(load("strings.json")["services"]["broadcast"]["fields"])
    assert fields == translated


def test_service_schema_matches_services_yaml():
    """The voluptuous schema and the UI description must not drift apart."""
    source = (COMPONENT / "__init__.py").read_text()
    constants = (COMPONENT / "const.py").read_text()
    values = dict(re.findall(r'ATTR_([A-Z_]+): Final = "([a-z_]+)"', constants))
    used = re.findall(r"vol\.(?:Required|Optional)\(ATTR_([A-Z_]+)", source)
    assert {values[name] for name in used} == set(
        load("services.yaml")["broadcast"]["fields"]
    )


def test_config_flow_keys_have_translations():
    constants = (COMPONENT / "const.py").read_text()
    values = dict(re.findall(r'CONF_([A-Z_]+): Final = "([a-z_]+)"', constants))
    used = re.findall(
        r"vol\.Optional\(\s*CONF_([A-Z_]+)", (COMPONENT / "config_flow.py").read_text()
    )
    keys = {values[name] for name in used}
    strings = load("strings.json")
    assert set(strings["config"]["step"]["user"]["data"]) == keys
    assert set(strings["options"]["step"]["init"]["data"]) == keys


def test_card_is_served_and_registered_under_the_same_name():
    constants = (COMPONENT / "const.py").read_text()
    filename = re.search(r'CARD_FILENAME: Final = "([^"]+)"', constants).group(1)
    assert (COMPONENT / "www" / filename).is_file()
