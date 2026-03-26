"""Tests for the run command and input routing."""

from __future__ import annotations


def test_route_inputs_path_params(cli_module):
    """Should route path parameters correctly."""
    parameters = [
        {"name": "petId", "in": "path"},
        {"name": "status", "in": "query"},
    ]
    input_data = {"petId": 123, "status": "available"}

    path_params, query_params, header_params, body = cli_module._route_inputs(
        input_data, parameters, has_request_body=False
    )

    assert path_params == {"petId": 123}
    assert query_params == {"status": "available"}
    assert header_params == {}
    assert body is None


def test_route_inputs_query_params(cli_module):
    """Should route query parameters correctly."""
    parameters = [
        {"name": "status", "in": "query"},
        {"name": "limit", "in": "query"},
    ]
    input_data = {"status": "available", "limit": 10}

    path_params, query_params, header_params, body = cli_module._route_inputs(
        input_data, parameters, has_request_body=False
    )

    assert path_params == {}
    assert query_params == {"status": "available", "limit": 10}
    assert body is None


def test_route_inputs_header_params(cli_module):
    """Should route header parameters correctly."""
    parameters = [
        {"name": "X-Request-Id", "in": "header"},
    ]
    input_data = {"X-Request-Id": "abc-123", "name": "Rex"}

    path_params, query_params, header_params, body = cli_module._route_inputs(
        input_data, parameters, has_request_body=True
    )

    assert header_params == {"X-Request-Id": "abc-123"}
    assert body == {"name": "Rex"}


def test_route_inputs_body_from_undeclared_keys(cli_module):
    """Keys not matching any parameter should go to body."""
    parameters = [
        {"name": "petId", "in": "path"},
    ]
    input_data = {"petId": 1, "name": "Rex", "status": "available"}

    path_params, query_params, header_params, body = cli_module._route_inputs(
        input_data, parameters, has_request_body=True
    )

    assert path_params == {"petId": 1}
    assert body == {"name": "Rex", "status": "available"}


def test_route_inputs_all_body_when_no_params(cli_module):
    """When no parameters declared and requestBody exists, send all as body."""
    parameters = []
    input_data = {"name": "Rex", "status": "available"}

    path_params, query_params, header_params, body = cli_module._route_inputs(
        input_data, parameters, has_request_body=True
    )

    assert body == {"name": "Rex", "status": "available"}


def test_route_inputs_empty_input(cli_module):
    """Should handle empty input gracefully."""
    parameters = [
        {"name": "status", "in": "query"},
    ]

    path_params, query_params, header_params, body = cli_module._route_inputs({}, parameters, has_request_body=False)

    assert path_params == {}
    assert query_params == {}
    assert header_params == {}
    assert body is None


def test_route_inputs_no_body_when_not_declared(cli_module):
    """Undeclared keys without requestBody should still go to body dict."""
    parameters = []
    input_data = {"name": "Rex"}

    path_params, query_params, header_params, body = cli_module._route_inputs(
        input_data, parameters, has_request_body=False
    )

    # No requestBody and no param_map — body_keys has content but not the fallback path
    assert body == {"name": "Rex"}


def test_route_inputs_mixed_all_locations(cli_module):
    """Should correctly split input across path, query, header, and body."""
    parameters = [
        {"name": "userId", "in": "path"},
        {"name": "format", "in": "query"},
        {"name": "X-Trace-Id", "in": "header"},
    ]
    input_data = {
        "userId": 42,
        "format": "json",
        "X-Trace-Id": "trace-abc",
        "email": "user@example.com",
        "name": "Alice",
    }

    path_params, query_params, header_params, body = cli_module._route_inputs(
        input_data, parameters, has_request_body=True
    )

    assert path_params == {"userId": 42}
    assert query_params == {"format": "json"}
    assert header_params == {"X-Trace-Id": "trace-abc"}
    assert body == {"email": "user@example.com", "name": "Alice"}


def test_extract_full_endpoint_schema_has_parameters(cli_module, petstore_spec):
    """Should extract parameters with 'in' locations from petstore spec."""
    endpoint = cli_module.extract_full_endpoint_schema(petstore_spec, "findPetsByStatus")
    assert endpoint is not None
    assert endpoint["method"] == "GET"
    assert endpoint["path"] == "/pet/findByStatus"
    # findPetsByStatus has a 'status' query parameter
    param_names = [p["name"] for p in endpoint["parameters"]]
    assert "status" in param_names
    status_param = next(p for p in endpoint["parameters"] if p["name"] == "status")
    assert status_param["in"] == "query"


def test_extract_full_endpoint_schema_path_param(cli_module, petstore_spec):
    """Should extract path parameters from petstore spec."""
    endpoint = cli_module.extract_full_endpoint_schema(petstore_spec, "getPetById")
    assert endpoint is not None
    assert endpoint["method"] == "GET"
    assert "/pet/{petId}" == endpoint["path"]
    param_names = [p["name"] for p in endpoint["parameters"]]
    assert "petId" in param_names
    pet_id_param = next(p for p in endpoint["parameters"] if p["name"] == "petId")
    assert pet_id_param["in"] == "path"


def test_extract_full_endpoint_schema_with_request_body(cli_module, petstore_spec):
    """Should detect requestBody on POST endpoints."""
    endpoint = cli_module.extract_full_endpoint_schema(petstore_spec, "addPet")
    assert endpoint is not None
    assert endpoint["method"] == "POST"
    assert endpoint["requestBody"] is not None
