"""Regression tests for tracker #2: incognito chats must never be persisted.

The UI promises incognito turns are not saved, but every turn was written to
chat_messages: Session.add_message delegates to _persist_message
unconditionally, the disconnect partial-save paths persisted before checking
the flag, and auto-naming generated (and saved) a title derived from the
incognito content. Enforcement now lives at the session-manager choke points
— _persist_message and replace_messages skip any message whose metadata
carries ``incognito: True`` — with the chat helpers marking that flag.
"""
import asyncio
import tempfile
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import core.session_manager as csm
from core.models import ChatMessage
from core.database import ChatMessage as DbChatMessage, Session as DbSession

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(f"sqlite:///{_TMPDB.name}", connect_args={"check_same_thread": False}, poolclass=NullPool)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _manager(monkeypatch):
    monkeypatch.setattr(csm, "SessionLocal", _TS)
    db = _TS()
    try:
        db.query(DbChatMessage).delete()
        db.query(DbSession).delete()
        db.commit()
    finally:
        db.close()
    mgr = csm.SessionManager()
    mgr.create_session("s1", "test", "http://x/v1", "model-x")
    return mgr


def _rows():
    db = _TS()
    try:
        return db.query(DbChatMessage).filter(DbChatMessage.session_id == "s1").all()
    finally:
        db.close()


def test_persist_message_skips_incognito(monkeypatch):
    mgr = _manager(monkeypatch)
    mgr._persist_message("s1", ChatMessage("user", "secret question", metadata={"incognito": True}))
    mgr._persist_message("s1", ChatMessage("user", "normal question"))
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].content == "normal question"


def test_replace_messages_drops_incognito_rows_keeps_memory(monkeypatch):
    # Compaction rewrites the whole history through replace_messages — a
    # session that has had incognito turns must not get them bulk-persisted.
    mgr = _manager(monkeypatch)
    msgs = [
        ChatMessage("user", "kept a"),
        ChatMessage("assistant", "secret reply", metadata={"incognito": True}),
        ChatMessage("user", "kept b"),
    ]
    assert mgr.replace_messages("s1", msgs)
    rows = _rows()
    assert [r.content for r in rows] == ["kept a", "kept b"]
    # In-memory history keeps all three for conversation context.
    assert len(mgr.get_session("s1").history) == 3
    db = _TS()
    try:
        assert db.query(DbSession).filter(DbSession.id == "s1").first().message_count == 2
    finally:
        db.close()


def test_add_message_path_end_to_end(monkeypatch):
    # The real wiring: Session.add_message -> core.models._session_manager
    # -> _persist_message. An incognito-flagged message must not produce a row.
    import core.models as cmodels
    mgr = _manager(monkeypatch)
    monkeypatch.setattr(cmodels, "_session_manager", mgr)
    sess = mgr.get_session("s1")
    sess.add_message(ChatMessage("user", "ephemeral", metadata={"incognito": True}))
    sess.add_message(ChatMessage("assistant", "persisted"))
    assert [r.content for r in _rows()] == ["persisted"]
    assert len(sess.history) == 2  # both visible in-memory


def test_add_user_message_marks_incognito():
    import routes.chat_helpers as ch
    captured = []
    sess = SimpleNamespace(add_message=lambda m: captured.append(m))
    chat_handler = SimpleNamespace(
        update_session_name_if_needed=lambda *a: (_ for _ in ()).throw(AssertionError("must not rename")))
    pre = SimpleNamespace(user_content="hi", attachment_meta=None, text_for_context="hi")
    ch.add_user_message(sess, chat_handler, pre, incognito=True)
    assert captured[0].metadata.get("incognito") is True


def test_save_assistant_response_marks_incognito():
    import routes.chat_helpers as ch
    captured = []
    sess = SimpleNamespace(add_message=lambda m: captured.append(m), model="m", history=[])
    out = ch.save_assistant_response(
        sess, None, "s1", "the reply", None, incognito=True,
    )
    assert out is None  # no edit/delete handle for ephemeral messages
    assert captured[0].metadata.get("incognito") is True


def test_no_auto_name_for_incognito(monkeypatch):
    import routes.chat_helpers as ch
    named = []

    async def fake_auto_name(mgr, sess):
        named.append(sess)

    monkeypatch.setattr(ch, "needs_auto_name", lambda name: True)
    monkeypatch.setattr(ch, "auto_name_session", fake_auto_name)
    sess = SimpleNamespace(name="Chat", model="m", history=[])

    async def main():
        # compare_mode skips the webhook; extraction gates are all off.
        ch.run_post_response_tasks(
            sess, None, "s1", "secret msg", "secret reply", None, {},
            None, None, None,
            incognito=True, compare_mode=True,
            extract_skills=False, allow_background_extraction=False,
        )
        await asyncio.sleep(0)

    asyncio.run(main())
    assert named == []
