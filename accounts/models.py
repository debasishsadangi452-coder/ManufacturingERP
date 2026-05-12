from django.contrib.auth.models import AbstractUser
from django.db import models

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
    auto_approve_limit = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        default=0.00,
        help_text="Procurement orders below this amount will be auto-approved for this user."
    )
    