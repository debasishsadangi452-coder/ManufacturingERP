from django.db import models

class Driver(models.Model):
    LICENSE_CHOICES = [
        ("LMV", "Light Motor Vehicle"),
        ("HMV", "Heavy Motor Vehicle"),
        ("TRANSPORT", "Transport Vehicle"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=200)
    license_number = models.CharField(max_length=100, unique=True)
    license_type = models.CharField(max_length=20, choices=LICENSE_CHOICES, default="HMV")
    phone = models.CharField(max_length=20, blank=True)
    is_available = models.BooleanField(default=True)
    experience_years = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.name} ({self.license_number})"

class Vehicle(models.Model):
    STATUS_CHOICES = [
        ("available", "Available"),
        ("in-use", "In Use"),
        ("maintenance", "Maintenance"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=200)
    vehicle_type = models.CharField(max_length=100)  # e.g. Refrigerated, Standard
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="available")
    driver = models.CharField(max_length=100, blank=True)
    current_location = models.CharField(max_length=200, blank=True)
    fuel_level = models.IntegerField(default=100)
    next_maintenance = models.DateField(null=True, blank=True)
    capacity = models.IntegerField(default=0)
    current_load = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.name} ({self.id})"

class DeliveryRoute(models.Model):
    STATUS_CHOICES = [
        ("planned", "Planned"),
        ("active", "Active"),
        ("completed", "Completed"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=200)
    stops = models.IntegerField(default=0)
    distance = models.CharField(max_length=50)
    estimated_time = models.CharField(max_length=50)
    assigned_vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="planned")
    efficiency = models.IntegerField(default=0)

    def __str__(self):
        return self.name

class Shipment(models.Model):
    STATUS_CHOICES = [
        ("preparing", "Preparing"),
        ("loading", "Loading"),
        ("in-transit", "In Transit"),
        ("delivered", "Delivered"),
        ("delayed", "Delayed"),
    ]

    PRIORITY_CHOICES = [
        ("normal", "Normal"),
        ("high", "High"),
        ("urgent", "Urgent"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    order_number = models.CharField(max_length=100, unique=True, blank=True)
    customer = models.CharField(max_length=200)
    destination = models.CharField(max_length=300)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="preparing")
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default="normal")
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True)
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True)
    route = models.ForeignKey(DeliveryRoute, on_delete=models.SET_NULL, null=True, blank=True)
    departure_time = models.DateTimeField(null=True, blank=True)
    estimated_arrival = models.DateTimeField(null=True, blank=True)
    progress = models.IntegerField(default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.order_number:
            import uuid
            self.order_number = f"SHP-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.order_number} → {self.destination}"
