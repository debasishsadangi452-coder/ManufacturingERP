import os
from django.core.management.base import BaseCommand
from django.apps import apps
from django.db import connection

class Command(BaseCommand):
    help = 'Clears all application data except for users, groups, and permissions.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Clearing all ERP data (preserving users)..."))

        # Explicit order to handle foreign key dependencies
        models_to_clear = [
            'logistics.DeliveryRoute',
            'logistics.Vehicle',
            'sales.Shipment',
            'sales.SalesOrderItem',
            'sales.SalesOrder',
            'sales.Customer',
            'quality.QualityCheck',
            'inventory.InventoryRequest',
            'production.ProductionOrder',
            'production.RecipeIngredient',
            'production.Recipe',
            'maintenance.MaintenanceTask',
            'maintenance.Equipment',
            'procurement.GoodsReceipt',
            'procurement.PurchaseOrderItem',
            'procurement.PurchaseOrder',
            'procurement.Vendor',
            'inventory.StockMovement',
            'inventory.Batch',
            'inventory.Stock',
            'inventory.Item',
            'inventory.Warehouse',
            'workforce.Shift',
            'workforce.TrainingProgram',
            'workforce.Employee',
            'core.Notification'
        ]
        
        from django.db import transaction
        
        try:
            with transaction.atomic():
                for model_path in models_to_clear:
                    try:
                        model = apps.get_model(model_path)
                        count = model.objects.count()
                        if count > 0:
                            self.stdout.write(f"Clearing {model_path} ({count} records)...")
                            model.objects.all().delete()
                        else:
                            self.stdout.write(f"Model {model_path} is already empty.")
                    except LookupError:
                        self.stdout.write(self.style.WARNING(f"Model {model_path} not found, skipping."))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"Error clearing {model_path}: {e}"))
        except Exception as outer_e:
            self.stdout.write(self.style.ERROR(f"Transaction failed: {outer_e}"))

        self.stdout.write(self.style.SUCCESS("Successfully cleared all application data."))
        self.stdout.write(self.style.MIGRATE_HEADING("Note: Run seeding scripts (setup_roles.py, etc.) to restore necessary metadata."))
