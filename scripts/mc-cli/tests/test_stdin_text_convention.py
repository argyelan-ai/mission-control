"""One stdin convention across every verb that takes prose.

Live incident 2026-07-30: an agent answered the operator with a heredoc —

    mc msg - << 'EOF'
    <a long, carefully written reply>
    EOF

— and MC stored a message whose entire body was "-". The API answered 201 with
a message id, so from the agent's side it looked like it had spoken. The
operator saw a bare dash and asked why.

The agent had `mc msg --help` open and still trusted the convention, because it
had watched `mc telegram -` work. `mc telegram` read stdin, `mc msg` and
`mc ask` took the dash literally. An inconsistent CLI earns exactly that
mistake, which is why these tests pin the convention itself, verb by verb,
rather than one handler.
"""
import io
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402

LONG = "**Antwort:** Es gibt keine solche Regel.\nZweite Zeile mit Umlauten: schön, für, größer."


class _Stdin(io.StringIO):
    """A piped stdin: not a terminal, so the verbs must read from it."""

    def isatty(self):
        return False


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _client():
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, **kw):
        client.calls.append({"method": method, "path": path, "body": body})
        return {"message_id": "00000000-0000-0000-0000-000000000000"}

    client.request.side_effect = request
    return client


class _MsgArgs:
    text = "-"
    type = "message"
    thread = None


class _AskArgs:
    question = "-"
    options = None
    blocking = False
    to = None
    priority = "medium"
    default = None
    deadline = None


@pytest.mark.parametrize(
    "verb,args,handler,field",
    [
        ("msg", _MsgArgs, lambda: commands._cmd_msg, "body"),
        ("ask", _AskArgs, lambda: commands._cmd_ask, "question"),
    ],
)
def test_dash_reads_the_piped_text(monkeypatch, verb, args, handler, field):
    """The heredoc case that was silently swallowed."""
    monkeypatch.setattr(sys, "stdin", _Stdin(LONG))
    client = _client()

    handler()(args(), client, MagicMock())

    sent = client.calls[0]["body"][field]
    assert sent == LONG, (
        f"`mc {verb} -` did not read stdin — it sent {sent!r}. That is the live "
        "bug: a carefully written reply is replaced by a dash, and the 201 "
        "response makes it look like it worked."
    )
    assert sent != "-"


@pytest.mark.parametrize(
    "verb,args,handler",
    [
        ("msg", _MsgArgs, lambda: commands._cmd_msg),
        ("ask", _AskArgs, lambda: commands._cmd_ask),
    ],
)
def test_a_lone_dash_with_no_pipe_is_refused(monkeypatch, verb, args, handler):
    """Never post an empty message: that would be the same bug, quieter."""
    monkeypatch.setattr(sys, "stdin", _Tty(""))
    client = _client()

    with pytest.raises(UsageError):
        handler()(args(), client, MagicMock())

    assert client.calls == [], "nothing may be sent when there is no text"


def test_an_ordinary_argument_is_untouched(monkeypatch):
    """The common case must not change — including its umlauts."""
    monkeypatch.setattr(sys, "stdin", _Stdin("ignoriert"))
    client = _client()

    class A(_MsgArgs):
        text = "Kurze Antwort mit schönen Umlauten"

    commands._cmd_msg(A(), client, MagicMock())
    assert client.calls[0]["body"]["body"] == "Kurze Antwort mit schönen Umlauten"


def test_telegram_keeps_the_convention_it_already_had(monkeypatch):
    """Regression guard: the verb that got it right must keep getting it right."""
    monkeypatch.setattr(sys, "stdin", _Stdin(LONG))
    client = _client()

    class A:
        text = "-"
        photo = None
        file = None
        task_id = None

    commands._cmd_telegram(A(), client, MagicMock())
    assert client.calls[0]["body"]["text"] == LONG
