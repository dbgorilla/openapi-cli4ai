"""Tests for OpenAPI spec parsing and $ref resolution."""

from __future__ import annotations


def test_extract_endpoint_summaries(cli_module, petstore_spec):
    """Should extract all endpoints from the Petstore spec."""
    endpoints = cli_module.extract_endpoint_summaries(petstore_spec)
    assert len(endpoints) == 19

    # Check a known endpoint
    find_by_status = [e for e in endpoints if e["path"] == "/pet/findByStatus"]
    assert len(find_by_status) == 1
    assert find_by_status[0]["method"] == "GET"
    assert find_by_status[0]["operationId"] == "findPetsByStatus"
    assert "pet" in find_by_status[0]["tags"]


def test_extract_endpoint_summaries_all_methods(cli_module, petstore_spec):
    """Should extract endpoints with correct HTTP methods."""
    endpoints = cli_module.extract_endpoint_summaries(petstore_spec)
    methods = {e["method"] for e in endpoints}
    assert "GET" in methods
    assert "POST" in methods
    assert "PUT" in methods
    assert "DELETE" in methods


def test_extract_endpoint_summaries_truncates_summary(cli_module):
    """Should truncate long summaries to 120 chars."""
    spec = {
        "paths": {
            "/test": {
                "get": {
                    "operationId": "test_op",
                    "summary": "A" * 200 + ". Second sentence.",
                    "tags": [],
                }
            }
        }
    }
    endpoints = cli_module.extract_endpoint_summaries(spec)
    assert len(endpoints) == 1
    assert len(endpoints[0]["summary"]) <= 120


def test_extract_endpoint_summaries_missing_operation_id(cli_module):
    """Should generate operationId from method_path when missing."""
    spec = {
        "paths": {
            "/test/path": {
                "get": {
                    "summary": "Test endpoint",
                    "tags": [],
                }
            }
        }
    }
    endpoints = cli_module.extract_endpoint_summaries(spec)
    assert endpoints[0]["operationId"] == "get_/test/path"


def test_extract_endpoint_summaries_deprecated(cli_module):
    """Should mark deprecated endpoints."""
    spec = {
        "paths": {
            "/old": {
                "get": {
                    "operationId": "old_op",
                    "summary": "Old endpoint",
                    "deprecated": True,
                    "tags": [],
                }
            },
            "/new": {
                "get": {
                    "operationId": "new_op",
                    "summary": "New endpoint",
                    "tags": [],
                }
            },
        }
    }
    endpoints = cli_module.extract_endpoint_summaries(spec)
    deprecated = [e for e in endpoints if e["deprecated"]]
    assert len(deprecated) == 1
    assert deprecated[0]["operationId"] == "old_op"


def test_extract_endpoint_summaries_empty_spec(cli_module):
    """Should return empty list for spec with no paths."""
    assert cli_module.extract_endpoint_summaries({}) == []
    assert cli_module.extract_endpoint_summaries({"paths": {}}) == []


def test_resolve_refs_simple(cli_module):
    """Should resolve a simple $ref pointer."""
    spec = {
        "components": {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                }
            }
        }
    }
    schema = {"$ref": "#/components/schemas/Pet"}
    resolved = cli_module.resolve_refs(schema, spec)
    assert resolved["type"] == "object"
    assert "name" in resolved["properties"]


def test_resolve_refs_nested(cli_module):
    """Should resolve nested $ref pointers."""
    spec = {
        "components": {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "properties": {
                        "category": {"$ref": "#/components/schemas/Category"},
                    },
                },
                "Category": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                },
            }
        }
    }
    schema = {"$ref": "#/components/schemas/Pet"}
    resolved = cli_module.resolve_refs(schema, spec)
    assert resolved["properties"]["category"]["type"] == "object"
    assert "name" in resolved["properties"]["category"]["properties"]


def test_resolve_refs_max_depth(cli_module):
    """Should stop resolving at max depth to prevent infinite loops."""
    spec = {
        "components": {
            "schemas": {
                "A": {"$ref": "#/components/schemas/B"},
                "B": {"$ref": "#/components/schemas/A"},  # Circular
            }
        }
    }
    schema = {"$ref": "#/components/schemas/A"}
    # Should not raise, just stop resolving
    resolved = cli_module.resolve_refs(schema, spec, max_depth=5)
    assert isinstance(resolved, dict)


def test_resolve_refs_preserves_non_refs(cli_module):
    """Should leave non-$ref values unchanged."""
    spec = {"components": {}}
    schema = {"type": "string", "format": "email"}
    resolved = cli_module.resolve_refs(schema, spec)
    assert resolved == {"type": "string", "format": "email"}


def test_extract_full_endpoint_schema(cli_module, petstore_spec):
    """Should extract full schema for a specific endpoint."""
    schema = cli_module.extract_full_endpoint_schema(petstore_spec, "findPetsByStatus")
    assert schema is not None
    assert schema["method"] == "GET"
    assert schema["path"] == "/pet/findByStatus"
    assert schema["operationId"] == "findPetsByStatus"
    assert "parameters" in schema
    assert len(schema["parameters"]) > 0


def test_extract_full_endpoint_schema_not_found(cli_module, petstore_spec):
    """Should return None for nonexistent operationId."""
    schema = cli_module.extract_full_endpoint_schema(petstore_spec, "nonexistent_op")
    assert schema is None


def test_compact_schema_limits_properties(cli_module):
    """Should truncate schemas with many properties."""
    schema = {
        "type": "object",
        "properties": {f"prop_{i}": {"type": "string"} for i in range(30)},
    }
    compact = cli_module._compact_schema(schema, max_props=10)
    assert len(compact["properties"]) == 10
    assert "_note" in compact
    assert "20" in compact["_note"]


def test_compact_schema_preserves_small(cli_module):
    """Should not truncate schemas with few properties."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    }
    compact = cli_module._compact_schema(schema, max_props=10)
    assert len(compact["properties"]) == 2
    assert "_note" not in compact
