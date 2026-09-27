from django.contrib import admin

from .models import Attempt, Memory, Status, Task, TaskDep, TaskEvent


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = [
        "id", "title", "status", "kind", "priority", "autonomy",
        "attempts", "tokens_used", "stuck_score", "claimed_by", "updated_at",
    ]
    list_filter = ["status", "kind", "autonomy", "created_by"]
    search_fields = ["title", "goal", "acceptance", "current_step"]
    readonly_fields = ["id", "created_at", "updated_at", "closed_at"]
    ordering = ["-priority", "created_at"]
    filter_horizontal = ["dependencies"]


@admin.register(TaskEvent)
class TaskEventAdmin(admin.ModelAdmin):
    list_display = ["ts", "task", "actor", "event", "from_status", "to_status"]
    list_filter = ["actor", "event", "to_status"]
    search_fields = ["task__title"]


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = ["task", "started_at", "ended_at", "outcome", "tokens_out", "score"]
    list_filter = ["outcome"]


admin.site.register(TaskDep)
admin.site.register(Memory)
admin.site.site_header = "kanban-agent"
admin.site.site_title = "kanban-agent"
