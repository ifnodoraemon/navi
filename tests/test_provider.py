from unittest.mock import Mock, patch

import pytest

from navi.provider import (
    ChatMessage,
    OpenAICompatibleProvider,
    _messages_for_response_format,
    _validate_structured_output,
)

@patch("navi.provider.resolve_model_config")
def test_openai_provider_init(mock_resolve):
    mock_config = Mock()
    mock_spec = Mock()
    
    mock_resolve.return_value = mock_config
    
    provider = OpenAICompatibleProvider(mock_config, mock_spec)
    
    assert provider.spec == mock_spec
    mock_resolve.assert_called_once_with(mock_config)


def test_structured_output_validation_checks_json_schema_types():
    output_schema = {
        "name": "planner_decision",
        "schema": {
            "type": "object",
            "required": ["tool", "args"],
            "properties": {
                "tool": {"type": "string"},
                "args": {
                    "type": "object",
                    "required": ["limit"],
                    "properties": {"limit": {"type": "integer"}},
                },
            },
        },
    }

    _validate_structured_output('{"tool":"search","args":{"limit":3}}', output_schema)

    with pytest.raises(RuntimeError, match=r"structured output schema mismatch"):
        _validate_structured_output('{"tool":"search","args":{"limit":"3"}}', output_schema)


def test_structured_output_validation_checks_array_items():
    output_schema = {
        "schema": {
            "type": "object",
            "required": ["tool_calls"],
            "properties": {
                "tool_calls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["tool"],
                        "properties": {"tool": {"type": "string"}},
                    },
                }
            },
        },
    }

    _validate_structured_output('{"tool_calls":[{"tool":"git.status"}]}', output_schema)

    with pytest.raises(RuntimeError, match=r"tool_calls"):
        _validate_structured_output('{"tool_calls":[{"tool":404}]}', output_schema)


def test_json_object_mode_does_not_inject_schema_into_prompt():
    messages = [ChatMessage("user", "hi")]
    outbound = _messages_for_response_format(messages, {"type": "json_object"})

    assert outbound[0].role == "system"
    assert "JSON mode is enabled" in outbound[0].content
    assert "JSON Schema" not in outbound[0].content
    assert "strictly matches" not in outbound[0].content
    assert outbound[1:] == messages
