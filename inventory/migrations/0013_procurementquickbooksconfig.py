import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_alter_company_id_alter_companysubscription_id_and_more'),
        ('inventory', '0012_salesquickbooksconfig'),
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
                    ('vendor_mapping', 'Awaiting Vendor Mapping'),
                    ('procurement_config', 'Awaiting Procurement Configuration'),
                    ('completed', 'Completed'),
                ],
                default='classification',
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name='ProcurementQuickBooksConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('vendors_mapped', models.BooleanField(default=False)),
                ('purchase_trigger', models.CharField(choices=[('goods_receipt', 'At goods receipt (material received into warehouse)'), ('po_creation', 'At PO creation / approval')], default='goods_receipt', max_length=20)),
                ('cost_source', models.CharField(choices=[('po_price', 'PO-negotiated price'), ('invoice_price', 'Actual vendor invoice amount (if it differs from PO)')], default='po_price', max_length=20)),
                ('payables_disclaimer_acknowledged', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='procurement_qb_config', to='accounts.company')),
            ],
        ),
    ]
