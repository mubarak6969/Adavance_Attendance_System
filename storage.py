"""Storage abstraction for face images and the trained model.

Two backends, selected by whether app.config["DATABASE_URL"] is set:

- Local (default, no DATABASE_URL): the original behavior -- images under
  DATASET_DIR/<student_id>/, model bytes in MODEL_PATH, metadata in
  MODEL_META_PATH. Used for local development.
- Database (DATABASE_URL set): images and the model are stored as blobs in
  the same database as students/attendance (see db.py's face_images and
  model_artifacts tables). Used in production on hosts with an ephemeral
  filesystem, where anything written to local disk here would be wiped on
  the next restart or redeploy.

app.py and model.py only call the functions below; neither knows or cares
which backend is active -- that's the point of this module.
"""
import os
import json
import shutil

import db

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


def _use_database(app_config):
    return bool(app_config.get("DATABASE_URL"))


# ---------- Face images ----------

def save_face_image(app_config, student_id, filename, data):
    if _use_database(app_config):
        db.save_face_image(student_id, filename, data)
        return
    folder = os.path.join(app_config["DATASET_DIR"], str(student_id))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, filename), "wb") as f:
        f.write(data)


def list_face_images(app_config, student_id):
    """Returns a list of raw image bytes captured for one student."""
    if _use_database(app_config):
        return db.list_face_image_blobs(student_id)
    folder = os.path.join(app_config["DATASET_DIR"], str(student_id))
    if not os.path.isdir(folder):
        return []
    blobs = []
    for fname in sorted(os.listdir(folder)):
        if fname.lower().endswith(_IMAGE_EXTENSIONS):
            with open(os.path.join(folder, fname), "rb") as f:
                blobs.append(f.read())
    return blobs


def count_face_images(app_config, student_id):
    if _use_database(app_config):
        return db.count_face_images(student_id)
    folder = os.path.join(app_config["DATASET_DIR"], str(student_id))
    if not os.path.isdir(folder):
        return 0
    return len([f for f in os.listdir(folder) if f.lower().endswith(_IMAGE_EXTENSIONS)])


def delete_face_images(app_config, student_id):
    if _use_database(app_config):
        db.delete_face_images(student_id)
        return
    folder = os.path.join(app_config["DATASET_DIR"], str(student_id))
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)


def student_ids_with_images(app_config):
    """Ids of every student who has captured at least one image -- used by
    training to know whose photos to build embeddings from.
    """
    if _use_database(app_config):
        return db.student_ids_with_images()
    dataset_dir = app_config["DATASET_DIR"]
    if not os.path.isdir(dataset_dir):
        return []
    return sorted(
        int(d) for d in os.listdir(dataset_dir)
        if os.path.isdir(os.path.join(dataset_dir, d)) and d.isdigit()
    )


# ---------- Model ----------

_MODEL_KEY = "model"


def save_model(app_config, model_bytes, meta):
    if _use_database(app_config):
        db.save_model_artifact(_MODEL_KEY, model_bytes, meta)
        return
    from util import atomic_write, atomic_write_json

    def _write(tmp_path):
        with open(tmp_path, "wb") as f:
            f.write(model_bytes)

    atomic_write(app_config["MODEL_PATH"], _write)
    atomic_write_json(app_config["MODEL_META_PATH"], meta)


def load_model_bytes(app_config):
    if _use_database(app_config):
        artifact = db.load_model_artifact(_MODEL_KEY)
        return artifact["data"] if artifact else None
    path = app_config["MODEL_PATH"]
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def load_model_meta(app_config):
    if _use_database(app_config):
        artifact = db.load_model_artifact(_MODEL_KEY)
        return artifact["meta"] if artifact else None
    path = app_config["MODEL_META_PATH"]
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
