import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_alter_company_id_alter_companysubscription_id_and_more'),
        ('inventory', '0011_item_bom_completed_item_classification_completed_at_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='quickbooksonboarding',
            name='status',
            field=models.CharField(
                choices=[
                    ('classification', 'Awaiting Item Classification'),
                    ('bom_setup', 'Awaiting BOM Setup'),
                    ('customer_mapping', 'Awaiting Customer Mapping'),
                    ('sales_config', 'Awaiting Sales Configuration'),
                    ('completed', 'Completed'),
                ],
                default='classification',
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name='SalesQuickBooksConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('customers_mapped', models.BooleanField(default=False)),
                ('sale_trigger', models.CharField(choices=[('shipment', 'At shipment / dispatch'), ('confirmation', 'At order confirmation')], default='shipment', max_length=20)),
                ('default_doc_type', models.CharField(choices=[('invoice', 'Invoice (bill customer, payment later)'), ('sales_receipt', 'Sales Receipt (customer pays immediately)')], default='invoice', max_length=20)),
                ('price_disclaimer_acknowledged', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='sales_qb_config', to='accounts.company')),
            ],
        ),
    ]
