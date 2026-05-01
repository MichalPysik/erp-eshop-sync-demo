from django.contrib import admin

from integrator.models import SyncedProduct


@admin.register(SyncedProduct)
class SyncedProductAdmin(admin.ModelAdmin):
    list_display = ("sku", "status", "active", "synced_at")
    list_filter = ("status", "active")
    search_fields = ("sku",)
    readonly_fields = ("last_hash", "synced_at", "payload")
