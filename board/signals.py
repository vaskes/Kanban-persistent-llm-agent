"""
Bootstrap rule: the first account on a fresh install is an administrator.

There is no registration, so every account is created deliberately by the
operator. The only time "who is first" is meaningful is the initial bootstrap,
and at that moment granting admin is almost always what was intended — an
install where the operator cannot reach the admin is a worse outcome than an
install where the first account is privileged.

The rule fires exactly once: only when the new user is the *first* row in the
table. Every later account is created with whatever privileges the creator
chose, and nothing here widens them.
"""

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=get_user_model(), dispatch_uid="kanban.first_user_is_admin")
def promote_first_user(sender, instance, created, **kwargs):
    if not created:
        return
    if instance.is_superuser or instance.is_staff:
        return
    # count() == 1 means this save just inserted the very first row
    if sender.objects.count() != 1:
        return
    instance.is_staff = True
    instance.is_superuser = True
    instance.save(update_fields=["is_staff", "is_superuser"])
