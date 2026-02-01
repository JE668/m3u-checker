import os, subprocess, json, threading, time, socket, datetime
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- 路径与配置文件 ---
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
OUTPUT_M3U = os.path.join(DATA_DIR, "iptv.m3u")
OUTPUT_TXT = os.path.join(DATA_DIR, "iptv.txt")

# --- 全局状态控制 ---
task_status = {
    "running": False, 
    "stop_requested": False,
    "total": 0, 
    "current": 0, 
    "success": 0,
    "logs": [], 
    "next_run": "未启用"
}

ip_cache = {}
api_lock = threading.Lock()
log_lock = threading.Lock()
scheduler = BackgroundScheduler()
scheduler.start()

def get_source_info(url):
    """提取 URL 中的 IP/域名 和 端口"""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
        if not port:
            port = 443 if parsed.scheme == 'https' else 80
        return f"{host}:{port}"
    except:
        return "未知接口"

def get_ip_info_throttled(url):
    """带 1.33s 频率限制的地理位置查询"""
    try:
        hostname = urlparse(url).hostname
        ip = socket.gethostbyname(hostname)
        if ip in ip_cache: return ip_cache[ip]
        
        with api_lock:
            # 即使多线程运行，也强制间隔 1.33s 保护 API
            time.sleep(1.33)
            res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5).json()
            if res.get('status') == 'success':
                info = f"📍{res.get('country','')} {res.get('regionName','')} {res.get('city','')} | 🏢{res.get('isp','')}"
                ip_cache[ip] = info
                return info
        return "📍未知位置"
    except:
        return "📍解析失败"

def test_single_channel(name, url):
    """核心检测逻辑：支持 Intel GPU 加速、测速、延迟、来源显示"""
    global task_status
    if task_status["stop_requested"]: return None
    
    source_info = get_source_info(url)
    use_hw = os.getenv("USE_HWACCEL", "false").lower() == "true"
    start_time = time.time()
    
    try:
        # 1. 延迟测试 (TTFB)
        resp = requests.get(url, stream=True, timeout=5, verify=False)
        latency = int((time.time() - start_time) * 1000)
        
        # 2. 测速测试 (下载 2 秒数据)
        total_data = 0
        speed_start = time.time()
        for chunk in resp.iter_content(chunk_size=1024*128):
            if task_status["stop_requested"]: 
                resp.close()
                return None
            total_data += len(chunk)
            if time.time() - speed_start > 2: break
        speed_duration = time.time() - speed_start
        speed_mbps = round((total_data * 8) / (speed_duration * 1024 * 1024), 2)
        resp.close()

        # 3. 分辨率探测 (可选 Intel VAAPI 硬件加速)
        hw_args = ['-hwaccel', 'vaapi', '-hwaccel_device', '/dev/dri/renderD128'] if use_hw else []
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0'] + hw_args + ['-i', url, '-timeout', '5000000']
        
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
        video = json.loads(result)['streams'][0]
        res_str = f"{video.get('width','?')}x{video.get('height','?')}"
        
        # 4. 获取地理位置
        geo = get_ip_info_throttled(url)
        hw_tag = "💎" if use_hw else "💻"
        
        detail = f"{hw_tag}{res_str} | ⏱️{latency}ms | 🚀{speed_mbps}Mbps | {geo} | 🔌{source_info}"
        
        with log_lock:
            task_status["success"] += 1
            task_status["current"] += 1
            task_status["logs"].append(f"✅ {name}: {detail}")
        
        return {"name": name, "url": url, "detail": detail}

    except Exception as e:
        with log_lock:
            task_status["current"
