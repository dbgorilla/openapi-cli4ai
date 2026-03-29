"""Tests for OpenAPI schema composition: allOf, oneOf, anyOf.

Verifies that resolve_refs correctly handles composition keywords
used in real-world API specs (Stripe, GitHub, Twilio, etc.).
"""

from __future__ import annotations

from openapi_cli4ai import cli as cli_mod


# ── allOf Tests ──────────────────────────────────────────────────────────────


class TestAllOf:
    """Verify that allOf merges sub-schemas into a single object."""

    def test_allof_merges_properties(self):
        """allOf should combine properties from all sub-schemas."""
        spec = {
            "components": {
                "schemas": {
                    "Base": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "created_at": {"type": "string"}},
                    },
                    "Pet": {
                        "allOf": [
                            {"$ref": "#/components/schemas/Base"},
                            {
                                "type": "object",
                                "properties": {"name": {"type": "string"}, "species": {"type": "string"}},
                            },
                        ]
                    },
                }
            }
        }

        resolved = cli_mod.resolve_refs(spec["components"]["schemas"]["Pet"], spec)
        assert "properties" in resolved
        assert "id" in resolved["properties"]
        assert "created_at" in resolved["properties"]
        assert "name" in resolved["properties"]
        assert "species" in resolved["properties"]

    def test_allof_merges_required_fields(self):
        """allOf should combine required fields from all sub-schemas."""
        spec = {}
        schema = {
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]},
            ]
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert "required" in resolved
        assert "a" in resolved["required"]
        assert "b" in resolved["required"]

    def test_allof_preserves_description(self):
        """allOf should keep description from first sub-schema that has one."""
        spec = {}
        schema = {
            "allOf": [
                {"description": "Base object", "type": "object", "properties": {"id": {"type": "integer"}}},
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ]
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert resolved.get("description") == "Base object"

    def test_allof_with_ref_and_inline(self):
        """allOf with mix of $ref and inline schemas should resolve correctly."""
        spec = {
            "components": {
                "schemas": {
                    "Timestamp": {
                        "type": "object",
                        "properties": {"created_at": {"type": "string", "format": "date-time"}},
                    }
                }
            }
        }
        schema = {
            "allOf": [
                {"$ref": "#/components/schemas/Timestamp"},
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ]
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert "created_at" in resolved["properties"]
        assert "name" in resolved["properties"]

    def test_allof_preserves_sibling_keys(self):
        """Sibling keys next to allOf should be preserved."""
        spec = {}
        schema = {
            "description": "A composite type",
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}},
            ],
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert resolved.get("description") == "A composite type"
        assert "a" in resolved["properties"]

    def test_allof_preserves_constraints(self):
        """allOf should preserve non-property keys like maxLength, pattern, enum."""
        spec = {}
        schema = {
            "allOf": [
                {"type": "string", "maxLength": 5},
                {"pattern": "^[A-Z]+$"},
            ]
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert resolved.get("type") == "string"
        assert resolved.get("maxLength") == 5
        assert resolved.get("pattern") == "^[A-Z]+$"

    def test_allof_preserves_additional_properties(self):
        """allOf should preserve additionalProperties and other schema keys."""
        spec = {}
        schema = {
            "allOf": [
                {"type": "object", "properties": {"id": {"type": "integer"}}},
                {"additionalProperties": False, "minProperties": 1},
            ]
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert resolved.get("additionalProperties") is False
        assert resolved.get("minProperties") == 1
        assert "id" in resolved["properties"]

    def test_allof_empty_schemas(self):
        """allOf with empty sub-schemas should produce empty merged result."""
        spec = {}
        schema = {"allOf": [{}, {}]}
        resolved = cli_mod.resolve_refs(schema, spec)
        assert isinstance(resolved, dict)
        assert "properties" not in resolved  # No properties from empty schemas


# ── oneOf Tests ──────────────────────────────────────────────────────────────


class TestOneOf:
    """Verify that oneOf resolves each variant."""

    def test_oneof_resolves_variants(self):
        """oneOf should resolve each variant schema."""
        spec = {
            "components": {
                "schemas": {
                    "Cat": {"type": "object", "properties": {"whiskers": {"type": "integer"}}},
                    "Dog": {"type": "object", "properties": {"breed": {"type": "string"}}},
                }
            }
        }
        schema = {
            "oneOf": [
                {"$ref": "#/components/schemas/Cat"},
                {"$ref": "#/components/schemas/Dog"},
            ]
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert "oneOf" in resolved
        assert len(resolved["oneOf"]) == 2
        assert "whiskers" in resolved["oneOf"][0]["properties"]
        assert "breed" in resolved["oneOf"][1]["properties"]

    def test_oneof_preserves_discriminator(self):
        """oneOf with discriminator sibling should preserve it."""
        spec = {}
        schema = {
            "oneOf": [
                {"type": "object", "properties": {"type": {"type": "string"}}},
                {"type": "object", "properties": {"type": {"type": "string"}}},
            ],
            "discriminator": {"propertyName": "type"},
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert "discriminator" in resolved
        assert resolved["discriminator"]["propertyName"] == "type"

    def test_oneof_inline_schemas(self):
        """oneOf with inline schemas (no $ref) should pass through resolved."""
        spec = {}
        schema = {
            "oneOf": [
                {"type": "string"},
                {"type": "integer"},
            ]
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert resolved["oneOf"][0]["type"] == "string"
        assert resolved["oneOf"][1]["type"] == "integer"


# ── anyOf Tests ──────────────────────────────────────────────────────────────


class TestAnyOf:
    """Verify that anyOf resolves each variant."""

    def test_anyof_resolves_variants(self):
        """anyOf should resolve each variant schema."""
        spec = {
            "components": {
                "schemas": {
                    "Address": {"type": "object", "properties": {"street": {"type": "string"}}},
                }
            }
        }
        schema = {
            "anyOf": [
                {"$ref": "#/components/schemas/Address"},
                {"type": "string"},
            ]
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert "anyOf" in resolved
        assert len(resolved["anyOf"]) == 2
        assert "street" in resolved["anyOf"][0]["properties"]
        assert resolved["anyOf"][1]["type"] == "string"

    def test_anyof_preserves_sibling_description(self):
        """anyOf with description sibling should preserve it."""
        spec = {}
        schema = {
            "description": "An address or string",
            "anyOf": [
                {"type": "object", "properties": {"zip": {"type": "string"}}},
                {"type": "string"},
            ],
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert resolved.get("description") == "An address or string"


# ── Nested Composition Tests ─────────────────────────────────────────────────


class TestNestedComposition:
    """Verify composition works when nested inside other schemas."""

    def test_allof_inside_property(self):
        """allOf nested inside a property should be resolved."""
        spec = {
            "components": {
                "schemas": {
                    "Name": {"type": "object", "properties": {"first": {"type": "string"}}},
                }
            }
        }
        schema = {
            "type": "object",
            "properties": {
                "owner": {
                    "allOf": [
                        {"$ref": "#/components/schemas/Name"},
                        {"type": "object", "properties": {"email": {"type": "string"}}},
                    ]
                }
            },
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        owner = resolved["properties"]["owner"]
        assert "first" in owner["properties"]
        assert "email" in owner["properties"]

    def test_oneof_inside_array_items(self):
        """oneOf inside array items should be resolved."""
        spec = {}
        schema = {
            "type": "array",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ]
            },
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert "oneOf" in resolved["items"]
        assert len(resolved["items"]["oneOf"]) == 2

    def test_composition_with_existing_refs(self):
        """Composition should work alongside existing $ref resolution."""
        spec = {
            "components": {
                "schemas": {
                    "Error": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "integer"},
                            "message": {"type": "string"},
                        },
                    },
                }
            }
        }
        # A response that uses $ref normally
        schema = {"$ref": "#/components/schemas/Error"}
        resolved = cli_mod.resolve_refs(schema, spec)
        assert "code" in resolved["properties"]

        # Same spec, but now with allOf wrapping
        schema2 = {
            "allOf": [
                {"$ref": "#/components/schemas/Error"},
                {"type": "object", "properties": {"details": {"type": "string"}}},
            ]
        }
        resolved2 = cli_mod.resolve_refs(schema2, spec)
        assert "code" in resolved2["properties"]
        assert "details" in resolved2["properties"]

    def test_ref_preserves_sibling_keys(self):
        """$ref with sibling keys like description should preserve them."""
        spec = {
            "components": {
                "schemas": {
                    "Pet": {"type": "object", "properties": {"name": {"type": "string"}}},
                }
            }
        }
        schema = {
            "$ref": "#/components/schemas/Pet",
            "description": "A pet object with extra context",
        }

        resolved = cli_mod.resolve_refs(schema, spec)
        assert "name" in resolved["properties"]
        assert resolved["description"] == "A pet object with extra context"
