"""
Operations the ERP never routes through ``queue_auto_post`` (vendor payments) are picked up here, so the Indian
ledger sees them without any change to procurement code. Companies on the existing system are untouched.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="procurement.VendorPayment", dispatch_uid="inacc_vendor_payment")
def on_vendor_payment(sender, instance, created, **kwargs):
    if not created:
        return
    from . import hooks
    company = instance.company or getattr(instance.vendor, "company", None)
    hooks.intercept("vendor_payment", company, instance.id, None)
