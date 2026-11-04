import os
import tempfile

FEATURE_FLAGS = {"appointments": False, "cancel_slot": False, "appointment_outcome": False}

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    API_BASE_URL = os.getenv("API_BASE_URL", "").rstrip("/")
    REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
    APP_ENV = os.getenv("APP_ENV", "development")
    SESSION_DIR = os.getenv("SESSION_DIR", os.path.join(tempfile.gettempdir(), "medislot-sessions"))
    STATIC_BASE_URL = os.getenv("STATIC_BASE_URL", "/static").rstrip("/")
    SESSION_TYPE = "filesystem"
    SESSION_PERMANENT = False
    SESSION_USE_SIGNER = True
    SESSION_FILE_THRESHOLD = 500
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
    WTF_CSRF_TIME_LIMIT = None
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024 + 128 * 1024
    JSON_SORT_KEYS = False
