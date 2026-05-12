from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_auditlog'),
    ]

    operations = [
        migrations.CreateModel(
            name='DataChangeEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('model_name', models.CharField(max_length=100)),
                ('record_id', models.IntegerField()),
                ('action', models.CharField(max_length=20)),
                ('payload', models.JSONField(default=dict)),
                ('visible_to', models.JSONField(default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
    ]
