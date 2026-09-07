import os
import sys
import importlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A Flask test client wired to an isolated, throwaway database/dataset
    directory so tests never touch the real project data.
    """
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "dataset"))
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "model.pkl"))
    monkeypatch.setenv("MODEL_META_PATH", str(tmp_path / "model_meta.json"))
    monkeypatch.setenv("TRAIN_STATUS_PATH", str(tmp_path / "train_status.json"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "app.log"))
    monkeypatch.setenv("SUPER_ADMIN_NAME", "Test Super Admin")
    monkeypatch.setenv("SUPER_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", "test-password")

    import config
    import db
    import storage
    import model
    import app as app_module

    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(storage)
    importlib.reload(model)
    importlib.reload(app_module)

    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def auth_client(client):
    """Logged in as the super_admin provisioned from SUPER_ADMIN_EMAIL/PASSWORD."""
    client.post("/login", data={"email": "admin@example.com", "password": "test-password"}, follow_redirects=True)
    return client


@pytest.fixture
def normal_user_client(client):
    """A separate, approved, active normal-user session (its own cookie
    jar), created via the real register -> [approve] -> login flow. The
    approval step calls db.set_user_approved() directly rather than going
    through /admin/users/<id>/approve -- dedicated tests exercise that
    endpoint itself; this fixture just needs to reach an approved state.
    """
    client.post("/register", data={
        "name": "Normal User", "email": "user@example.com",
        "password": "userpassword1", "confirm_password": "userpassword1",
    })
    import db as db_module
    with client.application.app_context():
        user = db_module.get_user_by_email("user@example.com")
        db_module.set_user_approved(user["id"], True)

    fresh = client.application.test_client()
    fresh.post("/login", data={"email": "user@example.com", "password": "userpassword1"}, follow_redirects=True)
    return fresh
