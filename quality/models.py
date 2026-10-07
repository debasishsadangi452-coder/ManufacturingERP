from django.db import models
from production.models import ProductionOrder


class QualityCheck(models.Model):

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    production_order = models.ForeignKey(
        ProductionOrder,
        on_delete=models.CASCADE
    )

    # The finished lot this QC check covers, so quality results are traceable
    # per-lot (SQF). Nullable: existing checks predate lots, and a check may be
    # recorded before the lot is created.
    lot = models.ForeignKey(
        "inventory.Batch", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="quality_checks",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )

    test_type = models.CharField(max_length=100, blank=True)
    parameter = models.CharField(max_length=100, blank=True)
    result = models.CharField(max_length=100, blank=True)
    target = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)

    inspected_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"QC for Production #{self.production_order.id}"


class IncomingQualityCheck(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("passed", "Passed"),
        ("failed", "Failed"),
        ("hold", "Hold"),
    ]

    check_number = models.CharField(max_length=64, unique=True, db_index=True)
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    goods_receipt = models.ForeignKey(
        "procurement.GoodsReceipt", on_delete=models.CASCADE, related_name="incoming_qc_checks"
    )
    purchase_order = models.ForeignKey(
        "procurement.PurchaseOrder", on_delete=models.CASCADE, related_name="incoming_qc_checks"
    )
    vendor = models.ForeignKey(
        "procurement.Vendor", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    item = models.ForeignKey(
        "inventory.Item", on_delete=models.CASCADE, related_name="+"
    )
    batch = models.ForeignKey(
        "inventory.Batch", null=True, blank=True, on_delete=models.SET_NULL, related_name="incoming_qc_checks"
    )
    received_warehouse = models.ForeignKey(
        "inventory.Warehouse", on_delete=models.CASCADE, related_name="+"
    )
    destination_warehouse = models.ForeignKey(
        "inventory.Warehouse", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    received_quantity = models.FloatField()
    released_quantity = models.FloatField(default=0.0)
    uom = models.CharField(max_length=20, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    inspector = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    inspection_date = models.DateTimeField(null=True, blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    remarks = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.check_number:
            import time
            gr_id = self.goods_receipt_id or "0"
            ts = int(time.time() * 1000) % 1000000
            candidate = f"IQC-{gr_id}-{ts}"
            while IncomingQualityCheck.objects.filter(check_number=candidate).exists():
                ts += 1
                candidate = f"IQC-{gr_id}-{ts}"
            self.check_number = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.check_number} · {self.item.name} ({self.get_status_display()})"
