from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import json

parser = WebhookParser("86576ad2ea8277199fda54db669acaaf")
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
                "text": "您好，請問貴公司的產品有哪些主要功能與計費方案？"
            }
        }
    ]
}

body = json.dumps(payload)
# Compute signature (just any valid or invalid, we bypass parser checks if we do it directly or use parser.parse)
# Actually WebhookParser.parse checks signature unless we mock it or compute it correctly.
# Let's compute it correctly:
import hmac, hashlib, base64
hash_val = hmac.new(b"86576ad2ea8277199fda54db669acaaf", body.encode('utf-8'), hashlib.sha256).digest()
signature = base64.b64encode(hash_val).decode('utf-8')

events = parser.parse(body, signature)
print("Parsed events:", events)
for ev in events:
    print("Event Class:", ev.__class__)
    print("isinstance(ev, MessageEvent):", isinstance(ev, MessageEvent))
    print("hasattr(ev, 'message'):", hasattr(ev, 'message'))
    if hasattr(ev, 'message'):
        print("Message Class:", ev.message.__class__)
        print("isinstance(ev.message, TextMessageContent):", isinstance(ev.message, TextMessageContent))
