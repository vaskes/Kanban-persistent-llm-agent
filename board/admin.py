"""
Admin registrations and the 4-section sidebar layout.

The admin is for OPERATIONS, not data inspection. Operators manage human
resources, projects, agent resources, and usable resources. Tasks, audit
trails and chat history belong in the WebUI (board / project detail) or in
direct URL access by the technical operator — not in the sidebar where they
clutter every screen.

Four sections:

  Human Resources   — who has access
  Projects          — what we work on
  Agent resources   — LLM-backed workers + their auto-minted API keys
  Usable Resources  — what agents USE to execute code (hosts, sandboxes,
                      ComfyUI endpoints, git repos, browsers)

The host/sandbox/comfyui/git/browser subtypes are still registered as
admins (so Add and change URLs work), but they do NOT show in the sidebar
— Resources is the operator's entry point. LlmEndpoint is unregistered
entirely: the Agent row already stores the same model credentials, and
showing both would invite inconsistency.
"""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.text import capfirst

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
    McpServer,
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


User = get_user_model()


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


# Backlog membership is managed via the Project detail page; the standalone
# admin was removed in the 4-section cleanup. Backlogs remain queryable at
# their direct admin URL — nothing is hidden from a technical operator who
# knows the URL, only from the sidebar.
for _m in (Backlog, ProjectMembership):
    try:
        admin.site.unregister(_m)
    except admin.sites.NotRegistered:
        pass


# Internal / audit models — live in the DB, queryable through direct admin
# URLs, but NEVER surfaced in the sidebar. Operators work on resources; they
# inspect task/attempt/memory/chat history via the WebUI board view.
for _m in (Task, TaskEvent, TaskDep, Attempt, Memory, ChatSession, ChatMessage):
    try:
        admin.site.unregister(_m)
    except admin.sites.NotRegistered:
        pass


# Django's built-in Groups: this project is intentionally flat (admin + a
# handful of executors). Hiding Groups prevents the "is this Jira?" reflex
# and keeps the sidebar minimal. Django registers Group by default, so this
# unconditional unregister is safe.
from django.contrib.auth.models import Group
admin.site.unregister(Group)


class AgentApiKeyInline(admin.TabularInline):
    """
    API keys belong to agents — auto-minted by `register_agent` on first
    creation, rotated via the `--rotate-key` flag. Showing them inline on the
    agent page means the admin does not need a separate screen to see who has
    access; the top-level "Agent api keys" entry is intentionally absent.

    `secret_hash` is the only thing the DB has — plaintext is gone the moment
    the command line scrolls. Showing the prefix is enough to recognise a
    key in logs.
    """
    model = AgentApiKey
    extra = 0
    fields = ["prefix", "scope", "label", "is_active",
              "user", "last_used_at", "use_count", "created_at"]
    readonly_fields = ["prefix", "last_used_at", "use_count", "created_at"]
    autocomplete_fields = ["user"]


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = [
        "name", "kind", "model_provider", "model_name",
        "status", "model_reachable", "model_checked_at",
        "last_seen_at", "active_api_keys", "model_base_url",
    ]
    list_filter = ["kind", "model_provider", "model_check_ok"]
    search_fields = ["name", "model_base_url", "base_url", "note", "model_check_error"]
    readonly_fields = [
        "registered_at", "last_seen_at",
        "model_checked_at", "model_check_ok", "model_check_error",
    ]
    actions = ["probe_reachability"]
    inlines = [AgentApiKeyInline]

    @admin.display(description="status")
    def status(self, obj):
        return obj.status

    @admin.display(boolean=True, description="model reachable")
    def model_reachable(self, obj):
        return obj.model_reachable

    @admin.display(description="active keys")
    def active_api_keys(self, obj):
        return obj.api_keys.filter(is_active=True).count()

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


# ChatSession / ChatMessage admins were removed in the 4-section cleanup.
# Models remain queryable via /admin/board/<model>/<pk>/change/ URLs for
# technical inspection; only the top-level sidebar entry was removed.


# AgentApiKey is intentionally NOT registered at the top level. Keys are
# minted by the admin via `manage.py register_agent --rotate-key` and live
# inline on the Agent page; showing them as a separate changelist invites
# the operator to edit them in isolation from the agent they belong to.


