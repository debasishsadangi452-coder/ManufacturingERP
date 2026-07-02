from django.db import migrations


def assign_default_company(apps, schema_editor):
    """Existing installs predate multi-company support: put every user (and the
    existing subscription, if any) into a default 'ClearWave' company."""
    User = apps.get_model('accounts', 'User')
    Company = apps.get_model('accounts', 'Company')
    CompanySubscription = apps.get_model('accounts', 'CompanySubscription')

    if not User.objects.filter(company__isnull=True).exists():
        return

    company = Company.objects.filter(slug='clearwave').first()
    if company is None:
        company = Company.objects.create(name='ClearWave', slug='clearwave')

    User.objects.filter(company__isnull=True).update(company=company)
    CompanySubscription.objects.filter(company__isnull=True).update(company=company)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_company_multitenant'),
    ]

    operations = [
        migrations.RunPython(assign_default_company, migrations.RunPython.noop),
    ]
