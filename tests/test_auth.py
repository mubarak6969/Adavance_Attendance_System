"""Tests for the multi-user account system: registration, approval,
roles, and server-side authorization. See test_app.py for the original
single-admin-era tests (still relevant to shared routes) and
test_database_backend.py for the Postgres-specific equivalents.
"""


def _register(client, name="New User", email="newuser@example.com", password="strongpassword1"):
    return client.post("/register", data={
        "name": name, "email": email, "password": password, "confirm_password": password,
    })


# ---------- Registration ----------

def test_registration_creates_pending_user(client):
    res = _register(client)
    assert res.status_code == 200
    assert b"pending administrator approval" in res.data.lower() or b"pending" in res.data.lower()

    import db
    with client.application.app_context():
        user = db.get_user_by_email("newuser@example.com")
        assert user is not None
        assert user["role"] == "user"
        assert user["is_approved"] in (0, False)
        assert user["is_active"] in (1, True)


def test_registration_rejects_duplicate_email(client):
    _register(client, email="dup@example.com")
    res = _register(client, name="Someone Else", email="dup@example.com")
    assert res.status_code == 200
    assert b"already exists" in res.data.lower()

    import db
    with client.application.app_context():
        # Only one row for that email, not two.
        rows = db.list_users()
        matching = [u for u in rows if u["email"] == "dup@example.com"]
        assert len(matching) == 1


def test_registration_rejects_mismatched_passwords(client):
    res = client.post("/register", data={
        "name": "X", "email": "mismatch@example.com",
        "password": "onepassword1", "confirm_password": "differentpassword2",
    })
    assert b"do not match" in res.data.lower()
    import db
    with client.application.app_context():
        assert db.get_user_by_email("mismatch@example.com") is None


def test_registration_rejects_short_password(client):
    res = client.post("/register", data={
        "name": "X", "email": "short@example.com",
        "password": "short", "confirm_password": "short",
    })
    assert b"8 characters" in res.data
    import db
    with client.application.app_context():
        assert db.get_user_by_email("short@example.com") is None


def test_registration_rejects_invalid_email(client):
    res = client.post("/register", data={
        "name": "X", "email": "not-an-email",
        "password": "validpassword1", "confirm_password": "validpassword1",
    })
    assert b"valid email" in res.data.lower()


def test_password_is_hashed_not_plaintext(client):
    _register(client, email="hashcheck@example.com", password="mypassword123")
    import db
    with client.application.app_context():
        user = db.get_user_by_email("hashcheck@example.com")
        assert "mypassword123" not in user["password_hash"]
        assert user["password_hash"].startswith(("pbkdf2:", "scrypt:"))  # werkzeug hash formats


# ---------- Login states ----------

def test_login_blocked_while_pending(client):
    _register(client, email="pending@example.com", password="pendingpassword1")
    res = client.post("/login", data={"email": "pending@example.com", "password": "pendingpassword1"})
    assert b"pending administrator approval" in res.data.lower()


def test_login_blocked_while_disabled(client):
    _register(client, email="disabledacct@example.com", password="disabledpassword1")
    import db
    with client.application.app_context():
        user = db.get_user_by_email("disabledacct@example.com")
        db.set_user_approved(user["id"], True)
        db.set_user_active(user["id"], False)
    res = client.post("/login", data={"email": "disabledacct@example.com", "password": "disabledpassword1"})
    assert b"disabled" in res.data.lower()


def test_login_succeeds_once_approved(client):
    _register(client, email="approveme@example.com", password="approvepassword1")
    import db
    with client.application.app_context():
        user = db.get_user_by_email("approveme@example.com")
        db.set_user_approved(user["id"], True)
    res = client.post(
        "/login", data={"email": "approveme@example.com", "password": "approvepassword1"}, follow_redirects=True
    )
    assert res.status_code == 200
    assert b"Dashboard" in res.data or b"dashboard" in res.data.lower()


def test_deactivating_user_ends_their_existing_session(client):
    """A session for a user who gets deactivated mid-session must stop
    working on the next request -- not just on next login.
    """
    _register(client, email="revoke@example.com", password="revokepassword1")
    import db
    with client.application.app_context():
        user = db.get_user_by_email("revoke@example.com")
        db.set_user_approved(user["id"], True)
    client.post("/login", data={"email": "revoke@example.com", "password": "revokepassword1"})
    assert client.get("/").status_code == 200

    with client.application.app_context():
        db.set_user_active(user["id"], False)

    res = client.get("/", follow_redirects=False)
    assert res.status_code == 302  # session was cleared, back to login


# ---------- Roles & authorization ----------

def test_super_admin_can_reach_admin_page(auth_client):
    res = auth_client.get("/admin")
    assert res.status_code == 200


def test_normal_user_cannot_reach_admin_page(normal_user_client):
    res = normal_user_client.get("/admin")
    assert res.status_code == 403


def test_normal_user_cannot_call_admin_apis(normal_user_client):
    res = normal_user_client.post("/admin/users/1/approve")
    assert res.status_code == 403
    res = normal_user_client.post("/admin/users/1/deactivate")
    assert res.status_code == 403
    res = normal_user_client.delete("/admin/users/1")
    assert res.status_code == 403


