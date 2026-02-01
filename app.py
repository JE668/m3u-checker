import os, subprocess, json, threading, time, socket, datetime, uuid
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- 路径与存储 ---
DATA_DIR = "/app/data"
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# --- 状态记录 ---
subs_status = {}
ip_cache = {}
api_lock = threading.Lock()
log_lock = threading.Lock()
scheduler = BackgroundScheduler()
scheduler.start()

def load_config():
    if not os.path.exists(CONFIG_FILE): return {"subscriptions": []}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return {"subscriptions": []}

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def get_ip_info(url):
    try:
        hostname = urlparse(url).hostname
        ip = socket.gethostbyname(hostname)
        if ip in ip_cache: return ip_cache[ip]
        with api_lock:
            time.sleep(1.33)
            res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5).json()
            if res.get('status') == 'success':
                info = f"📍{res.get('city','')} | 🏢{res.get('isp','')}"
                ip_cache[ip] = info
                return info
        return "📍未知位置"
    except: return "📍解析失败"

def probe_stream(url, use_hw):
    """
    智能探测：针对 Intel UHD 620 优化。
    即使是 QSV 模式，在 ffprobe 阶段使用 vaapi 映射也是最稳的。
    """
    accel_type = os.getenv("HW_ACCEL_TYPE", "qsv").lower()
    device = os.getenv("QSV_DEVICE") or os.getenv("VAAPI_DEVICE") or "/dev/dri/renderD128"
    
    if use_hw:
        try:
            # 针对 Intel 显卡的强力 QSV/VAAPI 组合命令
            if accel_type in ["quicksync", "qsv"]:
                # QSV 初始化
                hw_args = [
                    '-hwaccel', 'qsv',
                    '-qsv_device', device,
                    '-hwaccel_output_format', 'qsv'
                ]
                icon = "⚡"
            else:
                # 纯 VAAPI 模式
                hw_args = [
                    '-hwaccel', 'vaapi',
                    '-hwaccel_device', device,
                    '-hwaccel_output_format', 'vaapi'
                ]
                icon = "💎"

            # 探测命令，增加 probesize 防止网络流头部过长导致探测失败
            cmd = ['ffprobe', '-v', 'error', '-hide_banner', '-print_format', 'json', 
                   '-show_streams', '-select_streams', 'v:0',
                   '-probesize', '5000000', '-analyzeduration', '5000000'] + hw_args + ['-i', url]
            
            # 使用 subprocess.run 捕获 stderr 用于调试
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if 'streams' in data and len(data['streams']) > 0:
                    return data['streams'][0], icon
            
            # 如果硬件报错，打印到 Docker 后台日志供查验
            print(f"DEBUG HW FAILED: {result.stderr}")
        except Exception as e:
            print(f"DEBUG HW EXCEPTION: {str(e)}")

    # 软件探测回退 (CPU) - 极致兼容性
    cmd_cpu = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0', '-i', url, '-timeout', '5000000']
    try:
        out = subprocess.check_output(cmd_cpu, stderr=subprocess.STDOUT).decode('utf-8')
        return json.loads(out)['streams'][0], "💻"
    except:
        return None, "❌"

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    parsed = urlparse(url)
    source_tag = f"🔌{parsed.hostname}:{parsed.port or (443 if parsed.scheme=='https' else 80)}"
    
    start_time = time.time()
    try:
        # 1. 连接 & 测速
        resp = requests.get(url, stream=True, timeout=5, verify=False)
        latency = int((time.time() - start_time) * 1000)
        total_data, speed_start = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: break
            total_data += len(chunk)
            if time.time() - speed_start > 2: break
        speed = round((total_data * 8) / ((time.time() - speed_start) * 1024 * 1024), 2)
        resp.close()

        # 2. 探测
        video, icon = probe_stream(url, use_hw)
        if not video: raise Exception("Probe failed")
        
        res_str = f"{video.get('width')}x{video.get('height')}"
        geo = get_ip_info(url)
        
        with log_lock:
            status["success"] += 1
            status["current"] += 1
            status["logs"].append(f"✅ {name}: {icon}{res_str} | ⏱️{latency}ms | 🚀{speed}Mbps | {geo} | {source_tag}")
        return {"name": name, "url": url}
    except:
        with log_lock:
            status["current"] += 1
            status["logs"].append(f"❌ {name}: 连接失败 | {source_tag}")
        return None

