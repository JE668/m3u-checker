import os, subprocess, json, threading, time, socket, datetime, uuid, re
import requests, urllib3
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response, redirect
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = Flask(__name__)

DATA_DIR = "/app/data"
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
MASTER_LOG = os.path.join(DATA_DIR, "log.txt")
os.makedirs(OUTPUT_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

subs_status = {}
ip_cache = {}
api_lock, log_lock, file_lock = threading.Lock(), threading.Lock(), threading.Lock()
scheduler = BackgroundScheduler(); scheduler.start()

def get_now(): return datetime.datetime.now().strftime('%H:%M:%S')
def format_duration(seconds): return str(datetime.timedelta(seconds=int(seconds)))

def load_config():
    if not os.path.exists(CONFIG_FILE): return {"subscriptions": []}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return {"subscriptions": []}

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4, ensure_ascii=False)

def get_group(name):
    if "CCTV" in name.upper(): return "中央台"
    if "卫视" in name: return "各省卫视"
    if any(x in name for x in ["电影", "剧场", "影院"]): return "电影频道"
    if any(x in name for x in ["体育", "足球", "赛事"]): return "体育竞技"
    return "其他频道"

def probe_stream(url, use_hw):
    accel_type = os.getenv("HW_ACCEL_TYPE", "vaapi").lower()
    device = os.getenv("VAAPI_DEVICE") or os.getenv("QSV_DEVICE") or "/dev/dri/renderD128"
    hw_args = []
    icon = "💻"
    if use_hw:
        if accel_type in ["vaapi", "qsv", "quicksync"]:
            hw_args = ['-hwaccel', 'vaapi', '-hwaccel_device', device, '-hwaccel_output_format', 'vaapi']
            icon = "💎"
    cmd = ['ffprobe', '-v', 'error', '-show_streams', '-print_format', 'json', '-user_agent', 'Mozilla/5.0', 
           '-probesize', '5000000', '-analyzeduration', '5000000'] + hw_args + ['-i', url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            v = next((s for s in data.get('streams', []) if s['codec_type'] == 'video'), {})
            return {"res": f"{v.get('width','?')}x{v.get('height','?')}", "h": v.get('height', 0), "v_codec": v.get('codec_name', 'UNK').upper(), "icon": icon}
    except: pass
    return None

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    parsed = urlparse(url); hp = f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme=='https' else 80)}"
    if hp in status.get("blacklisted_hosts", set()): return None

    try:
        # 1. 测延迟 & 测速 (测速结果即为实测码率)
        start_time = time.time()
        resp = requests.get(url, stream=True, timeout=8, verify=False, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200: raise Exception("ConnectFail")
        latency = int((time.time() - start_time) * 1000)
        td, ss = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: break
            td += len(chunk)
            if time.time() - ss > 2: break
        speed = round((td * 8) / ((time.time() - ss) * 1024 * 1024), 2)
        resp.close()

        # 2. ffprobe 探测
        meta = probe_stream(url, use_hw)
        if not meta: raise Exception("MetadataFail")
        
        # 3. 统计分析数据 (用于大屏)
        with log_lock:
            status["consecutive_failures"][hp] = 0
            status["success"] += 1
            # 记录分辨率类型
            h = int(meta['h'])
            res_tag = "4K" if h >= 2160 else "1080P" if h >= 1080 else "720P" if h >= 720 else "SD"
            status["analytics"]["res"][res_tag] = status["analytics"]["res"].get(res_tag, 0) + 1
            # 记录延迟区间
            lat_tag = "<100ms" if latency < 100 else "<500ms" if latency < 500 else ">500ms"
            status["analytics"]["lat"][lat_tag] = status["analytics"]["lat"].get(lat_tag, 0) + 1
            
            detail = f"{meta['icon']}{meta['res']} | 🎬{meta['v_codec']} | 🚀{speed}Mbps | ⏱️{latency}ms"
            status["logs"].append(f"✅ {name}: {detail}")
            
        return {"name": name, "url": url, "group": get_group(name), "score": h + speed*10 - latency/10}
    except:
        with log_lock:
            status["consecutive_failures"][hp] = status.get("consecutive_failures", {}).get(hp, 0) + 1
            if status["consecutive_failures"][hp] >= 10: status["blacklisted_hosts"].add(hp)
            if not status["stop_requested"]: status["logs"].append(f"❌ {name}: 失败 | 🔌{hp}")
        return None
    finally:
        with log_lock: status["current"] += 1

def run_task(sub_id):
    config = load_config(); sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running"): return
    
    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0,
        "logs": [], "consecutive_failures": {}, "blacklisted_hosts": set(),
        "analytics": {"res": {}, "lat": {}} # 大屏统计数据
    }
    
    use_hw = os.getenv("USE_HWACCEL", "false").lower() == "true"
    raw_channels = []
    try:
        r = requests.get(sub["url"], timeout=15, verify=False); r.encoding = r.apparent_encoding
        lines = r.text.split('\n')
        for i, line in enumerate(lines):
            if "#EXTINF" in line:
                name = line.split(',')[-1].strip()
                for j in range(i+1, min(i+5, len(lines))):
                    if lines[j].strip().startswith("http"): raw_channels.append((name, lines[j].strip())); break
            elif "," in line and "http" in line:
                p = line.split(','); raw_channels.append((p[0].strip(), p[1].strip()))
    except: pass

    raw_channels = list(set(raw_channels)); subs_status[sub_id]["total"] = len(raw_channels)
    thread_num = int(sub.get("threads", 5))
    
    with ThreadPoolExecutor(max_workers=thread_num) as executor:
        futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
        valid_list = [f.result() for f in futures if not subs_status[sub_id]["stop_requested"] and f.result()]

    valid_list.sort(key=lambda x: x['score'], reverse=True)
    m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
    with open(m3u_p, 'w', encoding='utf-8') as fm:
        fm.write("#EXTM3U\n")
        for c in valid_list: fm.write(f"#EXTINF:-1 group-title=\"{c['group']}\",{c['name']}\n{c['url']}\n")
    
    subs_status[sub_id]["running"] = False

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/subs', methods=['GET', 'POST'])
def handle_subs():
    config = load_config()
    if request.method == 'POST':
        new_sub = request.json
        if not new_sub.get("id"): new_sub["id"] = str(uuid.uuid4())[:8]; config["subscriptions"].append(new_sub)
        else:
            for i, s in enumerate(config["subscriptions"]):
                if s["id"] == new_sub["id"]: config["subscriptions"][i] = new_sub
        save_config(config); return jsonify({"status": "ok"})
    return jsonify(config["subscriptions"])

@app.route('/api/status/<sub_id>')
def get_status(sub_id):
    s = subs_status.get(sub_id, {"running": False, "logs": [], "total":0, "current":0, "success":0, "analytics": {"res":{},"lat":{}}})
    return jsonify({
        "running": s["running"], "logs": s["logs"], "total": s["total"], 
        "current": s["current"], "success": s["success"], "analytics": s["analytics"]
    })

@app.route('/api/start/<sub_id>')
def start_api(sub_id):
    threading.Thread(target=run_task, args=(sub_id,)).start(); return jsonify({"status": "ok"})

@app.route('/api/stop/<sub_id>')
def stop_api(sub_id):
    if sub_id in subs_status: subs_status[sub_id]["stop_requested"] = True
    return jsonify({"status": "ok"})

@app.route('/sub/<sub_id>.<ext>')
def get_sub_file(sub_id, ext): return send_from_directory(OUTPUT_DIR, f"{sub_id}.{ext}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5123)
