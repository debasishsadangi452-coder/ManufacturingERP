import requests
try:
    r = requests.options('http://127.0.0.1:8000/api/token/')
    print(f"Status: {r.status_code}")
    print(f"Headers: {r.headers}")
except Exception as e:
    print(f"Error: {e}")
