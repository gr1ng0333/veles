from unittest.mock import patch

from ouroboros.llm import LLMClient, normalize_requested_model


@patch.dict(
    'os.environ',
    {
        'OUROBOROS_MODEL_LIGHT': 'gpt-5.1-codex-mini',
        'CODEX_CONSCIOUSNESS_ACCESS': 'token',
    },
    clear=True,
)
def test_normalize_requested_model_routes_light_codex_to_consciousness_prefix():
    assert normalize_requested_model('gpt-5.1-codex-mini') == 'codex-consciousness/gpt-5.1-codex-mini'


@patch.dict(
    'os.environ',
    {
        'OUROBOROS_MODEL_LIGHT': 'gpt-5.1-codex-mini',
    },
    clear=True,
)
def test_normalize_requested_model_keeps_bare_codex_without_consciousness_tokens():
    assert normalize_requested_model('gpt-5.1-codex-mini') == 'gpt-5.1-codex-mini'


@patch.dict(
    'os.environ',
    {
        'OUROBOROS_MODEL_LIGHT': 'gpt-5.1-codex-mini',
        'CODEX_CONSCIOUSNESS_REFRESH': 'refresh-token',
    },
    clear=True,
)
@patch('ouroboros.codex_proxy.call_codex', return_value=({'content': 'ok'}, {'cost': 0}))
def test_chat_routes_bare_light_codex_via_consciousness_tokens(call_codex):
    client = LLMClient()
    msg, usage = client.chat(
        messages=[{'role': 'user', 'content': 'ping'}],
        model='gpt-5.1-codex-mini',
        max_tokens=32,
    )

    assert msg['content'] == 'ok'
    assert usage['cost'] == 0
    call_codex.assert_called_once()
    _, kwargs = call_codex.call_args
    assert kwargs['model'] == 'gpt-5.1-codex-mini'
    assert kwargs['token_prefix'] == 'CODEX_CONSCIOUSNESS'
