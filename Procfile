release: python manage.py migrate
web: gunicorn freshfizz_erp.wsgi --log-file - --timeout 120
