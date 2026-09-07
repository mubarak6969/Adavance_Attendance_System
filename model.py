"""Face embedding extraction, model training, and prediction.

Honest description of the approach (see README for the full write-up):
MediaPipe face detection crops the face, which is converted to a 32x32
grayscale, histogram-equalized, flattened pixel vector and classified with
a RandomForestClassifier. This is a lightweight demonstration pipeline,
not a biometric-grade face-embedding model (e.g. FaceNet/ArcFace) -- it is
sensitive to lighting, pose, and image quality. It works well enough for a
small, cooperative dataset (a classroom of consistently-lit webcam shots)
but should not be marketed or relied on as secure biometric authentication.

This module has no idea where images or the trained model actually live --
it takes image bytes in and hands model bytes back. See storage.py for the
local-filesystem-vs-database persistence decision.
"""
import pickle
import logging
import datetime

import cv2
import numpy as np
from sklearn.ensemble import RandomForestClassifier

logger = logging.getLogger(__name__)

EMBED_SIZE = (32, 32)


def _get_face_detector(min_detection_confidence=0.5):
    import mediapipe as mp
    return mp.solutions.face_detection.FaceDetection(
        model_selection=1, min_detection_confidence=min_detection_confidence
    )


def crop_face_and_embed(bgr_image, detection):
    h, w = bgr_image.shape[:2]
    bbox = detection.location_data.relative_bounding_box
    x1 = int(max(0, bbox.xmin * w))
    y1 = int(max(0, bbox.ymin * h))
    x2 = int(min(w, (bbox.xmin + bbox.width) * w))
    y2 = int(min(h, (bbox.ymin + bbox.height) * h))
    if x2 <= x1 or y2 <= y1:
        return None
    face = bgr_image[y1:y2, x1:x2]
    face = cv2.cvtColor(face, cv2.COLOR_BGR2GRAY)
    face = cv2.equalizeHist(face)  # normalize lighting/contrast across captures
    face = cv2.resize(face, EMBED_SIZE, interpolation=cv2.INTER_AREA)
    emb = face.flatten().astype(np.float32) / 255.0
    return emb


