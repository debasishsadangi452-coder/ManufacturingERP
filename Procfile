release: python manage.py migrate
web: gunicorn freshfizz_erp.wsgi --log-file - --worker-class gthread --threads 4 --timeout 300
