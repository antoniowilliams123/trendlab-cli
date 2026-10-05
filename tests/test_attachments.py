import base64
import json
from pathlib import Path

import httpx
import pytest

from trendlab.context.manager import message_tokens
from trendlab.providers.anthropic_provider import translate_messages
from trendlab.providers.openai_compatible import OpenAICompatibleProvider
from trendlab.ui.attachments import (
    build_user_content,
    encode_image,
    find_image_paths,
    image_count,
    text_of,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _img(project: Path, name="shot.png") -> Path:
    p = project / name
    p.write_bytes(PNG)
    return p


def test_find_and_build_parts(project: Path):
    shot = _img(project)
    (project / "docs").mkdir()
    deep = _img(project, "docs/layout.jpg")
    refs = find_image_paths("look at @shot.png and docs/layout.jpg, also missing.png", project)
    assert [p for _, p in refs] == [shot.resolve(), deep.resolve()]
    content = build_user_content("why is @shot.png broken?", project)
    assert content[0] == {"type": "image_path", "path": str(shot.resolve())}
    assert content[1] == {"type": "text", "text": "why is shot.png broken?"}
    assert build_user_content("no images here", project) == "no images here"
    extra = build_user_content("", project, [shot])
    assert extra[-1]["text"] == "(see attached image)" and image_count(extra) == 1
    assert (
        text_of(content) == "[image: shot.png]\nwhy is shot.png broken?"
        and text_of("plain") == "plain"
    )
    media, data = encode_image(shot)
    assert media == "image/png" and base64.b64decode(data) == PNG
    with pytest.raises(ValueError, match="not found"):
        encode_image(project / "nope.png")
    assert message_tokens({"role": "user", "content": content}) > 1200


def test_anthropic_translation_of_image_parts(project: Path):
    shot = _img(project)
    content = build_user_content("fix @shot.png", project)
    _, msgs = translate_messages([{"role": "user", "content": content}], "claude-opus-5")
    parts = msgs[0]["content"]
    assert parts[0]["type"] == "image" and parts[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(parts[0]["source"]["data"]) == PNG and parts[1] == {
        "type": "text",
        "text": "fix shot.png",
    }
    shot.unlink()
    _, msgs = translate_messages([{"role": "user", "content": content}], "claude-opus-5")
    assert (
        msgs[0]["content"][0]["type"] == "text"
        and "image unavailable" in msgs[0]["content"][0]["text"]
    )


async def test_openai_translation_of_image_parts(project: Path, monkeypatch):
    monkeypatch.setenv("K", "k")
    _img(project)
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    p = OpenAICompatibleProvider(
        base_url="https://x/v1",
        model="deepseek-v4-pro",
        api_key_env="K",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await p.complete(
        [{"role": "user", "content": build_user_content("what is @shot.png", project)}]
    )
    parts = seen["body"]["messages"][0]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert parts[1] == {"type": "text", "text": "what is shot.png"}


async def test_app_attaches_images_and_exports(project: Path, _trendlab_home: Path):
    from rich.console import Console

    from trendlab.app import TrendLabApp
    from trendlab.config.loader import load_config
    from trendlab.providers.base import ModelResponse
    from trendlab.providers.scripted import ScriptedProvider
    from trendlab.telemetry.events import EventRecorder, EventType

    shot = _img(project)
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    provider = ScriptedProvider([ModelResponse(text="I see a tiny image.")])
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        console=Console(record=True, width=100),
    )
    rec = EventRecorder()
    tl.events.subscribe(rec)
    await tl.start(interactive=False)
    try:
        res = await tl.run_prompt("describe @shot.png please")
        assert res.status == "COMPLETED"
        sent = provider.calls[0][-1]["content"]
        assert sent[0]["type"] == "image_path" and sent[1]["text"] == "describe shot.png please"
        assert rec.of_type(EventType.IMAGES_ATTACHED)[0].data["images"] == ["shot.png"]
        text = tl.export_transcript().read_text()
        assert "[image: shot.png]" in text
        assert tl.store.messages(tl.session_id)[0]["content"][0]["path"] == str(shot.resolve())
    finally:
        await tl.stop()
