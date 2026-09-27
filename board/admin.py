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
    Browser,
    ChatMessage,
    ChatSession,
    ComfyUiEndpoint,
    GitRepo,
    Host,
    LlmEndpoint,
    Memory,
    Project,
    ProjectMembership,
    Resource,
    ResourceAllocation,
    Sandbox,
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
        "status", "model_reachable", "model_checked_at",
        "last_seen_at", "model_base_url",
    ]
    list_filter = ["kind", "model_provider", "model_check_ok"]
    search_fields = ["name", "model_base_url", "base_url", "note", "model_check_error"]
    readonly_fields = [
        "registered_at", "last_seen_at",
        "model_checked_at", "model_check_ok", "model_check_error",
    ]
    actions = ["probe_reachability"]

    @admin.display(description="status")
    def status(self, obj):
        return obj.status

    @admin.display(boolean=True, description="model reachable")
    def model_reachable(self, obj):
        return obj.model_reachable

    @admin.action(description="Probe model reachability now")
    def probe_reachability(self, request, queryset):
        """
        Synchronously GET {model_base_url}/models for every selected row.
        Updates model_checked_at / model_check_ok / model_check_error and
        last_seen_at so the status and model_reachable columns both flip.
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


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
#
# The base Resource admin is also registered so the operator sees all six
# kinds in one list. Each concrete kind has its own admin with its own
# fieldsets, because the relevant fields differ wildly across kinds.


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = [
        "name", "kind", "lifetime", "capacity",
        "free_slots", "archived", "updated_at",
    ]
    list_filter = ["kind", "lifetime", "archived"]
    search_fields = ["name", "description"]
    readonly_fields = ["created_at", "updated_at"]

    @admin.display(description="free")
    def free_slots(self, obj):
        return f"{obj.free_slots}/{obj.capacity}"


@admin.register(Host)
class HostAdmin(admin.ModelAdmin):
    list_display = [
        "name", "os_type", "hostname", "ip", "proto", "port", "archived",
    ]
    list_filter = ["os_type", "proto", "archived"]
    search_fields = ["name", "hostname", "ip", "description"]


@admin.register(Sandbox)
class SandboxAdmin(admin.ModelAdmin):
    list_display = [
        "name", "os_type", "image", "pool_size", "free_slots", "archived",
    ]
    list_filter = ["os_type", "archived"]
    search_fields = ["name", "image"]


@admin.register(LlmEndpoint)
class LlmEndpointAdmin(admin.ModelAdmin):
    list_display = ["name", "base_url", "model_id", "archived"]
    list_filter = ["archived"]
    search_fields = ["name", "base_url", "model_id"]


@admin.register(ComfyUiEndpoint)
class ComfyUiEndpointAdmin(admin.ModelAdmin):
    list_display = ["name", "url", "archived"]
    list_filter = ["archived"]
    search_fields = ["name", "url"]


@admin.register(GitRepo)
class GitRepoAdmin(admin.ModelAdmin):
    list_display = ["name", "url", "default_branch", "archived"]
    list_filter = ["archived"]
    search_fields = ["name", "url"]


@admin.register(Browser)
class BrowserAdmin(admin.ModelAdmin):
    list_display = ["name", "image", "headless", "pool_size", "archived"]
    list_filter = ["headless", "archived"]
    search_fields = ["name", "image"]


@admin.register(ResourceAllocation)
class ResourceAllocationAdmin(admin.ModelAdmin):
    list_display = [
        "resource", "owner", "status", "agent",
        "requested_at", "granted_at", "released_at",
    ]
    list_filter = ["status", "resource__kind"]
    search_fields = [
        "resource__name", "task__title", "project__key",
        "agent__name", "instance_id", "error",
    ]
    readonly_fields = [
        "requested_at", "granted_at", "released_at",
    ]
    date_hierarchy = "requested_at"

    @admin.display(description="owner")
    def owner(self, obj):
        if obj.task_id:
            return f"task:{obj.task_id[:8]}"
        if obj.project_id:
            return f"project:{obj.project_id}"
        return "-"

    def has_add_permission(self, request):
        """
        Allocations are normally created by the runtime in response to an
        agent's request. Manual creation by an operator is allowed (for
        seeding) but the operator should understand it counts against
        capacity. We leave it open — the operator owns the board.
        """
        return super().has_add_permission(request)