def _decode_and_detect(image_bytes, detector):
    """Shared core for both live recognition and training: bytes -> (status, embedding).

    status is one of: "ok", "no_face", "multiple_faces", "decode_error".
    """
    if not image_bytes:
        return "decode_error", None
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return "decode_error", None

    results = detector.process(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    if not results.detections:
        return "no_face", None
    if len(results.detections) > 1:
        return "multiple_faces", None

    emb = crop_face_and_embed(img, results.detections[0])
    if emb is None:
        return "no_face", None
    return "ok", emb


def extract_embedding_for_image(stream_or_bytes, min_detection_confidence=0.5):
    """Returns a dict: {"status": ..., "embedding": np.ndarray | None}

    status is one of: "ok", "no_face", "multiple_faces", "decode_error".
    Used for live recognition uploads (a file-like stream from the kiosk).
    """
    data = stream_or_bytes.read()
    detector = _get_face_detector(min_detection_confidence)
    try:
        status, emb = _decode_and_detect(data, detector)
    finally:
        detector.close()
    return {"status": status, "embedding": emb}


def is_valid_image(data):
    """True if `data` (raw bytes) decodes as an actual image.

    Used to reject uploads that merely have an allowed extension (.jpg)
    but aren't real image content -- extension checks alone don't verify
    the bytes are what they claim to be.
    """
    if not data:
        return False
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img is not None


def load_model_from_bytes(data):
    """Unpickle a trained classifier from raw bytes (as returned by
    storage.load_model_bytes). Returns None if there's no model yet, or if
    the stored bytes are missing/corrupt -- callers treat that the same as
    "not trained".
    """
    if not data:
        return None
    try:
        return pickle.loads(data)
    except (pickle.UnpicklingError, EOFError, ValueError, AttributeError) as e:
        logger.error("Failed to load model: %s", e)
        return None


def predict_with_model(clf, emb):
    proba = clf.predict_proba([emb])[0]
    idx = int(np.argmax(proba))
    label = clf.classes_[idx]
    conf = float(proba[idx])
    return label, conf


def train_model(
    image_data_by_student,
    min_images_per_student=5,
    min_students=2,
    face_detection_confidence=0.5,
    progress_callback=None,
):
    """Trains a RandomForest classifier from already-fetched image bytes.

    image_data_by_student: {student_id: [image_bytes, image_bytes, ...]}
    Returns (model_bytes, meta_dict) on success, or (None, None) on failure
    -- failures are reported through progress_callback, never raised, so the
    caller (a background thread) can always persist a terminal, non-"running"
    state. The caller is responsible for actually persisting the returned
    model_bytes/meta via storage.save_model(); this function has no
    filesystem or database access at all.
    """

    def report(pct, message, status="running"):
        logger.info("[train] %s%% - %s (%s)", pct, message, status)
        if progress_callback:
            progress_callback(pct, message, status)

    try:
        if not image_data_by_student:
            report(0, "No students with captured images were found.", "error")
            return None, None

        detector = _get_face_detector(face_detection_confidence)
        X, y = [], []
        skipped_unreadable = 0
        skipped_no_face = 0
        per_student_counts = {}

        try:
            student_ids = sorted(image_data_by_student.keys())
            total = len(student_ids)
            for i, sid in enumerate(student_ids, start=1):
                images = image_data_by_student[sid]
                used = 0
                for image_bytes in images:
                    status, emb = _decode_and_detect(image_bytes, detector)
                    if status == "decode_error":
                        skipped_unreadable += 1
                        continue
                    if status in ("no_face", "multiple_faces"):
                        skipped_no_face += 1
                        continue
                    X.append(emb)
                    y.append(int(sid))
                    used += 1
                per_student_counts[sid] = used
                pct = int((i / total) * 80)
                report(pct, f"Processed {i}/{total} students ({used} usable images)…")
        finally:
            detector.close()

        usable_students = [sid for sid, count in per_student_counts.items() if count >= min_images_per_student]

        if len(X) == 0:
            report(
                0,
                "No usable face images found in the dataset. Capture photos with a clearly "
                "visible, single face per image before training.",
                "error",
            )
            return None, None

        if len(usable_students) < min_students:
            report(
                0,
                f"Only {len(usable_students)} student(s) have at least {min_images_per_student} usable "
                f"images (need {min_students}+). Capture more images, then retrain.",
                "error",
            )
            return None, None

        X = np.stack(X)
        y = np.array(y)

        report(85, "Training RandomForest classifier…")
        # n_jobs intentionally left at its default (single-threaded, in-process).
        # joblib's process-based backend (n_jobs=-1/>1) spawns new Python
        # processes that re-import this module; since app.py builds the full
        # Flask app at import time (required so `gunicorn app:app` works),
        # every spawned worker would redundantly rebuild the whole app.
        # At this project's scale (a classroom-sized dataset) single-threaded
        # training is already sub-second, so there's no reason to risk it.
        clf = RandomForestClassifier(n_estimators=150, random_state=42)
        clf.fit(X, y)
        model_bytes = pickle.dumps(clf)

        trained_student_ids = sorted(int(sid) for sid in usable_students)
        meta = {
            "trained_at": datetime.datetime.utcnow().isoformat(),
            "num_students": len(trained_student_ids),
            "num_samples": int(len(y)),
            "trained_student_ids": trained_student_ids,
            "skipped_unreadable_images": skipped_unreadable,
            "skipped_no_face_images": skipped_no_face,
        }

        report(
            100,
            f"Training complete — {meta['num_students']} student(s), {meta['num_samples']} image(s) used.",
            "success",
        )
        return model_bytes, meta
    except Exception as e:  # noqa: BLE001 -- background job must never crash silently
        logger.exception("Training failed with an unexpected error")
        report(0, f"Training failed: {e}", "error")
        return None, None
