import os
import time
import requests
import logging

logger = logging.getLogger(__name__)

# 預設使用 FLUX.1-schnell 作為雲端生圖模型
DEFAULT_IMAGE_MODEL = "black-forest-labs/FLUX.1-schnell"
# 預設使用 SVD 作為圖片轉影片模型
DEFAULT_VIDEO_MODEL = "stabilityai/stable-video-diffusion-img2vid-xt"

def get_hf_token():
    """從環境變數載入 Hugging Face API Token"""
    # 優先從 line_bot/.env 或是當前環境變數載入
    token = os.getenv("HF_TOKEN", "").strip()
    return token

def query_hf_inference_api(model_id, payload, hf_token=None, is_binary=True, max_retries=3):
    """通用 Hugging Face Serverless Inference API 呼叫工具"""
    if not hf_token:
        hf_token = get_hf_token()
        
    api_url = f"https://api-inference.huggingface.co/models/{model_id}"
    headers = {}
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"
        
    for attempt in range(max_retries):
        try:
            logger.info(f"Calling HF API: {model_id} (Attempt {attempt+1}/{max_retries})")
            # 如果是圖片 bytes (SVD 模型)，直接傳遞 data，否則傳遞 json
            if isinstance(payload, bytes):
                response = requests.post(api_url, headers=headers, data=payload, timeout=90)
            else:
                response = requests.post(api_url, headers=headers, json=payload, timeout=90)
            
            # 處理模型正在加載中的 503 情況
            if response.status_code == 503:
                try:
                    err_json = response.json()
                    estimated_time = err_json.get("estimated_time", 20)
                    logger.warning(f"Model {model_id} is loading. Waiting for {estimated_time}s...")
                    time.sleep(min(estimated_time, 15))
                    continue
                except Exception:
                    time.sleep(10)
                    continue
                    
            if response.status_code == 401:
                raise ValueError("Hugging Face API 認證失敗，請檢查 HF_TOKEN 是否正確設定。")
                
            response.raise_for_status()
            
            if is_binary:
                return response.content
            else:
                return response.json()
                
        except requests.exceptions.RequestException as e:
            logger.error(f"HF API request failed: {e}")
            if attempt == max_retries - 1:
                raise RuntimeError(f"Hugging Face API 呼叫失敗: {str(e)}")
            time.sleep(3)
            
    raise RuntimeError("Hugging Face API 呼叫逾時或暫停服務。")

def generate_image(prompt, hf_token=None):
    """
    使用 FLUX.1-schnell 模型生成圖片
    回傳: 圖片的二進位 bytes
    """
    payload = {
        "inputs": prompt,
        "parameters": {
            "num_inference_steps": 4, # FLUX.1-schnell 只需要 4 步即可產出高畫質圖片
            "guidance_scale": 3.5
        }
    }
    return query_hf_inference_api(DEFAULT_IMAGE_MODEL, payload, hf_token, is_binary=True)

def generate_video(image_bytes, hf_token=None):
    """
    使用 Stable Video Diffusion 模型將圖片轉為短影片
    輸入: 圖片的二進位 bytes
    回傳: 影片的二進位 bytes
    """
    # 由於 SVD 是影像輸入，我們需要直接將圖片的二進位作為 POST payload 送過去
    # 有些 HF inference API 支援直接傳圖片 binary
    return query_hf_inference_api(DEFAULT_VIDEO_MODEL, image_bytes, hf_token, is_binary=True)
