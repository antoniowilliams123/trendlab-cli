from trendlab.config.schema import PermissionMode
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ToolCall
from trendlab.security.scan import find_secrets
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime


def test_find_secrets_shapes_and_placeholders():
    text = (
        'OPENAI_API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\n'
        "token = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        'password = "hunter2hunter2hunter2"\n'
        'API_KEY = "<your-api-key>"\n'
        'API_KEY = os.environ["API_KEY"]\n'
        "aws = AKIAABCDEFGHIJKLMNOP\n"
        "normal = 1\n"
    )
    found = find_secrets(text)
    assert [f.split(": ")[1] for f in found] == [
        "OpenAI/Anthropic-style key",
        "GitHub token",
        "private key block",
        "hard-coded secret assignment",
        "AWS access key",
    ]
    assert find_secrets("x = 1\nAPI_KEY = '...'\n") == []


async def test_runtime_blocks_secret_writes(project, manager_factory, events, recorder):
    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id),
    )
    res = await rt.execute(
        ToolCall(
            id="1",
            name="write_file",
            arguments={
                "path": "settings.py",
                "content": 'KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\n',
            },
        )
    )
    assert (
        not res.ok and res.output.startswith("BLOCKED") and not (project / "settings.py").exists()
    )
    ev = recorder.of_type(EventType.SECRET_WRITE_BLOCKED)[0].data
    assert ev["files"] == ["settings.py"] and "sk-" not in str(ev)
    res = await rt.execute(
        ToolCall(
            id="2",
            name="patch_file",
            arguments={
                "path": "src/app.py",
                "old_text": "TIMEOUT = 30",
                "new_text": 'TIMEOUT = 30\nTOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"',
            },
        )
    )
    assert not res.ok and "GitHub token" in res.output
    res = await rt.execute(
        ToolCall(
            id="3",
            name="write_file",
            arguments={
                "path": "settings.py",
                "content": 'import os\nKEY = os.environ["OPENAI_API_KEY"]\n',
            },
        )
    )
    assert res.ok
