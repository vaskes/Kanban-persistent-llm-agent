from django.apps import AppConfig


class BoardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "board"

    def ready(self):
        # registers the first-user-is-admin signal
        from . import signals  # noqa: F401
