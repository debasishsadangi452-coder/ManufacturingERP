import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('logistics', '0002_driver_shipment'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='route',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='logistics.deliveryroute'),
        ),
    ]
