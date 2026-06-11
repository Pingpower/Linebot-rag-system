import os
import re
import json
import requests as req_lib
from bs4 import BeautifulSoup
try:
    from duckduckgo_search import DDGS
    DDG_OK = True
except ImportError:
    DDG_OK = False
from config import logger

LLAMA_URL = os.getenv('LLAMA_SERVER_URL', 'http://127.0.0.1:8080')

def _fetch_url_text(url: str) -> str:
    """抓取 URL 並回傳純文字，最多 8000 字"""
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; LineBotKB/1.0)'}
    r = req_lib.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'lxml')
    # 移除 script/style/nav/footer
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
        tag.decompose()
    text = soup.get_text(separator='\n', strip=True)
    # 合併過多空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:8000]


def _search_web(query: str, max_results: int = 5) -> list[dict]:
    """用 DuckDuckGo HTML 頁面進行高可靠度搜尋，並自動解析與解碼跳轉 URL，防止 API 被擋"""
    from urllib.parse import urlparse, parse_qs
    headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    url = f'https://html.duckduckgo.com/html/?q={query}'
    results = []
    try:
        r = req_lib.get(url, headers=headers, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        
        for body in soup.find_all('div', class_='result__body')[:max_results]:
            title_el = body.find('a', class_='result__url')
            snippet_el = body.find('a', class_='result__snippet')
            if title_el and snippet_el:
                href = title_el.get('href', '')
                if 'uddg=' in href:
                    try:
                        parsed = urlparse(href)
                        qs = parse_qs(parsed.query)
                        if 'uddg' in qs:
                            href = qs['uddg'][0]
                    except Exception:
                        pass
                results.append({
                    'title': title_el.get_text(strip=True),
                    'href': href,
                    'body': snippet_el.get_text(strip=True)
                })
    except Exception as e:
        logger.error(f"DuckDuckGo HTML search failed: {e}")
        # Fallback to duckduckgo-search package
        try:
            with DDGS() as ddg:
                raw_results = list(ddg.text(query, max_results=max_results))
                for r in raw_results:
                    results.append({
                        'title': r.get('title', ''),
                        'href': r.get('href', ''),
                        'body': r.get('body', '')
                    })
        except Exception as e2:
            logger.error(f"Fallback DDGS search also failed: {e2}")
    return results


def _fallback_extract(content: str) -> list[dict]:
    """超強容錯的知識萃取解析器：當 json.loads 失敗時，嘗試用正則與多種模式復原資料"""
    entries = []
    
    # 模式一：解析 JSON-like 區塊（尋找 `{...}` 結構）
    blocks = re.findall(r'\{([^{}]+)\}', content)
    for block in blocks:
        try:
            # 尋找 title (支援雙引號、單引號、或無引號，寬鬆匹配直到逗號或大括號)
            title = ""
            title_match = re.search(r'["\'\s]*title["\'\s]*:\s*["\']?(.*?)["\']?(?:,|\s*\}|\s*"content"\s*:)', block, re.DOTALL | re.IGNORECASE)
            if title_match:
                title = title_match.group(1).strip()
            else:
                t_m = re.search(r'title["\']?\s*:\s*["\']?([^"\',}]+)', block, re.IGNORECASE)
                if t_m:
                    title = t_m.group(1).strip()
            
            # 尋找 content
            cnt = ""
            content_match = re.search(r'["\'\s]*content["\'\s]*:\s*["\']?(.*?)["\']?(?:,|\s*\})', block, re.DOTALL | re.IGNORECASE)
            if content_match:
                cnt = content_match.group(1).strip()
            else:
                c_m = re.search(r'content["\']?\s*:\s*["\']?([^"}]+)', block, re.IGNORECASE)
                if c_m:
                    cnt = c_m.group(1).strip()
            
            # 尋找 tags
            tags = []
            tags_match = re.search(r'tags["\']?\s*:\s*\[(.*?)\]', block, re.IGNORECASE)
            if tags_match:
                tags = [t.strip().replace('"', '').replace("'", "") for t in tags_match.group(1).split(',') if t.strip()]
            
            # 清理引號與轉義字元
            title = re.sub(r'^["\']|["\']$', '', title).replace('\\"', '"').replace("\\'", "'").strip()
            cnt = re.sub(r'^["\']|["\']$', '', cnt).replace('\\"', '"').replace("\\'", "'").strip()
            
            if title and cnt:
                entries.append({"title": title[:15], "content": cnt, "tags": tags})
        except Exception as e:
            logger.debug(f"Block parse failed: {e}")
            
    # 模式二：如果沒有解析到 JSON 物件，嘗試解析 Markdown 清單/項目標題
    if not entries:
        items = re.split(r'(?:\d+\.|\*|-|###)\s+', content)
        for item in items:
            if not item.strip():
                continue
            title_m = re.search(r'(?:標題|Title)\s*[:：]\s*(.*?)(?:\n|$)', item, re.IGNORECASE)
            content_m = re.search(r'(?:說明|內容|Content|Detail)\s*[:：]\s*(.*?)(?:\n\s*(?:標籤|Tags)|$)', item, re.DOTALL | re.IGNORECASE)
            tags_m = re.search(r'(?:標籤|Tags)\s*[:：]\s*(.*?)(?:\n|$)', item, re.IGNORECASE)
            
            if title_m and content_m:
                t = title_m.group(1).strip()
                c = content_m.group(1).strip()
                t = re.sub(r'^["\']|["\']$', '', t).strip()
                c = re.sub(r'^["\']|["\']$', '', c).strip()
                
                tags = []
                if tags_m:
                    tags = [tg.strip().replace('"', '').replace("'", "") for tg in re.split(r'[,，、\s]+', tags_m.group(1)) if tg.strip()]
                entries.append({"title": t[:15], "content": c, "tags": tags})
                
    return entries


def _llm_extract(raw_text: str, hint: str = '') -> list[dict]:
    """呼叫配置的 LLM (本地/Gemini/NVIDIA NIM/OpenRouter)，從原始文字萃取知識條目 JSON"""
    prompt = f"""你是知識庫整理助手。請從以下文字中萃取出所有重要且清晰的知識條目（如果是商品、服務或 FAQ 介紹，請將每款商品或每個問答獨立建立一條條目，總數在 2-8 個之間）。
{f'重點提示：{hint}' if hint else ''}

【原始文字】
{raw_text[:16000]}

【極重要指令】
請直接且僅回傳 JSON 陣列，直接以 [ 開頭並以 ] 結尾。格式如下：
[
  {{"title": "條目標題（10字以內）", "content": "詳細說明內容（100-300字）", "tags": ["標籤1", "標籤2"]}},
  ...
]

【注意事項】
1. 嚴禁在 JSON 的字串值內部使用未逸出的雙引號。若字串值內有雙引號，必須寫成 \\\"。
2. 請直接輸出 JSON 陣列，不要加入任何解釋文字或 Markdown 外殼包裝。"""

    provider = os.getenv('KNOWLEDGE_LLM_PROVIDER', 'local').lower().strip()
    
    def do_request(prov):
        headers = {'Content-Type': 'application/json'}
        if prov == 'gemini':
            api_key = os.getenv('GEMINI_API_KEY', '').strip()
            if not api_key:
                raise ValueError("GEMINI_API_KEY is missing")
            model = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            
            payload = {
                'contents': [{'parts': [{'text': prompt}]}],
                'generationConfig': {
                    'temperature': 0.2,
                    'maxOutputTokens': 4000,
                }
            }
        else:
            if prov == 'nvidia':
                api_key = os.getenv('NVIDIA_NIM_API_KEY', '').strip()
                if not api_key:
                    raise ValueError("NVIDIA_NIM_API_KEY is missing")
                model = os.getenv('NVIDIA_NIM_MODEL', 'meta/llama-3.1-405b-instruct').strip()
                url = 'https://integrate.api.nvidia.com/v1/chat/completions'
                headers['Authorization'] = f'Bearer {api_key}'
            elif prov == 'openrouter':
                api_key = os.getenv('OPENROUTER_API_KEY', '').strip()
                if not api_key:
                    raise ValueError("OPENROUTER_API_KEY is missing")
                model = os.getenv('OPENROUTER_MODEL', 'google/gemini-2.5-flash').strip()
                url = 'https://openrouter.ai/api/v1/chat/completions'
                headers['Authorization'] = f'Bearer {api_key}'
                headers['HTTP-Referer'] = 'https://github.com/Pingpower/-linebot-rag-system'
                headers['X-Title'] = 'Linebot RAG System'
            else:
                model = 'local'
                url = f'{LLAMA_URL}/v1/chat/completions'

            payload = {
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.2,
                'max_tokens': 4000,
                'stream': False,
            }

        # Retry logic for handling temporary 503/429 errors from LLM APIs
        max_retries = 4
        retry_delay = 2.0  # initial delay in seconds
        resp = None

        for attempt in range(max_retries):
            try:
                logger.info("Extracting knowledge using LLM Provider: %s (model: %s) [Attempt %d/%d]", 
                            prov, model if prov == 'gemini' else payload.get('model'), attempt + 1, max_retries)
                resp = req_lib.post(url, json=payload, headers=headers, timeout=120)
                resp.raise_for_status()
                break  # Success, exit retry loop
            except Exception as e:
                # Determine if the error is retryable (exclude 400, 401, 403, 404 client errors)
                is_retryable = True
                if hasattr(e, 'response') and e.response is not None:
                    status_code = e.response.status_code
                    if status_code in [400, 401, 403, 404]:
                        is_retryable = False

                if is_retryable and attempt < max_retries - 1:
                    logger.warning("LLM API request failed (attempt %d/%d) with error: %s. Retrying in %.1f seconds...", 
                                   attempt + 1, max_retries, str(e), retry_delay)
                    time.sleep(retry_delay)
                    retry_delay *= 1.5  # exponential backoff
                else:
                    logger.exception("LLM API request failed permanently after %d attempts. Error: %s", attempt + 1, str(e))
                    raise e
        
        if prov == 'gemini':
            raw_content = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        else:
            raw_content = resp.json()['choices'][0]['message']['content'].strip()

        content = raw_content
        content = re.sub(r'<think>[\s\S]*?(</think>|$)', '', content).strip()
        content = re.sub(r'```json\s*', '', content)
        content = re.sub(r'```\s*', '', content)
        content = content.strip()

        start = content.find('[')
        end = content.rfind(']')
        if start != -1 and end != -1 and end > start:
            content = content[start:end+1]
        
        content = re.sub(r',\s*\]', ']', content)
        content = re.sub(r',\s*\}', '}', content)

        def escape_json_newlines(s: str) -> str:
            res_chars = []
            in_str = False
            esc = False
            for char in s:
                if char == '"' and not esc:
                    in_str = not in_str
                if char == '\\' and not esc:
                    esc = True
                else:
                    esc = False
                
                if char == '\n' and in_str:
                    res_chars.append('\\n')
                elif char == '\r' and in_str:
                    res_chars.append('\\r')
                else:
                    res_chars.append(char)
            return "".join(res_chars)

        content = escape_json_newlines(content)

        try:
            return json.loads(content, strict=False)
        except Exception as json_err:
            logger.error("JSON parsing failed for provider %s: %s", prov, str(json_err))
            logger.error("Raw content: %s", raw_content)
            try:
                entries = _fallback_extract(content)
                if entries:
                    logger.info("Fallback parser successfully recovered %d entries", len(entries))
                    return entries
            except Exception as fb_err:
                logger.error("Fallback parser also failed: %s", str(fb_err))
            raise json_err

    try:
        return do_request(provider)
    except Exception as primary_err:
        logger.warning("Primary LLM extraction failed: %s", str(primary_err))
        gemini_key = os.getenv('GEMINI_API_KEY', '').strip()
        if provider != 'gemini' and gemini_key:
            logger.info("Triggering automatic fallback to Gemini...")
            try:
                return do_request('gemini')
            except Exception as fallback_err:
                logger.error("Fallback to Gemini also failed: %s", str(fallback_err))
        
        raise ValueError(f"無法解析 AI 回傳的資料格式：{str(primary_err)}。請縮短文字或提供更簡單的內容後再試。")