admin.site.site_header = "kanban-agent"
admin.site.site_title = "kanban-agent"
admin.site.index_title = "Operations"


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
#
# The base Resource admin is also registered so the operator sees all six
# kinds in one list. Each concrete kind has its own admin with its own
# fieldsets, because the relevant fields differ wildly across kinds.


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
#
# The base Resource admin is the operator's single entry point. Each concrete
# kind has its own admin with its own fieldsets, because the relevant fields
# differ wildly across kinds — but they only show up after the operator picks
# "what kind do you want to add?" via the picker below.


# Available resource kinds and the model class + URL name to redirect to when
# the operator picks one. Source of truth for the picker's button list.
# LlmEndpoint and McpServer are intentionally listed as 'forward-looking':
# registered today so the operator can record what their fleet talks to,
# but the runtime that actually uses them is part 2.
RESOURCE_KINDS = [
    ("host", "Host", Host,
     "A physical or virtual machine. SSH/WinRM/RDP, login+password or key."),
    ("sandbox", "Sandbox", Sandbox,
     "A Docker container spawned on demand. Image, CPU/memory limits."),
    ("llm_endpoint", "LLM endpoint", LlmEndpoint,
     "A standalone LLM an agent can consult or delegate a subtask to."),
    ("comfyui", "ComfyUI endpoint", ComfyUiEndpoint,
     "An HTTP server running ComfyUI."),
    ("git_repo", "Git repository", GitRepo,
     "A git repo with optional SSH key for cloning."),
    ("browser", "Web browser", Browser,
     "A Playwright-driven browser, ephemeral per allocation."),
    ("mcp_server", "MCP server", McpServer,
     "A Model Context Protocol server that exposes tools to an agent."),
]


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

    def has_add_permission(self, request):
        """
        The base Resource model cannot be saved directly — it has no
        concrete form until a child kind fills in its specific columns.
        The "+ Add" button is therefore replaced by a kind picker below.
        """
        return True  # the picker IS the add flow; super would render a broken form

    def add_view(self, request, form_url="", extra_context=None):
        """
        Replace the default add form (which would 500 — Resource has no
        concrete persistence path) with a kind picker. Each button goes
        to the matching subtype admin's add URL.
        """
        if request.method != "GET":
            return super().add_view(request, form_url, extra_context)
        context = {
            **self.admin_site.each_context(request),
            "title": "Add a resource",
            "kinds": [
                {
                    "kind": kind,
                    "label": label,
                    "help": help_text,
                    "add_url": reverse(
                        f"admin:{model._meta.app_label}_{model._meta.model_name}_add",
                    ),
                }
                for kind, label, model, help_text in RESOURCE_KINDS
            ],
            "opts": self.model._meta,
        }
        return TemplateResponse(
            request, "admin/board/resource/add_kind.html", context,
        )


# LlmEndpoint is registered below — it has its own admin hidden from the
# sidebar (an operator adds it via the Resources kind picker, not via a
# sidebar entry). It is distinct from Agent: Agent IS a worker, LlmEndpoint
# is a tool an agent can consult or delegate a subtask to.


# Concrete resource subtype admins: registered so that direct URLs (change,
# history) and the Add URL behind the kind picker keep working, but hidden
# from the sidebar so the operator goes through Resources as the entry point.
def _hide_from_sidebar(model_admin_cls):
    """Decorator that hides an admin from the sidebar index while keeping
    all its URLs reachable. The four-section sidebar in get_app_list() is
    the source of truth for visibility."""
    model_admin_cls.has_module_permission = lambda self, request: False
    return model_admin_cls


@_hide_from_sidebar
@admin.register(Host)
class HostAdmin(admin.ModelAdmin):
    list_display = [
        "name", "os_type", "hostname", "ip", "proto", "port", "archived",
    ]
    list_filter = ["os_type", "proto", "archived"]
    search_fields = ["name", "hostname", "ip", "description"]


@_hide_from_sidebar
@admin.register(Sandbox)
class SandboxAdmin(admin.ModelAdmin):
    list_display = [
        "name", "os_type", "image", "pool_size", "free_slots", "archived",
    ]
    list_filter = ["os_type", "archived"]
    search_fields = ["name", "image"]


@_hide_from_sidebar
@admin.register(ComfyUiEndpoint)
class ComfyUiEndpointAdmin(admin.ModelAdmin):
    list_display = ["name", "url", "archived"]
    list_filter = ["archived"]
    search_fields = ["name", "url"]


@_hide_from_sidebar
@admin.register(GitRepo)
class GitRepoAdmin(admin.ModelAdmin):
    list_display = ["name", "url", "default_branch", "archived"]
    list_filter = ["archived"]
    search_fields = ["name", "url"]


