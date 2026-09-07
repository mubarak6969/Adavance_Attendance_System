"""Integration tests for the PostgreSQL storage backend (production path,
used when DATABASE_URL is set -- see storage.py and db.py).

These only run when a real, throwaway Postgres database is available: set
TEST_DATABASE_URL to point at one before running pytest. They're skipped
otherwise so `pytest tests/` still passes with nothing but the local SQLite
workflow, per the project's "don't require cloud services for local dev"
design.

Never point this at a database with real data -- it creates and drops
tables freely.
"""
import os

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set -- skipping Postgres-backed integration tests",
)


@pytest.fixture
def pg_config():
    """A Flask app context pushed for the test's duration, configured to
    point at the throwaway test database -- storage.py's database backend
    goes through db.get_db(), which needs current_app/g, exactly like it
    would inside a real request or the training background thread's
    `with app.app_context():` block (see app.py's _train_worker).
    """
    import db
    from flask import Flask

    db.init_db(None, database_url=TEST_DATABASE_URL)
    conn = db._connect(None, database_url=TEST_DATABASE_URL)
    # Start each test from a clean slate.
    conn.execute("DELETE FROM face_images")
    conn.execute("DELETE FROM attendance")
    conn.execute("DELETE FROM students")
    conn.execute("DELETE FROM model_artifacts")
    conn.commit()
    conn.close()

    test_app = Flask(__name__)
    test_app.config["DATABASE_URL"] = TEST_DATABASE_URL
    test_app.config["DATABASE_PATH"] = None
    with test_app.app_context():
        yield {"DATABASE_URL": TEST_DATABASE_URL}


@pytest.fixture
def pg_conn():
    import db

    conn = db._connect(None, database_url=TEST_DATABASE_URL)
    yield conn
    conn.close()


def _make_student(conn, name="Test Student"):
    import datetime
    cur = conn.execute(
        "INSERT INTO students (name, roll, class, section, reg_no, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
        (name, "R1", "C1", "S1", "REG1", datetime.datetime.utcnow().isoformat()),
    )
    sid = cur.fetchone()["id"]
    conn.commit()
    return sid


def test_schema_created_and_reachable(pg_config, pg_conn):
    row = pg_conn.execute("SELECT COUNT(*) AS c FROM students").fetchone()
    assert row["c"] == 0


def test_save_load_delete_face_images(pg_config, pg_conn):
    import storage

    sid = _make_student(pg_conn)
    assert storage.list_face_images(pg_config, sid) == []

    storage.save_face_image(pg_config, sid, "a.jpg", b"bytes-1")
    storage.save_face_image(pg_config, sid, "b.jpg", b"bytes-2")

    assert storage.count_face_images(pg_config, sid) == 2
    assert sorted(storage.list_face_images(pg_config, sid)) == sorted([b"bytes-1", b"bytes-2"])
    assert storage.student_ids_with_images(pg_config) == [sid]

    storage.delete_face_images(pg_config, sid)
    assert storage.count_face_images(pg_config, sid) == 0


def test_face_images_cascade_deleted_with_student(pg_config, pg_conn):
    sid = _make_student(pg_conn)
    pg_conn.execute(
        "INSERT INTO face_images (student_id, filename, data, created_at) VALUES (?, ?, ?, ?)",
        (sid, "a.jpg", b"bytes", "now"),
    )
    pg_conn.commit()
    pg_conn.execute("DELETE FROM students WHERE id=?", (sid,))
    pg_conn.commit()
    row = pg_conn.execute("SELECT COUNT(*) AS c FROM face_images WHERE student_id=?", (sid,)).fetchone()
    assert row["c"] == 0


def test_save_and_load_model(pg_config):
    import storage

    assert storage.load_model_bytes(pg_config) is None
    storage.save_model(pg_config, b"fake-model-bytes", {"num_students": 3})
    assert storage.load_model_bytes(pg_config) == b"fake-model-bytes"
    assert storage.load_model_meta(pg_config)["num_students"] == 3

    # Retraining overwrites in place (upsert), not a second row.
    storage.save_model(pg_config, b"fake-model-v2", {"num_students": 4})
    assert storage.load_model_bytes(pg_config) == b"fake-model-v2"
    assert storage.load_model_meta(pg_config)["num_students"] == 4


def test_duplicate_attendance_rejected(pg_config, pg_conn):
    import db

    sid = _make_student(pg_conn)
    # db.mark_attendance() itself requires a Flask app context (it goes
    # through get_db()); exercise the same unique-index guarantee directly
    # through the raw connection this fixture already provides instead.
    import datetime
    ts = datetime.datetime.utcnow().isoformat()
    pg_conn.execute(
        "INSERT INTO attendance (student_id, name, timestamp) VALUES (?, ?, ?)", (sid, "T", ts)
    )
    pg_conn.commit()
    with pytest.raises(db.INTEGRITY_ERRORS):
        pg_conn.execute(
            "INSERT INTO attendance (student_id, name, timestamp) VALUES (?, ?, ?)", (sid, "T", ts)
        )
        pg_conn.commit()
    pg_conn.rollback()


def test_end_to_end_student_and_training_lifecycle(pg_config, pg_conn):
    """Create a student, store face images, and confirm they're queryable
    for training the way _train_worker actually uses storage.py -- without
    needing a real face detector/model, which is covered elsewhere.
    """
    import storage

    sid = _make_student(pg_conn, name="Lifecycle Student")
    for i in range(3):
        storage.save_face_image(pg_config, sid, f"img{i}.jpg", f"bytes-{i}".encode())

    student_ids = storage.student_ids_with_images(pg_config)
    assert sid in student_ids
    image_data_by_student = {s: storage.list_face_images(pg_config, s) for s in student_ids}
    assert len(image_data_by_student[sid]) == 3

    storage.save_model(pg_config, b"trained-model", {
        "trained_at": "now", "num_students": 1, "trained_student_ids": [sid],
    })
    meta = storage.load_model_meta(pg_config)
    assert sid in meta["trained_student_ids"]

    # Cleanup: deleting the student should cascade-delete their face_images
    # (see test_face_images_cascade_deleted_with_student for the dedicated
    # assertion) and leave student_ids_with_images() no longer listing them.
    pg_conn.execute("DELETE FROM students WHERE id=?", (sid,))
    pg_conn.commit()
    assert sid not in storage.student_ids_with_images(pg_config)
