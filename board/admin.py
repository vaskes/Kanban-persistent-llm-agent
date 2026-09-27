"""
Admin registrations for the whole tree.

The admin site is the operator's safety net: whatever the WebUI does not cover
yet, there is a full CRUD surface here with filters, search and bulk actions.
Registering a model is therefore never optional — an unlisted model is a model
nobody can inspect when something looks wrong.
"""

from django.contrib import admin

from .models import (
    Agent,
    AgentApiKey,
    Attempt,
    Backlog,
    ChatMessage,
    ChatSession,
    Memory,
    Project,
    ProjectMembership,
    Task,
    TaskDep,
    TaskEvent,
)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ["key", "name", "is_default", "archived", "created_by", "created_at"]
    list_filter = ["is_default", "archived"]
    search_fields = ["key", "name", "description", "repo_url"]
    readonly_fields = ["created_at"]
    ordering = ["-is_default", "name"]
    actions = ["archive", "unarchive"]

    @admin.action(description="Archive selected projects")
    def archive(self, request, queryset):
        n = queryset.exclude(is_default=True).update(archived=True)
        self.message_user(request, f"{n} project(s) archived.")

    @admin.action(description="Unarchive selected projects")
    def unarchive(self, request, queryset):
        n = queryset.update(archived=False)
        self.message_user(request, f"{n} project(s) unarchived.")


@admin.register(ProjectMembership)
class ProjectMembershipAdmin(admin.ModelAdmin):
    list_display = ["project", "user", "can_write", "granted_by", "granted_at"]
    list_filter = ["can_write", "project"]
    search_fields = ["user__username", "project__key", "project__name"]


@admin.register(Backlog)
class BacklogAdmin(admin.ModelAdmin):
    list_display = ["name", "project", "is_default", "created_at"]
    list_filter = ["is_default", "project"]


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = [
        "name", "kind", "model_provider", "model_name",
        "status", "last_seen_at", "model_base_url",
    ]
    list_filter = ["kind", "model_provider"]
    search_fields = ["name", "model_base_url", "base_url", "note"]
    readonly_fields = ["registered_at", "last_seen_at"]
    actions = ["probe_reachability"]

    @admin.display(description="status")
    def status(self, obj):
        return obj.status

    @admin.action(description="Probe model reachability now")
    def probe_reachability(self, request, queryset):
        """
        Synchronously GET {model_base_url}/models for every selected row.
        Updates last_seen_at on success so the status column flips from
        'unreachable' (never heard of it) to 'reachable' (model answered).
        The full result of every probe is written to the message, because
        'it probably worked' is not what the operator needs.
        """
        from . import probe as _probe
        for a in queryset:
            result = _probe.probe_agent(a)
            ok = "OK" if result["ok"] else "FAIL"
            self.message_user(
                request,
                f"{a.name}: {ok} — {result.get('error') or result.get('models') or 'no models'}",
            )


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ["id", "scope", "anchor_label", "project", "created_by", "created_at"]
    list_filter = ["scope", "project"]
    search_fields = ["project__key", "task__title"]
    readonly_fields = ["created_at"]


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ["created_at", "session", "role", "agent", "content_short"]
    list_filter = ["role", "session__scope"]
    search_fields = ["content"]
    readonly_fields = ["created_at"]

    @admin.display(description="content")
    def content_short(self, obj):
        return obj.content[:80]


@admin.register(AgentApiKey)
class AgentApiKeyAdmin(admin.ModelAdmin):
    list_display = ["prefix", "label", "scope", "user", "is_active", "use_count", "last_used_at"]
    list_filter = ["scope", "is_active"]
    search_fields = ["prefix", "label", "user__username"]
    readonly_fields = ["prefix", "secret_hash", "created_at", "last_used_at", "use_count"]


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = [
        "id", "title", "status", "project", "kind", "priority", "autonomy",
        "assignee", "attempts", "tokens_used", "stuck_score", "claimed_by",
        "updated_at",
    ]
    list_filter = ["status", "kind", "autonomy", "project", "created_by", "assignee"]
    search_fields = ["title", "goal", "acceptance", "current_step"]
    readonly_fields = ["id", "created_at", "updated_at", "closed_at"]
    ordering = ["-priority", "created_at"]


@admin.register(TaskEvent)
class TaskEventAdmin(admin.ModelAdmin):
    list_display = ["ts", "task", "actor", "event", "from_status", "to_status"]
    list_filter = ["actor", "event", "to_status"]
    search_fields = ["task__title"]


@admin.register(TaskDep)
class TaskDepAdmin(admin.ModelAdmin):
    list_display = ["task", "depends_on"]
    search_fields = ["task__title", "depends_on__title"]


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = ["task", "started_at", "ended_at", "outcome", "tokens_out", "score"]
    list_filter = ["outcome"]


@admin.register(Memory)
class MemoryAdmin(admin.ModelAdmin):
    list_display = ["kind", "content_short", "source_task", "confidence", "use_count"]
    list_filter = ["kind"]
    search_fields = ["content"]

    @admin.display(description="content")
    def content_short(self, obj):
        return obj.content[:80]

admin.site.site_header = "kanban-agent"
admin.site.site_title = "kanban-agent"
admin.site.index_title = "Board data"
