"""
Registration.

A self-hosted instance needs people to be able to arrive and sign up, without an
operator provisioning each account by hand. Access after registering is
deliberately nothing at all: a new account can see no project until an
administrator grants it read access, so signing up grants nothing.

The first account to register becomes an administrator (see board.signals).
After that, registration always creates an unprivileged user. This is the
documented bootstrap rule, and it is why an instance that is reachable before
its owner has registered should not be exposed to an untrusted network.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction
from django.utils import timezone


class RegistrationForm(UserCreationForm):
    """Username, optional email, password with confirmation."""

    class Meta:
        model = get_user_model()
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {"autofocus": True, "autocapitalize": "none", "spellcheck": "false"}
        )
        self.fields["username"].help_text = (
            "Letters, digits and @ . + - _ only. This is your sign-in name."
        )
        self.fields["email"].required = False
        self.fields["email"].help_text = "Optional. Used for nothing but contact."
        self.fields["password1"].help_text = ""
        self.fields["password2"].help_text = "Enter the same password again."


def register_user(form: RegistrationForm) -> tuple[object, bool]:
    """
    Create the account.

    Returns (user, is_first_user). The flag lets the caller tell a brand new
    administrator from an ordinary signup, so the UI can say so.
    """
    with transaction.atomic():
        before = get_user_model().objects.count()
        user = form.save()
        # the post_save signal promotes the first account; re-read to see
        # whether that was this one
        user.refresh_from_db()
        is_first = before == 0
    return user, is_first


def new_user_sees_nothing(user) -> bool:
    """
    A freshly registered account has no project access.

    Registration grants no membership and does not make the user an admin
    unless they were the first. The default project is *not* granted
    implicitly: an account that could read it would not be empty.
    """
    from .permissions import visible_projects

    return not visible_projects(user).exists()