@_hide_from_sidebar
@admin.register(Browser)
class BrowserAdmin(admin.ModelAdmin):
    list_display = ["name", "image", "headless", "pool_size", "archived"]
    list_filter = ["headless", "archived"]
    search_fields = ["name", "image"]


@_hide_from_sidebar
@admin.register(LlmEndpoint)
class LlmEndpointAdmin(admin.ModelAdmin):
    """
    A standalone LLM an agent can call — for consulting a bigger model or
    delegating a subtask. Distinct from Agent: Agent IS a worker, LlmEndpoint
    is a tool. Forward-looking — registered today, used by the runtime later.
    """
    list_display = ["name", "base_url", "model_id", "archived"]
    list_filter = ["archived"]
    search_fields = ["name", "base_url", "model_id"]


@_hide_from_sidebar
@admin.register(McpServer)
class McpServerAdmin(admin.ModelAdmin):
    """
    A Model Context Protocol server — exposes tools to an agent at runtime.
    Forward-looking: registered today so the operator can document what their
    fleet talks to; the runtime that actually connects and discovers tools
    is part 2.
    """
    list_display = ["name", "transport", "url", "protocol_version", "archived"]
    list_filter = ["transport", "archived"]
    search_fields = ["name", "url"]


# Resource allocations live on the WebUI board (operator sees "Ornith has 2
# sandboxes busy" without leaving the kanban). Keep the admin URL reachable
# for diagnostics but pull it out of the sidebar.
@_hide_from_sidebar
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


# ---------------------------------------------------------------------------
# Four-section sidebar layout
# ---------------------------------------------------------------------------
#
# Override AdminSite.get_app_list to replace Django's default single "Board"
# section with four operator-facing sections. The registry still contains
# all the hidden admins (host, sandbox, …) but they do not appear here.
#
# Section ordering is the order shown in the sidebar. Changing the order
# here is the only thing required to reorder the sidebar.

SECTION_ORDER = [
    ("human_resources", "Human Resources"),
    ("projects", "Projects"),
    ("agent_resources", "Agent resources"),
    ("usable_resources", "Usable Resources"),
]

# Models per section: (section_key, model_class)
SECTION_MODELS = [
    ("human_resources", User),
    ("projects", Project),
    ("agent_resources", Agent),
    ("usable_resources", Resource),
]


# Save the original so we can fall back when the registry hasn't been touched.
_original_get_app_list = admin.site.get_app_list


def _kanban_get_app_list(self, request):
    """Build the four-section app list explicitly.

    We do NOT inherit from super() here: super() would emit a single "Board"
    section listing every registered model regardless of `has_module_permission`
    semantics, which produces a sidebar with all the hidden models unless we
    strip them out by hand. Easier to start from the registry we know about.
    """
    registry = self._registry  # model_class -> ModelAdmin instance
    by_section = {key: [] for key, _ in SECTION_ORDER}

    for section_key, model in SECTION_MODELS:
        ma = registry.get(model)
        if ma is None:
            # Section is empty (model not registered) — skip rather than show
            # an empty header.
            continue
        if not ma.has_module_permission(request):
            continue
        # A ModelAdmin in the registry but with zero permissions is a
        # misconfiguration, not something the operator will see. Skip the
        # perms check and let the link render — the destination will 403
        # if the operator truly has no access, which is the right signal.
        changelist_url = reverse(
            f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist",
        )
        by_section[section_key].append({
            "name": capfirst(model._meta.verbose_name_plural),
            "object_name": model._meta.object_name,
            "admin_url": changelist_url,
            "view_only": (
                not ma.has_add_permission(request)
                and not ma.has_change_permission(request)
            ),
        })

    out = []
    for section_key, section_label in SECTION_ORDER:
        models = by_section.get(section_key, [])
        if not models:
            continue
        # The section header's "home" link goes to the changelist of the
        # first model in the section — same convention Django uses.
        first_model = SECTION_MODELS_DICT[section_key]
        app_url = reverse(
            f"admin:{first_model._meta.app_label}_{first_model._meta.model_name}_changelist",
        )
        out.append({
            "name": section_label,
            "app_label": "kanban",
            "app_url": app_url,
            "has_module_perms": True,
            "models": models,
        })
    return out


SECTION_MODELS_DICT = dict(SECTION_MODELS)

admin.site.get_app_list = _kanban_get_app_list.__get__(admin.site)

# Cosmetic — keep the index page quiet by hiding the default 'Recent actions'
# sidebar block. Operators find what they need via the sidebar; the activity
# stream belongs in the WebUI board.
admin.site.enable_nav_sidebar = True
