from django.db import models
from production.models import ProductionOrder


class QualityCheck(models.Model):
    """One QA inspection: incoming raw material or production output (TB-04).

    `status` is the overall decision kept for the existing screens
    (pending / approved / rejected). `result` and the quantity fields record
    the detailed outcome, so a single inspection can pass part of a quantity
    and reject, quarantine or send the rest to rework.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]
    INSPECTION_TYPE_CHOICES = [
        ("production", "Production / Finished Goods"),
        ("incoming", "Incoming Raw Material"),
    ]
    DECISION_CHOICES = [
        ("", "Not decided"),
        ("pass", "Pass"),
        ("partial", "Partial pass"),
        ("fail", "Fail"),
        ("quarantine", "Quarantine"),
        ("rework", "Rework Required"),
    ]

    inspection_type = models.CharField(max_length=20, choices=INSPECTION_TYPE_CHOICES, default="production")

    # Production QA. Nullable because incoming inspections have no order.
    production_order = models.ForeignKey(
        ProductionOrder,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    output = models.ForeignKey(
        "production.ProductionOutput", null=True, blank=True, on_delete=models.CASCADE,
        related_name="quality_checks",
    )

    # Incoming QA.
    goods_receipt = models.ForeignKey(
        "procurement.GoodsReceipt", null=True, blank=True, on_delete=models.CASCADE,
        related_name="quality_checks",
    )
    vendor = models.ForeignKey(
        "procurement.Vendor", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    item = models.ForeignKey(
        "inventory.Item", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )

    # The lot this QC check covers, so quality results are traceable
    # per-lot (SQF). Nullable: existing checks predate lots.
    lot = models.ForeignKey(
        "inventory.Batch", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="quality_checks",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )
    result_decision = models.CharField(max_length=20, choices=DECISION_CHOICES, blank=True, default="")

    # Quantities. Null on checks created before Track B (whole-order QA).
    received_quantity = models.FloatField(null=True, blank=True)
    sample_quantity = models.FloatField(null=True, blank=True)
    accepted_quantity = models.FloatField(null=True, blank=True)
    rejected_quantity = models.FloatField(null=True, blank=True)
    quarantined_quantity = models.FloatField(null=True, blank=True)
    rework_quantity = models.FloatField(null=True, blank=True)

    test_type = models.CharField(max_length=100, blank=True)
    parameter = models.CharField(max_length=100, blank=True)
    result = models.CharField(max_length=100, blank=True)
    target = models.CharField(max_length=100, blank=True)
    # Further inspection parameters: [{"parameter", "target", "result", "pass"}]
    parameters = models.JSONField(default=list, blank=True)
    remarks = models.TextField(blank=True)

    inspector = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    inspected_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    certificate_number = models.CharField(max_length=40, blank=True, default="")

    @property
    def inspected_quantity(self):
        if self.received_quantity is not None:
            return self.received_quantity
        if self.output_id:
            return self.output.quantity
        return self.production_order.quantity if self.production_order_id else 0

    @property
    def pending_quantity(self):
        if self.status == "pending":
            return self.inspected_quantity
        decided = sum(
            q or 0 for q in (
                self.accepted_quantity, self.rejected_quantity,
                self.quarantined_quantity, self.rework_quantity,
            )
        )
        return max(self.inspected_quantity - decided, 0)

    def __str__(self):
        if self.inspection_type == "incoming":
            return f"Incoming QC #{self.id}"
        return f"QC for Production #{self.production_order_id}"


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
