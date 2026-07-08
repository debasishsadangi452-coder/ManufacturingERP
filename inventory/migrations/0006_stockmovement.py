from django.conf import settings
from django.db import migrations


class Migration(migrations.Migration):
    """Historical no-op.

    0001_initial was regenerated at some point and now creates StockMovement
    itself, so the original CreateModel here crashed every fresh database
    (including the test database) with "table already exists". Databases
    that ran the old version already have this migration recorded as
    applied, so emptying the operations is safe for them too.
    """

    dependencies = [
        ('inventory', '0005_inventoryrequest_procuring'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = []
