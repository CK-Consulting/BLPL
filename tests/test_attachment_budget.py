"""What a turn actually puts on the wire.

Every turn resends the whole conversation, which a stateless chat API requires.
Resending the same PDF twice in one request, or carrying a datasheet from
twenty turns ago that has already been read and written up, it does not.

The measurement that prompted this, from a real conversation: 57k tokens of
text and 79.7 MB of base64 PDFs, of which 34 MB was three files included twice
over. Anthropic's request limit is 32 MB, so every turn failed — and the error
said "request too large" without saying what was large about it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import attachments as store
from app.chat import attachments_left_out, history_to_messages
from blpl.core.llm_chat import DocumentBlock, TextBlock


@pytest.fixture
def convo(tmp_path: Path) -> Path:
    (tmp_path / "attachments").mkdir()
    return tmp_path


def _put(convo: Path, name: str, mb: float) -> str:
    # Distinct bytes per file. The store is content-addressed, so two "different"
    # files with identical content are one attachment — which is correct, and
    # made the first draft of these tests assert against their own fixture.
    body = b"%PDF-1.4" + name.encode() + b"x" * int(mb * 1024 * 1024)
    return store.save(convo, name, body).id


def _msg(aid: str, name: str) -> dict:
    return {
        "role": "user",
        "content": "",
        "metadata": {
            "blocks": [
                {"type": "text", "text": "have a look"},
                {"type": "document", "attachment": aid, "name": name,
                 "media_type": "application/pdf"},
            ]
        },
    }


def _kinds(msgs) -> list[str]:
    return [type(b).__name__ for m in msgs for b in m.content]


def test_the_same_file_twice_is_sent_once(convo, monkeypatch) -> None:
    """Pure waste: the provider gains nothing from the second copy, and it is
    charged for as request body either way."""
    aid = _put(convo, "spec.pdf", 1)
    events = [_msg(aid, "spec.pdf"), _msg(aid, "spec.pdf")]
    msgs = history_to_messages(events, convo)
    assert _kinds(msgs).count("DocumentBlock") == 1
    # And it is the *later* one that carries the bytes, so the file sits nearest
    # the question being asked about it.
    assert isinstance(msgs[1].content[1], DocumentBlock)
    assert isinstance(msgs[0].content[1], TextBlock)


def test_an_old_attachment_is_dropped_but_named(convo, monkeypatch) -> None:
    monkeypatch.setattr("app.chat._ATTACHMENT_BUDGET", 2 * 1024 * 1024)
    old = _put(convo, "old.pdf", 1.4)
    new = _put(convo, "new.pdf", 1.4)
    events = [_msg(old, "old.pdf"), _msg(new, "new.pdf")]
    msgs = history_to_messages(events, convo)
    assert isinstance(msgs[1].content[1], DocumentBlock)   # newest kept
    stand_in = msgs[0].content[1]
    assert isinstance(stand_in, TextBlock)
    # Named, not silently absent. An assistant answering "as the datasheet
    # shows" about a datasheet it never received is the failure being avoided.
    assert "old.pdf" in stand_in.text and "not included again" in stand_in.text


def test_what_was_left_out_is_reported_not_only_substituted(convo, monkeypatch) -> None:
    monkeypatch.setattr("app.chat._ATTACHMENT_BUDGET", 2 * 1024 * 1024)
    a = _put(convo, "a.pdf", 1.4)
    b = _put(convo, "b.pdf", 1.4)
    events = [_msg(a, "a.pdf"), _msg(b, "b.pdf")]
    assert attachments_left_out(events, convo) == ["a.pdf"]


def test_the_budget_binds_the_message_just_sent(convo, monkeypatch) -> None:
    """Three big datasheets on one message was the actual case, and exempting
    the newest message from the budget meant that request was over the limit on
    every turn from then on."""
    monkeypatch.setattr("app.chat._ATTACHMENT_BUDGET", 3 * 1024 * 1024)
    ids = [_put(convo, f"d{i}.pdf", 1.6) for i in range(3)]
    events = [
        {
            "role": "user",
            "content": "",
            "metadata": {
                "blocks": [
                    {"type": "document", "attachment": i, "name": f"d{n}.pdf",
                     "media_type": "application/pdf"}
                    for n, i in enumerate(ids)
                ]
            },
        }
    ]
    msgs = history_to_messages(events, convo)
    # The first attached fits; the two behind it do not, and are named.
    assert _kinds(msgs).count("DocumentBlock") == 1
    assert sorted(attachments_left_out(events, convo)) == ["d1.pdf", "d2.pdf"]


def test_attaching_something_is_never_a_no_op(convo, monkeypatch) -> None:
    """Even a file over the whole budget goes: dropping everything would leave
    the user staring at a reply about a document that was never sent."""
    monkeypatch.setattr("app.chat._ATTACHMENT_BUDGET", 1024)
    aid = _put(convo, "huge.pdf", 2)
    msgs = history_to_messages([_msg(aid, "huge.pdf")], convo)
    assert _kinds(msgs).count("DocumentBlock") == 1


def test_a_conversation_within_budget_is_untouched(convo) -> None:
    a = _put(convo, "a.pdf", 0.5)
    b = _put(convo, "b.pdf", 0.5)
    events = [_msg(a, "a.pdf"), _msg(b, "b.pdf")]
    assert _kinds(history_to_messages(events, convo)).count("DocumentBlock") == 2
    assert attachments_left_out(events, convo) == []


def test_the_ui_still_gets_history_with_no_store(convo) -> None:
    """attachments_dir is None for callers that only want the shape of the
    history. That path must not start substituting text for images."""
    aid = _put(convo, "a.pdf", 0.1)
    msgs = history_to_messages([_msg(aid, "a.pdf")], None)
    assert _kinds(msgs) == ["TextBlock"]


def test_an_upload_that_could_never_be_sent_is_refused(convo) -> None:
    """base64 adds a third, and an attachment travels with the whole
    conversation on every later turn. A 32 MB PDF was accepted and then could
    not be sent — a rejection deferred to somewhere less obvious."""
    assert store.MAX_DOCUMENT_BYTES * 4 // 3 < 32 * 1024 * 1024
    assert store.MAX_IMAGE_BYTES * 4 // 3 < 32 * 1024 * 1024


def test_a_file_attached_twice_is_named_once_when_it_is_dropped(convo, monkeypatch) -> None:
    """"not sending X, X" reads as two problems. It is one file."""
    monkeypatch.setattr("app.chat._ATTACHMENT_BUDGET", 2 * 1024 * 1024)
    old = _put(convo, "old.pdf", 1.4)
    new = _put(convo, "new.pdf", 1.4)
    events = [_msg(old, "old.pdf"), _msg(old, "old.pdf"), _msg(new, "new.pdf")]
    assert attachments_left_out(events, convo) == ["old.pdf"]
