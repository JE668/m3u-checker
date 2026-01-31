import os, subprocess, json, threading, time, socket, datetime
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- 配置与路径 ---
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
OUTPUT_M3U = os.path.join(DATA_DIR, "iptv.m3u")
OUTPUT_TXT = os.path.join(DATA_DIR, "iptv.txt")

# --- 全局变量与锁 ---
task_status = {
    "running": False, "total": 0, "current": 0, "success": 0,
    "logs": [], "next_run": "未设置"
}

ip_cache = {}
api_lock = threading.Lock()
log_lock = threading.Lock()
scheduler = BackgroundScheduler()
scheduler.start()

def get_ip_info_throttled(url):
    """带频率限制和缓存的 IP 查询"""
    try:
        hostname = urlparse(url).hostname
        ip = socket.gethostbyname(hostname)
        if ip in ip_cache: return ip_cache[ip]
        
        with api_lock:
            time.sleep(1.33)  # 保护 ip-api.com 的 45次/分钟 限制
            res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5).json()
            if res.get('status') == 'success':
                info = f"{res.get('country','')} {res.get('regionName','')} {res.get('city','')} | {res.get('isp','')}"
                ip_cache[ip] = info
                return info
        return "未知位置 | 未知网络"
    except:
        return "IP解析失败"

def test_single_channel(name, url):
    """单频道检测：分辨率、延迟、测速"""
    global task_status
    start_time = time.time()
    try:
        # 1. 延迟测试 (连接到获取首字节时间)
        resp = requests.get(url, stream=True, timeout=5, verify=False)
        latency = int((time.time() - start_time) * 1000)
        
        # 2. 测速 (下载2秒数据)
        total_data = 0
        speed_start = time.time()
        for chunk in resp.iter_content(chunk_size=1024*128):
            total_data += len(chunk)
            if time.time() - speed_start > 2: break
        speed_duration = time.time() - speed_start
        speed_mbps = round((total_data * 8) / (speed_duration * 1024 * 1024), 2)
        resp.close()

        # 3. 分辨率探测 (ffprobe)
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0', '-i', url, '-timeout', '5000000']
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
        video = json.loads(result)['streams'][0]
        res_str = f"{video.get('width','?')}x{video.get('height','?')}"

        # 4. 获取归属地
        geo = get_ip_info_throttled(url)
        
        detail = f"{res_str} | {latency}ms | {speed_mbps}Mbps | {geo}"
        
        with log_lock:
            task_status["success"] += 1
            task_status["current"] += 1
            task_status["logs"].append(f"✅ {name}: {detail}")
        return {"name": name, "url": url, "detail": detail}
    except:
        with log_lock:
            task_status["current"] += 1
        return None

def parse_channels(urls):
    """从多个源解析频道"""
    channels = []
    for sub_url in urls:
        if not sub_url.strip(): continue
        try:
            r = requests.get(sub_url, timeout=10)
            r.encoding = r.apparent_encoding
            text = r.text
            lines = text.split('\n')
            # M3U 逻辑
            if "#EXTINF" in text:
                for i, line in enumerate(lines):
                    if "#EXTINF" in line:
                        name = line.split(',')[-1].strip()
                        for j in range(i+1, min(i+5, len(lines))):
                            u = lines[j].strip()
                            if u.startswith("http"):
                                channels.append((name, u))
                                break
            # TXT 逻辑
            else:
                for line in lines:
                    if "," in line and "http" in line:
                        parts = line.split(',')
                        if len(parts) >= 2:
                            channels.append((parts[0].strip(), parts[1].strip()))
        except: continue
    return list(set(channels)) # 去重

def run_task():
    global task_status
    if task_status["running"]: return
    task_status.update({"running": True, "current": 0, "success": 0, "logs": [f"🚀 任务启动: {datetime.datetime.now().strftime('%H:%M:%S')}"]})
    
    if not os.path.exists(CONFIG_FILE):
        task_status["running"] = False
        return

    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    
    raw_list = parse_channels(config.get("urls", []))
    task_status["total"] = len(raw_list)
    task_status["logs"].append(f"📦 解析完成，共 {len(raw_list)} 个待测频道")

    valid_results = []
    thread_num = int(config.get("threads", 5))
    
    with ThreadPoolExecutor(max_workers=thread_num) as executor:
        futures = [executor.submit(test_single_channel, n, u) for n, u in raw_list]
        for f in futures:
            res = f.result()
            if res: valid_results.append(res)

    # 保存结果
    with open(OUTPUT_M3U, 'w', encoding='utf-8') as fm, open(OUTPUT_TXT, 'w', encoding='utf-8') as ft:
        fm.write("#EXTM3U\n")
        for c in valid_results:
            fm.write(f"#EXTINF:-1,{c['name']} [{c['detail']}]\n{c['url']}\n")
            ft.write(f"{c['name']},{c['url']}\n")
    
    task_status["logs"].append(f"🏁 任务结束，有效源: {len(valid_results)}")
    task_status["running"] = False

@app.route('/')
def index(): return render_template('index.html')

@app.route('/status')
def get_status(): return jsonify(task_status)

@app.route('/live.m3u')
def sub_url():
    if not os.path.exists(OUTPUT_M3U): return "Not found", 404
    with open(OUTPUT_M3U, 'r', encoding='utf-8') as f: content = f.read()
    res = make_response(content)
    res.headers["Content-Type"] = "application/x-mpegurl"
    return res

@app.route('/settings', methods=['POST'])
def save_settings():
    data = request.json
    with open(CONFIG_FILE, 'w') as f: json.dump(data, f)
    update_scheduler()
    return jsonify({"status": "success", "next_run": task_status["next_run"]})

@app.route('/start')
def start_manually():
    threading.Thread(target=run_task).start()
    return "ok"

@app.route('/download/<path:filename>')
def download(filename): return send_from_directory(DATA_DIR, filename)

def update_scheduler():
    if not os.path.exists(CONFIG_FILE): return
    with open(CONFIG_FILE, 'r') as f: config = json.load(f)
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

if __name__ == '__main__':
    update_scheduler()
    app.run(host='0.0.0.0', port=5123)
