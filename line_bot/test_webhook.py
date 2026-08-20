import os
import json
import hmac
import hashlib
import base64
import requests
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment
load_dotenv('/home/pipadmin/文件/line_bot/.env')
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Fetch a valid active company slug
res = supabase.table('companies').select('*').eq('is_active', True).limit(1).execute()
companies = res.data or []
if not companies:
    print("Error: No active company found in Supabase.")
    exit(1)

company = companies[0]
slug = company['slug']
secret = company['line_channel_secret']
print(f"Testing company slug: {slug}")
print(f"Company ID: {company['id']}")
print(f"Channel secret: {secret}")

# Construct mock LINE message payload (with quoteToken to satisfy LINE v3 SDK model validation)
payload = {
    "destination": "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "events": [
        {
            "replyToken": "mockReplyToken1234567890",
            "type": "message",
            "mode": "active",
            "timestamp": 1690000000000,
            "source": {
                "type": "user",
                "userId": "Uf705f8ffb4286954c9e05db19c16f88f"
            },
            "webhookEventId": "mockWebhookEventId123",
            "deliveryContext": {
                "isRedelivery": False
            },
            "message": {
                "id": "12345678",
                "type": "text",
                "text": "請問你們的服務如何計費與申請？",
                "quoteToken": "mockQuoteToken123456"
            }
        }
    ]
}

body = json.dumps(payload)

# Compute signature
hash_val = hmac.new(secret.encode('utf-8'), body.encode('utf-8'), hashlib.sha256).digest()
signature = base64.b64encode(hash_val).decode('utf-8')

# Send POST request
url = f"http://127.0.0.1:5000/callback/{slug}"
headers = {
    "Content-Type": "application/json",
    "X-Line-Signature": signature
}

print(f"Sending POST to {url}...")
r = requests.post(url, headers=headers, data=body)
print(f"Status Code: {r.status_code}")
print(f"Response Body: {r.text}")
