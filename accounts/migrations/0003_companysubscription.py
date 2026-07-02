from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_user_auto_approve_limit'),
    ]

    operations = [
        migrations.CreateModel(
            name='CompanySubscription',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('plan', models.CharField(choices=[('starter', 'Starter'), ('standard', 'Standard'), ('professional', 'Professional'), ('premium_ai', 'Premium AI')], max_length=20)),
                ('status', models.CharField(choices=[('trial', 'Trial'), ('active', 'Active'), ('past_due', 'Past Due'), ('cancelled', 'Cancelled')], default='active', max_length=20)),
                ('user_limit', models.IntegerField(blank=True, help_text='Null = unlimited', null=True)),
                ('warehouse_limit', models.IntegerField(blank=True, null=True)),
                ('production_line_limit', models.IntegerField(blank=True, null=True)),
                ('ai_monthly_message_limit', models.IntegerField(default=0)),
                ('ai_messages_used', models.IntegerField(default=0)),
                ('current_period_start', models.DateTimeField(blank=True, null=True)),
                ('current_period_end', models.DateTimeField(blank=True, null=True)),
                ('onboarding_completed', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
