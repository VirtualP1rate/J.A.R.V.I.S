"""CalDAV sync uid handling: never hijack another user's event via a shared
VEVENT uid, but never abort the whole calendar's sync over one either.

CalendarEvent.uid is the global primary key. The first fix scoped the lookup
to the syncing calendar so user B's sync could not steal user A's row — but a
scoped miss with the uid present elsewhere then fell through to an INSERT
that failed the PK constraint and aborted the entire calendar's batch on
every cycle (tracker #48). _find_existing_event now resolves a scoped miss
globally: adopt a same-owner row (normal move-between-own-calendars
semantics), flag a cross-owner row so the caller skips just that VEVENT.
"""
import tempfile
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarEvent, CalendarCal
from src.caldav_sync import _find_existing_event

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(f"sqlite:///{_TMPDB.name}", connect_args={"check_same_thread": False}, poolclass=NullPool)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _setup():
    db = _TS()
    try:
        db.query(CalendarEvent).delete(); db.query(CalendarCal).delete()
        db.add(CalendarCal(id="calA", owner="alice", name="A"))
        db.add(CalendarCal(id="calA2", owner="alice", name="A2"))
        db.add(CalendarCal(id="calB", owner="bob", name="B"))
        # dtstart/dtend are NOT NULL in the schema, so seed valid values.
        db.add(CalendarEvent(
            uid="shared@svc", calendar_id="calA", summary="Alice event",
            dtstart=datetime(2026, 6, 4, 9, 0), dtend=datetime(2026, 6, 4, 10, 0),
        ))
        db.commit()
    finally:
        db.close()


def _cal(db, cal_id):
    return db.query(CalendarCal).filter(CalendarCal.id == cal_id).first()


def test_cross_owner_uid_reports_conflict_not_row():
    _setup()
    db = _TS()
    try:
        # Bob's calendar syncing the same uid must NOT resolve Alice's row —
        # and must learn it's a conflict so the sync skips this VEVENT
        # instead of inserting into a guaranteed PK failure.
        row, conflict = _find_existing_event(db, {}, "shared@svc", _cal(db, "calB"))
        assert row is None
        assert conflict is True
        # Same calendar still resolves its own event (normal update path).
        own, conflict = _find_existing_event(db, {}, "shared@svc", _cal(db, "calA"))
        assert own is not None and own.calendar_id == "calA"
        assert conflict is False
    finally:
        db.close()


def test_same_owner_row_is_adopted():
    _setup()
    db = _TS()
    try:
        # The same VEVENT under a second calendar of the SAME owner is the
        # event moving/being shared between alice's own calendars — adopt it
        # (the caller reassigns calendar_id) instead of failing the insert.
        row, conflict = _find_existing_event(db, {}, "shared@svc", _cal(db, "calA2"))
        assert conflict is False
        assert row is not None and row.uid == "shared@svc"
    finally:
        db.close()


def test_alice_event_is_not_moved():
    _setup()
    db = _TS()
    try:
        # Simulate the sync deciding there is no usable row for calB.
        row, conflict = _find_existing_event(db, {}, "shared@svc", _cal(db, "calB"))
        assert row is None and conflict is True
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == "shared@svc").first()
        assert ev.calendar_id == "calA"  # unchanged — not hijacked
    finally:
        db.close()


def test_unknown_uid_is_plain_miss():
    _setup()
    db = _TS()
    try:
        row, conflict = _find_existing_event(db, {}, "brand-new@svc", _cal(db, "calB"))
        assert row is None and conflict is False
    finally:
        db.close()


def test_pending_takes_precedence():
    _setup()
    db = _TS()
    try:
        sentinel = object()
        row, conflict = _find_existing_event(db, {"shared@svc": sentinel}, "shared@svc", _cal(db, "calB"))
        assert row is sentinel and conflict is False
    finally:
        db.close()