def run_task(sub_id):
    config = load_config()
    sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub: return

    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0,
        "logs": [f"🎬 [{datetime.datetime.now().strftime('%H:%M:%S')}] 任务启动"]
    }
    
    use_hw = os.getenv("USE_HWACCEL", "false").lower() == "true"
    raw_channels = []
    try:
        r = requests.get(sub["url"], timeout=15)
        r.encoding = r.apparent_encoding
        text = r.text
        if "#EXTINF" in text:
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if "#EXTINF" in line:
                    name = line.split(',')[-1].strip()
                    for j in range(i+1, min(i+5, len(lines))):
                        u = lines[j].strip()
                        if u.startswith("http"):
                            raw_channels.append((name, u)); break
        else:
            for line in text.split('\n'):
                if "," in line and "http" in line:
                    p = line.split(',')
                    if len(p) >= 2: raw_channels.append((p[0].strip(), p[1].strip()))
    except: pass

    raw_channels = list(set(raw_channels))
    subs_status[sub_id]["total"] = len(raw_channels)
    
    valid_list = []
    with ThreadPoolExecutor(max_workers=int(sub.get("threads", 5))) as executor:
        futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
        for f in futures:
            if subs_status[sub_id]["stop_requested"]: break
            res = f.result()
            if res: valid_list.append(res)

    m3u_path = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
    txt_path = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
    with open(m3u_path, 'w', encoding='utf-8') as fm, open(txt_path, 'w', encoding='utf-8') as ft:
        fm.write("#EXTM3U\n")
        for c in valid_list:
            fm.write(f"#EXTINF:-1,{c['name']}\n{c['url']}\n")
            ft.write(f"{c['name']},{c['url']}\n")
            
    subs_status[sub_id]["logs"].append(f"🏁 任务结束，有效源: {len(valid_list)}")
    subs_status[sub_id]["running"] = False

# --- 路由 ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/subs', methods=['GET', 'POST'])
def handle_subs():
    config = load_config()
    if request.method == 'POST':
        new_sub = request.json
        if not new_sub.get("id"):
            new_sub["id"] = str(uuid.uuid4())[:8]
            config["subscriptions"].append(new_sub)
        else:
            for i, s in enumerate(config["subscriptions"]):
                if s["id"] == new_sub["id"]: config["subscriptions"][i] = new_sub
        save_config(config); update_global_scheduler(); return jsonify({"status": "ok"})
    return jsonify(config["subscriptions"])

@app.route('/api/subs/delete/<sub_id>')
def delete_sub(sub_id):
    config = load_config(); config["subscriptions"] = [s for s in config["subscriptions"] if s["id"] != sub_id]
    save_config(config); update_global_scheduler(); return jsonify({"status": "ok"})

@app.route('/api/status/<sub_id>')
def get_status(sub_id):
    return jsonify(subs_status.get(sub_id, {"running": False, "logs": [], "total":0, "current":0, "success":0}))

@app.route('/api/start/<sub_id>')
def start_task(sub_id):
    threading.Thread(target=run_task, args=(sub_id,)).start(); return jsonify({"status": "ok"})

@app.route('/api/stop/<sub_id>')
def stop_task(sub_id):
    if sub_id in subs_status: subs_status[sub_id]["stop_requested"] = True
    return jsonify({"status": "ok"})

@app.route('/sub/<sub_id>.<ext>')
def get_sub_file(sub_id, ext):
    return send_from_directory(OUTPUT_DIR, f"{sub_id}.{ext}")

def update_global_scheduler():
    scheduler.remove_all_jobs()
    config = load_config()
    for sub in config["subscriptions"]:
        sid, mode = sub["id"], sub.get("schedule_mode", "none")
        if mode == "fixed":
            for t in sub.get("fixed_times", "").split(','):
                if ':' in t:
                    h, m = t.strip().split(':')
                    scheduler.add_job(run_task, 'cron', hour=h, minute=m, args=[sid])
        elif mode == "interval":
            scheduler.add_job(run_task, 'interval', hours=int(sub.get("interval_hours", 12)), args=[sid])

if __name__ == '__main__':
    update_global_scheduler(); app.run(host='0.0.0.0', port=5123)
