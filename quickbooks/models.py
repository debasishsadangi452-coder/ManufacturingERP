from django.conf import settings
from django.core import signing
from django.db import models
from django.utils import timezone


class QuickBooksConnection(models.Model):
    ENVIRONMENT_CHOICES = [
        ("sandbox", "Sandbox"),
        ("production", "Production"),
    ]

    company = models.OneToOneField(
        "accounts.Company", on_delete=models.CASCADE, related_name="quickbooks_connection"
    )
    realm_id = models.CharField(max_length=100, db_index=True)
    environment = models.CharField(max_length=20, choices=ENVIRONMENT_CHOICES, default="sandbox")
    access_token_signed = models.TextField(blank=True)
    refresh_token_signed = models.TextField(blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True)
    company_name = models.CharField(max_length=255, blank=True)
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    connected_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-connected_at"]

    def set_access_token(self, token):
        self.access_token_signed = signing.dumps(token or "")

    def get_access_token(self):
        return signing.loads(self.access_token_signed) if self.access_token_signed else ""

    def set_refresh_token(self, token):
        self.refresh_token_signed = signing.dumps(token or "")

    def get_refresh_token(self):
        return signing.loads(self.refresh_token_signed) if self.refresh_token_signed else ""

    def __str__(self):
        return f"{self.company} -> QuickBooks {self.environment} ({self.realm_id})"


class QuickBooksSyncRun(models.Model):
    STATUS_CHOICES = [
        ("running", "Running"),
        ("success", "Success"),
        ("failed", "Failed"),
    ]

    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE)
    connection = models.ForeignKey(
        QuickBooksConnection, on_delete=models.CASCADE, related_name="sync_runs"
    )
    sync_type = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="running")
    records_created = models.IntegerField(default=0)
    records_updated = models.IntegerField(default=0)
    records_seen = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Heartbeat while a run is working; a "running" run that stops beating was
    # cut off (worker killed, redeploy) and is expired by background.running_job.
    last_activity_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-started_at"]


class QuickBooksEntityLink(models.Model):
    ENTITY_CHOICES = [
        ("customer", "Customer"),
        ("vendor", "Vendor"),
        ("item", "Item"),
        ("invoice", "Invoice"),
        ("bill", "Bill"),
        ("payment", "Payment"),
        ("sales_order", "Sales Order"),
        ("purchase_order", "Purchase Order"),
        ("bill_payment", "Bill Payment"),
        ("journal_entry", "Journal Entry"),
        ("report", "Report"),
    ]

    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE)
    entity_type = models.CharField(max_length=50, choices=ENTITY_CHOICES)
    local_object_id = models.PositiveIntegerField()
    quickbooks_id = models.CharField(max_length=100, db_index=True)
    sync_token = models.CharField(max_length=100, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("company", "entity_type", "quickbooks_id")


class QuickBooksSyncError(models.Model):
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE)
    sync_run = models.ForeignKey(
        QuickBooksSyncRun, on_delete=models.CASCADE, related_name="errors", null=True, blank=True
    )
    entity_type = models.CharField(max_length=50, blank=True)
    quickbooks_id = models.CharField(max_length=100, blank=True)
    message = models.TextField()
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class QuickBooksAccountMapping(models.Model):
    """Which QuickBooks account an ERP ledger account posts to when ERP journal
    entries are mirrored into QuickBooks (the company's main books)."""

    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE)
    account = models.OneToOneField(
        "accounting.Account", on_delete=models.CASCADE, related_name="quickbooks_mapping"
    )
    quickbooks_account_id = models.CharField(max_length=100)
    quickbooks_account_name = models.CharField(max_length=255, blank=True)
    quickbooks_account_type = models.CharField(max_length=100, blank=True)
    auto_mapped = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["account__code"]

    def __str__(self):
        return f"{self.account.code} -> QB {self.quickbooks_account_name or self.quickbooks_account_id}"
