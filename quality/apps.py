from django.apps import AppConfig


class QualityConfig(AppConfig):
    name = "quality"

    def ready(self):
        import quality.signals
