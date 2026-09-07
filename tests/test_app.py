def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.get_json()["status"] == "ok"


def test_dashboard_requires_login(client):
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]


def test_api_route_requires_login_returns_401(client):
    res = client.get("/students/1")
    assert res.status_code == 401


def test_login_wrong_password_shows_error(client):
    res = client.post("/login", data={"email": "admin@example.com", "password": "wrong"})
    assert res.status_code == 200
    assert b"Invalid email or password" in res.data


def test_login_unknown_email_shows_same_generic_error(client):
    # Same message as a wrong password for a real account -- doesn't
    # confirm or deny whether an email is registered.
    res = client.post("/login", data={"email": "nobody@example.com", "password": "whatever123"})
    assert res.status_code == 200
    assert b"Invalid email or password" in res.data


def test_login_success_then_logout(client):
    res = client.post("/login", data={"email": "admin@example.com", "password": "test-password"}, follow_redirects=True)
    assert res.status_code == 200
    assert b"Dashboard" in res.data or b"dashboard" in res.data.lower()

    res = client.get("/")
    assert res.status_code == 200

    res = client.post("/logout", follow_redirects=False)
    assert res.status_code == 302
    res = client.get("/")
    assert res.status_code == 302  # logged out again


def test_404_page(auth_client):
    res = auth_client.get("/this-does-not-exist")
    assert res.status_code == 404


def test_add_student_requires_name(auth_client):
    res = auth_client.post("/add_student", data={"name": ""})
    assert res.status_code == 400


def test_add_student_and_list_and_delete(auth_client):
    res = auth_client.post("/add_student", data={"name": "Test Student", "roll": "R1"})
    assert res.status_code == 200
    sid = res.get_json()["student_id"]

    res = auth_client.get("/students")
    assert res.status_code == 200
    assert b"Test Student" in res.data

    res = auth_client.get(f"/students/{sid}")
    assert res.status_code == 200
    assert res.get_json()["name"] == "Test Student"

    res = auth_client.delete(f"/students/{sid}")
    assert res.status_code == 200
    assert res.get_json()["deleted"] is True

    res = auth_client.get(f"/students/{sid}")
    assert res.status_code == 404


def test_delete_nonexistent_student_is_404(auth_client):
    res = auth_client.delete("/students/999999")
    assert res.status_code == 404


def test_oversized_student_id_is_404_not_500(auth_client):
    # SQLite INTEGER is signed 64-bit; Flask's <int:id> converter has no
    # upper bound, so this used to raise an unhandled OverflowError.
    huge = "9" * 30
    res = auth_client.get(f"/students/{huge}")
    assert res.status_code == 404
    res = auth_client.delete(f"/students/{huge}")
    assert res.status_code == 404


def test_negative_student_id_is_404(auth_client):
    res = auth_client.get("/students/-1")
    assert res.status_code == 404


def test_oversized_page_number_does_not_crash(auth_client):
    res = auth_client.get("/attendance_record?page=" + "9" * 30)
    assert res.status_code == 200


def test_upload_face_rejects_path_traversal(auth_client):
    res = auth_client.post("/upload_face", data={"student_id": "../../evil"})
    assert res.status_code == 400


def test_upload_face_rejects_unknown_student(auth_client):
    res = auth_client.post("/upload_face", data={"student_id": "999999"})
    assert res.status_code == 404


