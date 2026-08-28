"""Test the Excel import endpoint."""
import requests

# Login
resp = requests.post("http://localhost:8002/api/auth/login", json={"username": "admin", "password": "admin123"})
token = resp.json()["access_token"]

# Import Excel
with open("docs/test/test_qiaojia_quotes.xlsx", "rb") as f:
    resp = requests.post(
        "http://localhost:8002/api/quotes/import",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("test_qiaojia_quotes.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"category": "桥架"},
    )

print(f"Status: {resp.status_code}")
print(resp.json())
