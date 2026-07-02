from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.text import slugify


class Company(models.Model):
    """A registered tenant. Every user and subscription belongs to a company."""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, help_text="Used in usernames: name.role@slug")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name).replace('-', '')
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class User(AbstractUser):
    ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('hr', 'HR'),
        ('store', 'Store'),
        ('production', 'Production'),
        ('quality', 'Quality'),
        ('sales', 'Sales'),
        ('finance', 'Finance'),
    ]

    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    company = models.ForeignKey(
        Company, null=True, blank=True, on_delete=models.CASCADE, related_name='users'
    )
    auto_approve_limit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0.00,
        help_text="Procurement orders below this amount will be auto-approved for this user."
    )


def generate_username(first_name: str, role: str, company: Company) -> str:
    """Build the company username convention: firstname.role@companyslug.
    Appends a number on collision (john2.production@acme)."""
    base = ''.join(ch for ch in first_name.lower() if ch.isalnum()) or 'user'
    username = f"{base}.{role}@{company.slug}"
    n = 2
    while User.objects.filter(username=username).exists():
        username = f"{base}{n}.{role}@{company.slug}"
        n += 1
    return username


class CompanySubscription(models.Model):
    """One subscription per company. The plan controls which modules and limits
    the tenant has purchased; user roles still control daily access."""

    PLAN_CHOICES = [
        ('starter', 'Starter'),
        ('standard', 'Standard'),
        ('professional', 'Professional'),
        ('premium_ai', 'Premium AI'),
    ]
    STATUS_CHOICES = [
        ('trial', 'Trial'),
        ('active', 'Active'),
        ('past_due', 'Past Due'),
        ('cancelled', 'Cancelled'),
    ]

    company = models.OneToOneField(
        Company, null=True, blank=True, on_delete=models.CASCADE, related_name='subscription'
    )
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    user_limit = models.IntegerField(null=True, blank=True, help_text="Null = unlimited")
    warehouse_limit = models.IntegerField(null=True, blank=True)
    production_line_limit = models.IntegerField(null=True, blank=True)
    ai_monthly_message_limit = models.IntegerField(default=0)
    ai_messages_used = models.IntegerField(default=0)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    onboarding_completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_company(cls, company):
        if company is None:
            return None
        return cls.objects.filter(company=company).first()

    def __str__(self):
        return f"{self.company}: {self.get_plan_display()} ({self.status})"
