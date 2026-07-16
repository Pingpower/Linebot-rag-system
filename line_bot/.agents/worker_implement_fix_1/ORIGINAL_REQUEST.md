## 2026-07-14T12:33:35Z
You are the Worker agent responsible for implementing the LINE Bot AI reply fix.
Your working directory is: /home/pipadmin/文件/line_bot/.agents/worker_implement_fix_1

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Scope / Task Details:
1. Examine /home/pipadmin/文件/line_bot/semantic_cache.py (Line 24) and /home/pipadmin/文件/line_bot/sync_embeddings.py (Line 26).
   Update the url strings: change "v1beta" to "v1" for the Gemini models API endpoint.
2. Examine the systemd user service file /home/pipadmin/.config/systemd/user/linebot-llama.service.
   Change "--ctx-size 4096" to "--ctx-size 8192" in the ExecStart command.
3. Reload systemd user config and restart services:
   - Run: systemctl --user daemon-reload
   - Run: systemctl --user restart linebot-llama.service
   - Run: systemctl --user restart linebot-flask.service
4. Verify the fixes:
   - Run `python3 /home/pipadmin/文件/line_bot/sync_embeddings.py` and verify it runs successfully.
   - Write a test script (e.g., test_webhook.py) that:
     a) Queries Supabase to fetch a valid company slug and its channel secret.
     b) Generates a mock LINE MessageEvent payload (text message).
     c) Computes the HMAC-SHA256 signature of the payload using the company's line_channel_secret, base64-encoded.
     d) Sends a POST request to http://127.0.0.1:5000/callback/{slug} with the X-Line-Signature header.
     e) Inspects the logs (e.g. /home/pipadmin/文件/flask.log or stdout) to verify the LLM request is successfully processed, does not overflow context, and returns a successful response without the "AI 服務暫時無法使用" error.
5. Provide a handoff report listing the commands run, the files changed (with diffs), and verification logs. Write it to handoff.md in your working directory and notify the parent (ID: 8a89e8e2-0763-4b1c-b351-6f9021cbf9a3) via send_message.
Remember to update progress.md in your working directory to signal liveness.
