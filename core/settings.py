"""
Django settings for the kanban-agent task board.

Part 1 scope: task tracker core. No agent runtime yet — only the
provider interface, the state machine, budgets and operator controls.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key: str, default: str | None = None) -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-not-for-production")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django_htmx",
    "channels",
    "board",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "kanban"),
        "USER": env("POSTGRES_USER", "kanban"),
        "PASSWORD": env("POSTGRES_PASSWORD", "kanban"),
        "HOST": env("POSTGRES_HOST", "127.0.0.1"),
        "PORT": env("POSTGRES_PORT", "5433"),
        "CONN_MAX_AGE": int(env("POSTGRES_CONN_MAX_AGE", "60")),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- auth ---
# There is no registration. Accounts are created by the operator on the host.
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# Sessions carry the "don't log in every time" behaviour. Two weeks by default;
# the cookie persists across browser restarts.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = int(env("SESSION_COOKIE_AGE", str(14 * 24 * 3600)))
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_HTTPONLY = True
# Set SESSION_COOKIE_SECURE=true only when the board is served over HTTPS.
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE

# --- agent provider (part 1: interface + implementations) ---
AGENT_PROVIDER = env("AGENT_PROVIDER", "fake")
AGENT_LOCAL_LLAMA_BASE_URL = env("AGENT_LOCAL_LLAMA_BASE_URL", "http://127.0.0.1:8080/v1")
AGENT_LOCAL_LLAMA_MODEL = env("AGENT_LOCAL_LLAMA_MODEL", "")
AGENT_LOCAL_LLAMA_API_KEY = env("AGENT_LOCAL_LLAMA_API_KEY", "sk-noop")
AGENT_LOCAL_VLLM_BASE_URL = env("AGENT_LOCAL_VLLM_BASE_URL", "http://127.0.0.1:8000/v1")
AGENT_MINIMAX_BASE_URL = env("AGENT_MINIMAX_BASE_URL", "")
AGENT_MINIMAX_API_KEY = env("AGENT_MINIMAX_API_KEY", "")
AGENT_QWEN_BASE_URL = env("AGENT_QWEN_BASE_URL", "")
AGENT_QWEN_API_KEY = env("AGENT_QWEN_API_KEY", "")

# --- runtime tuning (consumed by the agent runtime in part 2) ---
TASK_LEASE_SECONDS = int(env("TASK_LEASE_SECONDS", "900"))
TASK_HEARTBEAT_SECONDS = int(env("TASK_HEARTBEAT_SECONDS", "10"))
TASK_DEFAULT_MAX_ATTEMPTS = int(env("TASK_DEFAULT_MAX_ATTEMPTS", "3"))
TASK_DEFAULT_MAX_TOKENS = int(env("TASK_DEFAULT_MAX_TOKENS", "200000"))
MAX_DEPTH = int(env("MAX_DEPTH", "4"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "board": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
