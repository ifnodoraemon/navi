from unittest.mock import Mock, patch
from navi.provider import OpenAICompatibleProvider

@patch("navi.provider.resolve_model_config")
def test_openai_provider_init(mock_resolve):
    mock_config = Mock()
    mock_spec = Mock()
    
    mock_resolve.return_value = mock_config
    
    provider = OpenAICompatibleProvider(mock_config, mock_spec)
    
    assert provider.spec == mock_spec
    mock_resolve.assert_called_once_with(mock_config)
