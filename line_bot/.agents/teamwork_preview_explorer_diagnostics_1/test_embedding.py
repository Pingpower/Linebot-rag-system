import os
import requests
from dotenv import load_dotenv

# Load env from parent directory
load_dotenv('/home/pipadmin/文件/line_bot/.env')
gemini_key = os.getenv("GEMINI_API_KEY", "")

print(f"Loaded GEMINI_API_KEY: {gemini_key[:5]}...{gemini_key[-5:] if len(gemini_key) > 10 else ''}")

# Test v1beta
url_v1beta = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={gemini_key}"
payload = {
    "model": "models/text-embedding-004",
    "content": {"parts": [{"text": "Hello World"}]}
}
print("\nTesting v1beta...")
try:
    r = requests.post(url_v1beta, json=payload, timeout=10)
    print(f"Status: {r.status_code}")
    print(f"Response: {r.text}")
except Exception as e:
    print(f"Error: {e}")

# Test v1
url_v1 = f"https://generativelanguage.googleapis.com/v1/models/text-embedding-004:embedContent?key={gemini_key}"
print("\nTesting v1...")
try:
    r = requests.post(url_v1, json=payload, timeout=10)
    print(f"Status: {r.status_code}")
    print(f"Response: {r.text}")
except Exception as e:
    print(f"Error: {e}")
