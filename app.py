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
    """核心检测逻辑：支持加速、测速、延迟、来源显示"""
    global task_status
    if task_status["stop_requested"]: return None
    
    source_info = get_source_info(url)
    use_hw = os.getenv("USE_HWACCEL", "false").lower() == "true"
    start_time = time.time()
    
    try:
        # 1. 延迟测试
        resp = requests.get(url, stream=True, timeout=5, verify=False)
        latency = int((time.time() - start_time) * 1000)
        
        # 2. 测速测试
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

        # 3. 分辨率探测
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
    except:
        with log_lock:
            task_status["current"] += 1
            task_status["logs"].append(f"❌ {name}: 连接失败 | 🔌{source_info}")
        return None

def run_task():
    global task_status
    if task_status["running"]: return
    task_status.update({"running": True, "stop_requested": False, "current": 0, "success": 0, "logs": [f"🎬 [{datetime.datetime.now().strftime('%H:%M:%S')}] 检测任务启动..."]})
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        raw_channels = []
        for sub_url in config.get("urls", []):
            if not sub_url.strip(): continue
            try:
                r = requests.get(sub_url, timeout=10)
                r.encoding = r.apparent_encoding
                lines = r.text.split('\n')
                if "#EXTINF" in r.text:
                    for i, line in enumerate(lines):
                        if "#EXTINF" in line:
                            name = line.split(',')[-1].strip()
                            for j in range(i+1, min(i+5, len(lines))):
                                u = lines[j].strip()
                                if u.startswith("http"):
                                    raw_channels.append((name, u))
                                    break
                else:
                    for line in lines:
                        if "," in line and "http" in line:
                            parts = line.split(',')
                            if len(parts) >= 2: raw_channels.append((parts[0].strip(), parts[1].strip()))
            except: continue
        
        raw_channels = list(set(raw_channels))
        task_status["total"] = len(raw_channels)
        task_status["logs"].append(f"🔎 解析完成，待测频道: {len(raw_channels)}")

        valid_results = []
        thread_num = int(config.get("threads", 5))
        with ThreadPoolExecutor(max_workers=thread_num) as executor:
            futures = [executor.submit(test_single_channel, n, u) for n, u in raw_channels]
            for f in futures:
                if task_status["stop_requested"]: break
                res = f.result()
                if res: valid_results.append(res)

        if not task_status["stop_requested"]:
            with open(OUTPUT_M3U, 'w', encoding='utf-8') as fm, open(OUTPUT_TXT, 'w', encoding='utf-8') as ft:
                fm.write("#EXTM3U\n")
                for c in valid_results:
                    fm.write(f"#EXTINF:-1,{c['name']} [{c['detail']}]\n{c['url']}\n")
                    ft.write(f"{c['name']},{c['url']}\n")
            task_status["logs"].append(f"🏁 任务结束！有效源: {len(valid_results)}")
        else:
            task_status["logs"].append("🚫 任务已取消。")
    except Exception as e:
        task_status["logs"].append(f"⚠️ 运行时错误: {str(e)}")
    finally:
        task_status["running"] = False

@app.route('/')
def index(): return render_template('index.html')

@app.route('/status')
def get_status(): return jsonify(task_status)

@app.route('/stop')
def stop_task():
    task_status["stop_requested"] = True
    return jsonify({"status": "stopping"})

@app.route('/live.m3u')
def sub_api():
    if not os.path.exists(OUTPUT_M3U): return "尚未生成文件", 404
    with open(OUTPUT_M3U, 'r', encoding='utf-8') as f: content = f.read()
    res = make_response(content)
    res.headers["Content-Type"] = "application/x-mpegurl"
    return res

@app.route('/settings', methods=['POST'])
def save_settings():
    data = request.json
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(data, f)
    update_scheduler()
    return jsonify({"status": "success", "next_run": task_status["next_run"]})

@app.route('/start')
def start_manually():
    if not task_status["running"]: threading.Thread(target=run_task).start()
    return "ok"

@app.route('/download/<path:filename>')
def download(filename): return send_from_directory(DATA_DIR, filename)

def update_scheduler():
    if not os.path.exists(CONFIG_FILE): return
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: config = json.load(f)
        scheduler.remove_all_jobs()
        mode = config.get("schedule_mode", "none")
        if mode == "fixed":
            for t in config.get("fixed_times", "").split(','):
                if ':' in t:
                    h, m = t.strip().split(':')
                    scheduler.add_job(run_task, 'cron', hour=h, minute=m)
        elif mode == "interval":
            scheduler.add_job(run_task, 'interval', hours=int(config.get("interval_hours", 12)))
        jobs = scheduler.get_jobs()
        task_status["next_run"] = jobs[0].next_run_time.strftime('%Y-%m-%d %H:%M:%S') if jobs else "未启用"
    except: task_status["next_run"] = "配置错误"

if __name__ == '__main__':
    update_scheduler()
    app.run(host='0.0.0.0', port=5123)
