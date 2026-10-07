from django.db import migrations


def backfill(apps, schema_editor):
    """Give existing production orders a number, their produced quantity and a
    QA status derived from their checks, so Track B screens read them correctly."""
    ProductionOrder = apps.get_model("production", "ProductionOrder")
    QualityCheck = apps.get_model("quality", "QualityCheck")
    for order in ProductionOrder.objects.all().iterator():
        changed = []
        if not order.order_number:
            order.order_number = f"PRD-{order.id:05d}"
            changed.append("order_number")
        if order.status == "completed" and not order.produced_quantity:
            order.produced_quantity = order.quantity
            changed.append("produced_quantity")
            statuses = set(QualityCheck.objects.filter(production_order_id=order.id).values_list("status", flat=True))
            if "approved" in statuses:
                order.qa_status = "passed"
            elif "pending" in statuses:
                order.qa_status = "pending"
            elif "rejected" in statuses:
                order.qa_status = "failed"
            changed.append("qa_status")
        if changed:
            order.save(update_fields=changed)


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0013_productionorder_notes_productionorder_order_number_and_more"),
        ("quality", "0005_qualitycheck_accepted_quantity_and_more"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
