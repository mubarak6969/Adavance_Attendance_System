"""Environment-aware application configuration.

Settings are read from environment variables (optionally loaded from a
local .env file via python-dotenv) with safe defaults for local
development. Production requires SECRET_KEY, SUPER_ADMIN_EMAIL, and
SUPER_ADMIN_PASSWORD to be set explicitly -- the app refuses to start
without them.
"""
import os
import secrets

# Load .env for local development only. Production (Render, or any real
# deployment) always sets its environment variables directly -- see
# render.yaml -- so this must never run there: if a stray/example .env
# file happened to be present, load_dotenv()'s default of not overriding
# already-set variables would silently backfill any *missing* required
# production secret (SECRET_KEY, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
# from that file, defeating the "refuse to start without them" guarantee
# below.
if os.environ.get("FLASK_ENV", "development").lower() != "production":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _bool_env(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Config:
    """Shared defaults for every environment."""

    BASE_DIR = BASE_DIR

    # Local/dev storage (used when DATABASE_URL is not set -- see below).
    DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "attendance.db"))
    DATASET_DIR = os.environ.get("DATASET_DIR", os.path.join(BASE_DIR, "dataset"))
    MODEL_PATH = os.environ.get("MODEL_PATH", os.path.join(BASE_DIR, "model.pkl"))
    MODEL_META_PATH = os.environ.get("MODEL_META_PATH", os.path.join(BASE_DIR, "model_meta.json"))
    TRAIN_STATUS_PATH = os.environ.get("TRAIN_STATUS_PATH", os.path.join(BASE_DIR, "train_status.json"))
    LOG_PATH = os.environ.get("LOG_PATH", os.path.join(BASE_DIR, "app.log"))

    # Production storage on hosts with an ephemeral filesystem (free-tier
    # PaaS: no persistent disk survives a restart/redeploy there). When
    # DATABASE_URL is set, db.py switches from SQLite to this Postgres
    # connection, and storage.py switches from local dataset/+model.pkl
    # files to storing both as blobs in that same database -- one free,
    # durable backing store instead of a filesystem the host can wipe.
    # Leave unset for local development (falls back to SQLite + local files).
    DATABASE_URL = os.environ.get("DATABASE_URL")

    # First-run provisioning for the one account that must always exist.
    # Used only to create the super_admin row in the `users` table if it
    # doesn't already exist yet -- NOT the live authentication mechanism
    # (that's the users table itself, see db.py). An existing super_admin's
    # password is never overwritten by these on subsequent startups.
    SUPER_ADMIN_NAME = os.environ.get("SUPER_ADMIN_NAME", "Super Admin")
    SUPER_ADMIN_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL")
    SUPER_ADMIN_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD")

    # ML / recognition tuning -- all configurable, nothing hardcoded in route code.
    RECOGNITION_CONFIDENCE_THRESHOLD = float(os.environ.get("RECOGNITION_CONFIDENCE_THRESHOLD", 0.5))
    FACE_DETECTION_CONFIDENCE = float(os.environ.get("FACE_DETECTION_CONFIDENCE", 0.5))
    CAPTURE_IMAGE_COUNT = int(os.environ.get("CAPTURE_IMAGE_COUNT", 50))
    RECOGNITION_POLL_INTERVAL_MS = int(os.environ.get("RECOGNITION_POLL_INTERVAL_MS", 1500))
    MIN_TRAINING_IMAGES_PER_STUDENT = int(os.environ.get("MIN_TRAINING_IMAGES_PER_STUDENT", 5))
    MIN_TRAINING_STUDENTS = int(os.environ.get("MIN_TRAINING_STUDENTS", 2))

    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 25 * 1024 * 1024))  # 25MB per request

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_LIFETIME_HOURS = int(os.environ.get("SESSION_LIFETIME_HOURS", 12))

    LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", 5))
    LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", 60))

    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = False
    LOG_LEVEL = "INFO"
    ENV_NAME = "base"

    @staticmethod
    def validate():
        """Raise if required settings are missing. Overridden per-environment."""
        return


class DevelopmentConfig(Config):
    ENV_NAME = "development"
    DEBUG = True
    LOG_LEVEL = "DEBUG"
    # Randomized per-process fallback so `python app.py` works out of the box.
    # Sessions won't survive a restart, which is fine for local development only.
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    SUPER_ADMIN_EMAIL = Config.SUPER_ADMIN_EMAIL or "admin@example.com"
    SUPER_ADMIN_PASSWORD = Config.SUPER_ADMIN_PASSWORD or "admin123"


class ProductionConfig(Config):
    ENV_NAME = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    LOG_LEVEL = "INFO"
    SECRET_KEY = os.environ.get("SECRET_KEY")

    @staticmethod
    def validate():
        missing = [
            name for name in ("SECRET_KEY", "SUPER_ADMIN_EMAIL", "SUPER_ADMIN_PASSWORD")
            if not os.environ.get(name)
        ]
        if missing:
            raise RuntimeError(
                "Missing required environment variable(s) for production: "
                + ", ".join(missing)
                + ". Set them before starting the server (see .env.example)."
            )


class TestingConfig(Config):
    ENV_NAME = "testing"
    TESTING = True
    DEBUG = True
    SECRET_KEY = "test-secret-key"
    SUPER_ADMIN_EMAIL = "admin@example.com"
    SUPER_ADMIN_PASSWORD = "test-password"
    WTF_CSRF_ENABLED = False


_CONFIG_MAP = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def get_config():
    env_name = os.environ.get("FLASK_ENV", "development").lower()
    config_class = _CONFIG_MAP.get(env_name, DevelopmentConfig)
    config_class.validate()
    return config_class
