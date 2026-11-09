from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.text import slugify


class Company(models.Model):
    """A registered tenant. Every user and subscription belongs to a company."""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, help_text="Used in usernames: name.role@slug")
    created_at = models.DateTimeField(auto_now_add=True)
    # Platform super admin only shows companies flagged here; everything else is
    # hidden from it (and untouchable through it) until the owner decides otherwise.
    superadmin_visible = models.BooleanField(default=False)
    # Set while/after a demo company is cloned by the super admin ("" for normal companies).
    DEMO_STATUS_CHOICES = [("", "n/a"), ("creating", "Creating"), ("ready", "Ready"), ("failed", "Failed")]
    demo_status = models.CharField(max_length=10, blank=True, default="", choices=DEMO_STATUS_CHOICES)
    demo_error = models.TextField(blank=True, default="")

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
        # Platform owner, above every company: manages users, no tenant data access.
        ('superadmin', 'Super Admin'),
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


class LoginEvent(models.Model):
    """One sign-in attempt (successful or not) with the network address it came from.

    `username` is what was typed, so failed attempts for unknown accounts are kept
    too. `forwarded_for` keeps the raw X-Forwarded-For chain: `ip_address` is the
    best guess at the client, but the chain is the evidence if it is ever disputed."""

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="login_events")
    username = models.CharField(max_length=254, blank=True, default="")
    success = models.BooleanField(default=False, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    forwarded_for = models.CharField(max_length=500, blank=True, default="")
    user_agent = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.username or '?'} {'ok' if self.success else 'failed'} from {self.ip_address or 'unknown'}"


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
