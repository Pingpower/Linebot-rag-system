import os
import requests
from dotenv import load_dotenv

load_dotenv()
GEMINI_KEY = os.getenv('GEMINI_API_KEY', '')
url = f"https://generativelanguage.googleapis.com/v1/models?key={GEMINI_KEY}"

try:
    r = requests.get(url, timeout=10)
    print("V1 Status:", r.status_code)
    if r.status_code == 200:
        models = r.json().get('models', [])
        for m in models:
            print(m['name'], m.get('supportedGenerationMethods'))
    else:
        print(r.text)
except Exception as e:
    print("Failed:", e)
