"""Forms for managing projects and their membership."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model

from .models import Project, ProjectMembership


class ProjectForm(forms.ModelForm):
    """
    Create and edit a project.

    `key` is not offered as a free field: it is a SlugField Django generates
    from the name, so the URL stays readable, and editing it later would break
    every link and every stored chat scope pointing at the project.

    There is deliberately no clean_name. CharField strips whitespace before
    field validation, so a whitespace-only name is already rejected as
    "This field is required." — a hand-written check there would never run.
    """

    class Meta:
        model = Project
        fields = ["name", "description", "repo_url"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class MembershipForm(forms.Form):
    """
    Grant a user access to a project.

    Two levels only, read and write. Anything finer has not been needed and
    would be one more thing to get wrong; absence of a row means no access.
    """

    user = forms.ModelChoiceField(
        queryset=get_user_model().objects.order_by("username"),
        label="User",
    )
    can_write = forms.BooleanField(
        required=False,
        label="Can write",
        help_text="Clear for read-only. Without a row the user has no access at all.",
    )

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        if project is not None:
            # Existing members are deliberately NOT filtered out of the
            # queryset. Excluding them made a duplicate submission fail with
            # Django's generic "Select a valid choice", and made the clear
            # message from clean_user unreachable. A rejected action should say
            # what is wrong, not that the input was malformed.
            if not self.fields["user"].queryset.exists():
                self.fields["user"].help_text = "No accounts exist yet."

    def clean_user(self):
        user = self.cleaned_data["user"]
        if self.project and self.project.memberships.filter(user=user).exists():
            raise forms.ValidationError(
                f"{user.username} already has access to this project."
            )
        return user


class MemberRowForm(forms.Form):
    """Toggle write access for an existing member."""

    can_write = forms.BooleanField(required=False)
