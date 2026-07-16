# Context

## Working Directory
`/home/pipadmin/文件/line_bot`

## Target Files
- `/home/pipadmin/文件/line_bot/app.py`
- Local services (Supabase, local LLM port 8080, etc.)

## Current Knowledge
- Users receive the error response: "抱歉，AI 服務暫時無法使用，請稍後再試。" when using the LINE bot.
- This is likely caused by an exception in the LLM call, API client setup, OpenAI timeout, or Supabase.