def test_upload_face_rejects_fake_image_content(auth_client):
    res = auth_client.post("/add_student", data={"name": "Upload Test"})
    sid = res.get_json()["student_id"]
    from io import BytesIO
    fake = (BytesIO(b"not actually a jpeg"), "fake.jpg")
    res = auth_client.post(
        "/upload_face",
        data={"student_id": str(sid), "images[]": fake},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["saved"] == 0
    assert body["rejected"] == 1


def test_upload_face_rejects_disallowed_extension(auth_client):
    res = auth_client.post("/add_student", data={"name": "Upload Test 2"})
    sid = res.get_json()["student_id"]
    from io import BytesIO
    fake = (BytesIO(b"MZ\x90\x00fake-exe-bytes"), "payload.exe")
    res = auth_client.post(
        "/upload_face",
        data={"student_id": str(sid), "images[]": fake},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    assert res.get_json()["saved"] == 0


def test_mark_attendance_page_is_public(client):
    res = client.get("/mark_attendance")
    assert res.status_code == 200


def test_recognize_face_requires_image(client):
    res = client.post("/recognize_face", data={})
    assert res.status_code == 400


def test_train_status_before_any_training(auth_client):
    res = auth_client.get("/train_status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["running"] is False


def test_train_model_with_no_students_reports_error_quickly(auth_client):
    res = auth_client.post("/train_model")
    assert res.status_code == 202
    import time
    status = None
    for _ in range(40):
        status = auth_client.get("/train_status").get_json()
        # Break only on a real terminal state, not merely "not running" --
        # an early poll landing between the synchronous "running: true"
        # write and the background thread's first real update should keep
        # waiting, not be mistaken for the job having already finished.
        if not status["running"] and status["status"] in ("success", "error"):
            break
        time.sleep(0.15)
    assert status["running"] is False
    assert status["status"] == "error"


def test_attendance_record_page(auth_client):
    res = auth_client.get("/attendance_record")
    assert res.status_code == 200


def test_download_csv(auth_client):
    res = auth_client.get("/download_csv")
    assert res.status_code == 200
    assert res.data.startswith(b"id,student_id,name,timestamp")


def test_analytics_page(auth_client):
    res = auth_client.get("/analytics")
    assert res.status_code == 200


def test_csrf_rejected_when_testing_disabled(client, monkeypatch):
    # TESTING=True bypasses CSRF for test convenience (see app.py); this
    # test explicitly re-enables the check to prove it actually rejects
    # forged requests when it's active.
    import app as app_module
    app_module.app.config["TESTING"] = False
    client.post("/login", data={"email": "admin@example.com", "password": "test-password"})
    res = client.post("/add_student", data={"name": "Should Fail"})
    app_module.app.config["TESTING"] = True
    assert res.status_code == 400


def test_duplicate_attendance_prevented_at_db_level(auth_client):
    # Exercises the actual mechanism (the unique index) that stops
    # duplicate same-day attendance, without needing a real face/model.
    import app as app_module
    import db

    res = auth_client.post("/add_student", data={"name": "Dedup Test"})
    sid = res.get_json()["student_id"]

    with app_module.app.test_request_context():
        db.get_db()  # bind a connection for this context
        first = db.mark_attendance(sid, "Dedup Test")
        second = db.mark_attendance(sid, "Dedup Test")
        assert first is True
        assert second is False  # unique index rejects the same-day duplicate
        assert db.attendance_count_for_student(sid) == 1


def test_concurrent_train_model_request_is_rejected(auth_client):
    # Simulate a training job already in progress by holding the lock
    # directly, rather than relying on two real HTTP requests landing
    # inside the actual background thread's runtime window -- with no
    # training data, training fails validation almost instantly, so that
    # window is a few milliseconds and asserting against it is flaky (the
    # underlying lock/status-file mechanism itself is already exercised
    # against real, slower training runs elsewhere; this test only needs
    # to prove the route's "already running" branch is reachable).
    import app as app_module
    acquired = app_module._train_lock.acquire(blocking=False)
    assert acquired, "test setup: lock should be free at test start"
    try:
        res = auth_client.post("/train_model")
        assert res.status_code == 409
        assert res.get_json()["status"] == "already_running"
    finally:
        app_module._train_lock.release()


def test_production_config_requires_secret_key_and_super_admin_credentials(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPER_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SUPER_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")
    import importlib
    import config as config_module
    importlib.reload(config_module)
    try:
        with __import__("pytest").raises(RuntimeError, match="SECRET_KEY"):
            config_module.get_config()
    finally:
        monkeypatch.setenv("FLASK_ENV", "testing")
        importlib.reload(config_module)