def test_unauthenticated_cannot_call_admin_apis(client):
    res = client.post("/admin/users/1/approve")
    assert res.status_code == 401
    res = client.get("/admin", follow_redirects=False)
    assert res.status_code == 302


def test_normal_user_can_add_student_but_not_delete(normal_user_client):
    res = normal_user_client.post("/add_student", data={"name": "Shared Workflow Student"})
    assert res.status_code == 200
    sid = res.get_json()["student_id"]

    res = normal_user_client.delete(f"/students/{sid}")
    assert res.status_code == 403


def test_normal_user_cannot_train_model(normal_user_client):
    res = normal_user_client.post("/train_model")
    assert res.status_code == 403


def test_normal_user_can_view_dashboard_and_attendance(normal_user_client):
    assert normal_user_client.get("/").status_code == 200
    assert normal_user_client.get("/students").status_code == 200
    assert normal_user_client.get("/attendance_record").status_code == 200
    assert normal_user_client.get("/analytics").status_code == 200


def test_role_cannot_be_forged_via_request_data(normal_user_client):
    """Even if a client sends role=super_admin in the request, authorization
    is decided by the authenticated database user's role, never by
    anything in the request itself.
    """
    res = normal_user_client.post(
        "/train_model", data={"role": "super_admin"}, headers={"X-Fake-Role": "super_admin"}
    )
    assert res.status_code == 403


# ---------- Super admin approval/role-management endpoints ----------

def test_super_admin_can_approve_and_deactivate_user(auth_client):
    import db
    with auth_client.application.app_context():
        uid = db.create_user("Approvable", "approvable@example.com", "x", role="user", is_approved=False)

    res = auth_client.post(f"/admin/users/{uid}/approve")
    assert res.status_code == 200
    with auth_client.application.app_context():
        assert db.get_user(uid)["is_approved"] in (1, True)

    res = auth_client.post(f"/admin/users/{uid}/deactivate")
    assert res.status_code == 200
    with auth_client.application.app_context():
        assert db.get_user(uid)["is_active"] in (0, False)


def test_cannot_deactivate_the_last_super_admin(auth_client):
    import db
    with auth_client.application.app_context():
        admin = db.get_user_by_email("admin@example.com")
    res = auth_client.post(f"/admin/users/{admin['id']}/deactivate")
    assert res.status_code == 400
    with auth_client.application.app_context():
        assert db.get_user(admin["id"])["is_active"] in (1, True)


def test_cannot_delete_the_last_super_admin(auth_client):
    import db
    with auth_client.application.app_context():
        admin = db.get_user_by_email("admin@example.com")
    res = auth_client.delete(f"/admin/users/{admin['id']}")
    assert res.status_code == 400
    with auth_client.application.app_context():
        assert db.get_user(admin["id"]) is not None


def test_cannot_demote_the_last_super_admin(auth_client):
    import db
    with auth_client.application.app_context():
        admin = db.get_user_by_email("admin@example.com")
    res = auth_client.post(f"/admin/users/{admin['id']}/role", data={"role": "user"})
    assert res.status_code == 400
    with auth_client.application.app_context():
        assert db.get_user(admin["id"])["role"] == "super_admin"


def test_can_deactivate_a_super_admin_when_another_remains_active(auth_client):
    import db
    with auth_client.application.app_context():
        second_admin_id = db.create_user(
            "Second Admin", "second-admin@example.com", "x", role="super_admin",
            is_approved=True, is_active=True,
        )
    res = auth_client.post(f"/admin/users/{second_admin_id}/deactivate")
    assert res.status_code == 200


def test_admin_delete_user_requires_csrf(client, monkeypatch):
    import app as app_module
    _register(client, email="csrfcheck@example.com")
    app_module.app.config["TESTING"] = False
    client.post("/login", data={"email": "admin@example.com", "password": "test-password"})
    import db
    with client.application.app_context():
        uid = db.get_user_by_email("csrfcheck@example.com")["id"]
    res = client.delete(f"/admin/users/{uid}")
    app_module.app.config["TESTING"] = True
    assert res.status_code == 400


# ---------- Super admin provisioning ----------

def test_super_admin_is_provisioned_on_startup(client):
    import db
    with client.application.app_context():
        admin = db.get_user_by_email("admin@example.com")
        assert admin is not None
        assert admin["role"] == "super_admin"
        assert admin["is_approved"] in (1, True)
        assert admin["is_active"] in (1, True)


def test_ensure_super_admin_never_overwrites_existing_password(client):
    import db
    from werkzeug.security import generate_password_hash, check_password_hash

    with client.application.app_context():
        admin = db.get_user_by_email("admin@example.com")
        original_hash = admin["password_hash"]
        # Re-run provisioning as create_app() does on every startup, with a
        # DIFFERENT password -- must be a no-op since the account exists.
        db.ensure_super_admin("Test Super Admin", "admin@example.com", generate_password_hash("a-different-password"))
        admin_after = db.get_user_by_email("admin@example.com")
        assert admin_after["password_hash"] == original_hash
        assert check_password_hash(admin_after["password_hash"], "test-password")


# ---------- Empty state ----------

def test_dashboard_renders_cleanly_with_zero_students(auth_client):
    res = auth_client.get("/")
    assert res.status_code == 200
    assert b"NaN" not in res.data
    assert b"undefined" not in res.data
    assert b"0%" in res.data or b"attendance_rate" not in res.data.lower()
