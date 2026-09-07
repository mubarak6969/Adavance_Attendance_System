"""Unit tests for the storage abstraction's local (filesystem) backend --
the one every environment can run without any external service. See
test_database_backend.py for the Postgres-backed counterpart, which only
runs when a test database is configured.
"""
import storage


def _local_config(tmp_path):
    return {
        "DATABASE_URL": None,
        "DATASET_DIR": str(tmp_path / "dataset"),
        "MODEL_PATH": str(tmp_path / "model.pkl"),
        "MODEL_META_PATH": str(tmp_path / "model_meta.json"),
    }


def test_save_and_list_face_images(tmp_path):
    cfg = _local_config(tmp_path)
    assert storage.list_face_images(cfg, 1) == []
    assert storage.count_face_images(cfg, 1) == 0

    storage.save_face_image(cfg, 1, "a.jpg", b"fake-jpeg-bytes-1")
    storage.save_face_image(cfg, 1, "b.jpg", b"fake-jpeg-bytes-2")
    storage.save_face_image(cfg, 2, "c.jpg", b"fake-jpeg-bytes-3")

    assert storage.count_face_images(cfg, 1) == 2
    assert storage.count_face_images(cfg, 2) == 1
    blobs = storage.list_face_images(cfg, 1)
    assert sorted(blobs) == sorted([b"fake-jpeg-bytes-1", b"fake-jpeg-bytes-2"])


def test_delete_face_images(tmp_path):
    cfg = _local_config(tmp_path)
    storage.save_face_image(cfg, 1, "a.jpg", b"data")
    assert storage.count_face_images(cfg, 1) == 1
    storage.delete_face_images(cfg, 1)
    assert storage.count_face_images(cfg, 1) == 0
    # Deleting again (nothing left) must not raise.
    storage.delete_face_images(cfg, 1)


def test_student_ids_with_images(tmp_path):
    cfg = _local_config(tmp_path)
    assert storage.student_ids_with_images(cfg) == []
    storage.save_face_image(cfg, 3, "a.jpg", b"data")
    storage.save_face_image(cfg, 1, "a.jpg", b"data")
    assert storage.student_ids_with_images(cfg) == [1, 3]


def test_save_and_load_model(tmp_path):
    cfg = _local_config(tmp_path)
    assert storage.load_model_bytes(cfg) is None
    assert storage.load_model_meta(cfg) is None

    storage.save_model(cfg, b"fake-pickled-model", {"trained_at": "now", "num_students": 2})

    assert storage.load_model_bytes(cfg) == b"fake-pickled-model"
    meta = storage.load_model_meta(cfg)
    assert meta["num_students"] == 2


def test_save_model_overwrites_previous(tmp_path):
    cfg = _local_config(tmp_path)
    storage.save_model(cfg, b"v1", {"num_students": 1})
    storage.save_model(cfg, b"v2", {"num_students": 2})
    assert storage.load_model_bytes(cfg) == b"v2"
    assert storage.load_model_meta(cfg)["num_students"] == 2
