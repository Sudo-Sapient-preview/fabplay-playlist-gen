import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


DEBUG = _env_bool("DJANGO_DEBUG", True)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-rewrite-dev-secret-key"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is disabled.")

ALLOWED_HOSTS = _env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1"] if DEBUG else [])
if not ALLOWED_HOSTS and not DEBUG:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be configured when DJANGO_DEBUG is disabled.")

CORS_ALLOWED_ORIGINS = _env_list("ALLOWED_ORIGINS", ["http://localhost:8000"] if DEBUG else [])
CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS", CORS_ALLOWED_ORIGINS)

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "corsheaders",
    "api.apps.ApiConfig",
    "webapp.apps.WebAppConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "fabplay.middleware.ApiCsrfBypassMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "fabplay.urls"
WSGI_APPLICATION = "fabplay.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "web" / "static"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [],
        },
    }
]

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "web" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

if DEBUG:
    staticfiles_backend = "django.contrib.staticfiles.storage.StaticFilesStorage"
    WHITENOISE_USE_FINDERS = True
else:
    staticfiles_backend = "whitenoise.storage.CompressedManifestStaticFilesStorage"
    WHITENOISE_USE_FINDERS = False

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": staticfiles_backend,
    },
}

WHITENOISE_MAX_AGE = int(os.getenv("WHITENOISE_MAX_AGE", "31536000" if not DEBUG else "0"))

# Django expects a default database even though app persistence lives in
# Supabase and local JSON stores.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

APPEND_SLASH = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Audio is served from the public Supabase storage bucket. When SONGS_BASE_URL
# is not set explicitly it is derived from SUPABASE_URL.
SONGS_BUCKET = os.getenv("SONGS_BUCKET", "songs").strip()
_songs_base = os.getenv("SONGS_BASE_URL", "").strip().rstrip("/")
if not _songs_base:
    _sb_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    if _sb_url:
        _songs_base = f"{_sb_url}/storage/v1/object/public/{SONGS_BUCKET}"
SONGS_BASE_URL = _songs_base
SONGS_DIR = os.getenv("SONGS_DIR", str(BASE_DIR.parent / "songs"))

# PostHog analytics (frontend key is also exposed via /api/config)
POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY", "").strip()
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com").strip().rstrip("/") or "https://us.i.posthog.com"
POSTHOG_ENABLED = _env_bool("POSTHOG_ENABLED", bool(POSTHOG_API_KEY))

USE_X_FORWARDED_HOST = _env_bool("DJANGO_USE_X_FORWARDED_HOST", not DEBUG)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

SESSION_COOKIE_SECURE = _env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = _env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "3600" if not DEBUG else "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = _env_bool("DJANGO_SECURE_HSTS_PRELOAD", not DEBUG)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        }
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
}
