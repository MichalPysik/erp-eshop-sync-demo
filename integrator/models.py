from django.db import models


class SyncStatus(models.TextChoices):
    PENDING = "PENDING", "Loaded from ERP, not yet sent"
    SUCCESS = "SUCCESS", "Successfully sent to e-shop"
    FAILED = "FAILED", "Failed to send to e-shop"


class SyncedProduct(models.Model):
    sku = models.CharField(max_length=64, unique=True, db_index=True)
    last_hash = models.CharField(max_length=64, blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=10,
        choices=SyncStatus.choices,
        default=SyncStatus.PENDING,
    )
    active = models.BooleanField(default=True)
    payload = models.JSONField(null=True, blank=True, help_text="Last transformed payload sent to e-shop")

    class Meta:
        ordering = ["sku"]

    def __str__(self):
        return f"{self.sku} ({self.status})"
