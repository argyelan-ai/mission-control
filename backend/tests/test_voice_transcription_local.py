"""A local STT endpoint can replace the OpenAI cloud — one setting, no code.

The operator's voice notes are transcribed by the shared chain in
``voice_transcription``. By default that chain talks to the OpenAI cloud with
his API key. With ``STT_BASE_URL`` set it talks to an OpenAI-compatible server
on his own machine (Parakeet v3 behind a FastAPI wrapper on the Mac Mini) —
same protocol, so the switch is a URL, not a second code path.

The subtle requirement these tests pin: a LOCAL endpoint must work WITHOUT an
OpenAI key. The old guard (``no key -> no ears``) was correct for the cloud
and wrong for local — an operator who runs local STT precisely because he does
not want a cloud dependency must not be forced to configure a cloud key.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.services.voice_transcription import get_voice_transcriber


@pytest.fixture(autouse=True)
def _reset_settings():
    """Each test states its own STT world; none may leak into the next."""
    before = (settings.openai_api_key, settings.stt_base_url, settings.stt_model)
    yield
    settings.openai_api_key, settings.stt_base_url, settings.stt_model = before


def _capture():
    return patch(
        "jarvis_core.brain.transcribe_audio", new_callable=AsyncMock,
        return_value="transkribiert",
    )


@pytest.mark.asyncio
async def test_local_endpoint_works_without_an_openai_key():
    """The whole point of going local: no cloud account required."""
    settings.openai_api_key = ""
    settings.stt_base_url = "http://host.docker.internal:8585/v1"
    settings.stt_model = ""

    transcriber = get_voice_transcriber()
    assert transcriber is not None, (
        "a configured local endpoint must give the channel ears — "
        "requiring a cloud key here defeats the purpose of local STT"
    )

    with _capture() as call:
        result = await transcriber(b"audio", filename="voice.m4a")

    assert result == "transkribiert"
    kwargs = call.await_args.kwargs
    assert kwargs["base_url"] == "http://host.docker.internal:8585/v1"


@pytest.mark.asyncio
async def test_cloud_stays_the_default_when_no_local_url_is_set():
    settings.openai_api_key = "sk-test"
    settings.stt_base_url = ""

    transcriber = get_voice_transcriber()
    assert transcriber is not None

    with _capture() as call:
        await transcriber(b"audio")

    kwargs = call.await_args.kwargs
    assert kwargs["api_key"] == "sk-test"
    assert "base_url" not in kwargs or "openai.com" in str(kwargs.get("base_url")), (
        "without STT_BASE_URL the chain must keep talking to the cloud default"
    )


def test_no_key_and_no_local_url_still_means_no_ears():
    """The old guard holds when NOTHING is configured."""
    settings.openai_api_key = ""
    settings.stt_base_url = ""
    assert get_voice_transcriber() is None


@pytest.mark.asyncio
async def test_stt_model_overrides_the_cloud_model_name():
    """A local server serves its own model; the cloud model name would be
    meaningless (or worse, rejected) there."""
    settings.openai_api_key = ""
    settings.stt_base_url = "http://host.docker.internal:8585/v1"
    settings.stt_model = "parakeet-tdt-0.6b-v3"

    transcriber = get_voice_transcriber()
    with _capture() as call:
        await transcriber(b"audio")

    assert call.await_args.kwargs["model"] == "parakeet-tdt-0.6b-v3"


@pytest.mark.asyncio
async def test_local_endpoint_wins_over_a_configured_cloud_key():
    """Both configured -> local wins. The operator set the URL deliberately;
    silently preferring the cloud would send his voice off-device again."""
    settings.openai_api_key = "sk-test"
    settings.stt_base_url = "http://host.docker.internal:8585/v1"

    transcriber = get_voice_transcriber()
    with _capture() as call:
        await transcriber(b"audio")

    assert call.await_args.kwargs["base_url"] == "http://host.docker.internal:8585/v1"
