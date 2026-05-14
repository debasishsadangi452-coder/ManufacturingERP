release: python manage.py migrate
web: gunicorn freshfizz_erp.wsgi --log-file - -w 4 --timeout 60
