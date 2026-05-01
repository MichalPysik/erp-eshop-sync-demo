from django.db import models


class SyncStatus(models.TextChoices):
    PENDING = "PENDING", "Loaded from ERP, not yet sent"
    SUCCESS = "SUCCESS", "Successfully sent to e-shop"
    FAILED = "FAILED", "Failed to send to e-shop"


class SyncedProduct(models.Model):
    sku = models.CharField(max_length=64, unique=True, db_index=True)
    last_hash = models.CharField(max_length=64, blank=True, default="", help_text="SHA256 hash of the last payload successfully sent to e-shop")
    synced_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the last successful sync")
    status = models.CharField(
        max_length=10,
        choices=SyncStatus.choices,
        default=SyncStatus.PENDING,
        help_text="Status of the last e-shop sync attempt",
    )
    active = models.BooleanField(default=True, help_text="Product is currently present in at least one of the ERP or the e-shop")
    payload = models.JSONField(null=True, blank=True, help_text="Last transformed payload obtained from ERP")

    class Meta:
        ordering = ["sku"]

    def __str__(self):
        return f"{self.sku} ({self.status})"
