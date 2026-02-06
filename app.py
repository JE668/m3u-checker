import os, subprocess, json, threading, time, socket, datetime, uuid
import requests
import urllib3
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response, redirect
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# --- 路径配置 ---
DATA_DIR = "/app/data"
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
MASTER_LOG = os.path.join(DATA_DIR, "log.txt")
os.makedirs(OUTPUT_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

subs_status = {}
ip_cache = {}
api_lock = threading.Lock()
log_lock = threading.Lock()
file_lock = threading.Lock()
scheduler = BackgroundScheduler()
scheduler.start()

def get_now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def write_master_log(content):
    try:
        with file_lock:
            with open(MASTER_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{get_now()}] {content}\n")
    except: pass

def load_config():
    if not os.path.exists(CONFIG_FILE): return {"subscriptions": []}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return {"subscriptions": []}

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def get_source_info(url):
    try:
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return f"{parsed.hostname}:{port}"
    except: return "未知接口"

def get_ip_info(url):
    try:
        hostname = urlparse(url).hostname
        ip = socket.gethostbyname(hostname)
        if ip in ip_cache: return ip_cache[ip]
        with api_lock:
            time.sleep(1.35)
            res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5, verify=False).json()
            if res.get('status') == 'success':
                info = {"city": res.get('city', '未知城市'), "isp": res.get('isp', '未知运营商')}
                ip_cache[ip] = info
                return info
        return None
    except: return None

def probe_stream(url, use_hw):
    accel_type = os.getenv("HW_ACCEL_TYPE", "qsv").lower()
    device = os.getenv("QSV_DEVICE") or os.getenv("VAAPI_DEVICE") or "/dev/dri/renderD128"
    if use_hw:
        try:
            hw_args = ['-hwaccel', 'qsv', '-qsv_device', device] if accel_type in ["qsv", "quicksync"] else ['-hwaccel', 'vaapi', '-hwaccel_device', device]
            cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0', '-probesize', '5000000', '-analyzeduration', '5000000'] + hw_args + ['-i', url]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0: return json.loads(r.stdout)['streams'][0], ("⚡" if "qsv" in str(hw_args) else "💎")
        except: pass 
    try:
        cmd_cpu = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0', '-i', url]
        r = subprocess.run(cmd_cpu, capture_output=True, text=True, timeout=10)
        if r.returncode == 0: return json.loads(r.stdout)['streams'][0], "💻"
    except: pass
    return None, "❌"

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    hp = get_source_info(url)
    with log_lock:
        if hp not in status["summary_host"]: status["summary_host"][hp] = {"t": 0, "s": 0}

    try:
        start_time = time.time()
        resp = requests.get(url, stream=True, timeout=8, verify=False)
        lat = int((time.time() - start_time) * 1000)
        td, ss = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: 
                resp.close()
                return None
            td += len(chunk)
            if time.time() - ss > 2: break
        speed = round((td * 8) / ((time.time() - ss) * 1024 * 1024), 2)
        resp.close()
        video, icon = probe_stream(url, use_hw)
        if not video: raise Exception("Probe Fail")
        res_str = f"{video.get('width')}x{video.get('height')}"
        geo = get_ip_info(url)
        city = geo['city'] if geo else "未知城市"
        isp = geo['isp'] if geo else "未知网络"
        with log_lock:
            if city not in status["summary_city"]: status["summary_city"][city] = {"t": 0, "s": 0}
        msg = f"{icon}{res_str} | ⏱️{lat}ms | 🚀{speed}Mbps | 📍{city} | 🏢{isp} | 🔌{hp}"
        write_master_log(f"[{status['sub_name']}] ✅ {name}: {msg}")
        with log_lock:
            if not status["stop_requested"]:
                status["success"] += 1
                status["summary_host"][hp]["s"] += 1
                status["summary_city"][city]["s"] += 1
                status["logs"].append(f"✅ {name}: {msg}")
        return {"name": name, "url": url}
    except:
        geo = get_ip_info(url); city = geo['city'] if geo else "未知城市"
        with log_lock:
            if city not in status["summary_city"]: status["summary_city"][city] = {"t": 0, "s": 0}
            if not status["stop_requested"]: status["logs"].append(f"❌ {name}: 连接失败 | 🔌{hp}")
        write_master_log(f"[{status['sub_name']}] ❌ {name}: 连接失败 | 🔌{hp}")
        return None
    finally:
        with log_lock:
            status["current"] += 1
            if hp in status["summary_host"]: status["summary_host"][hp]["t"] += 1
            if city in status["summary_city"]: status["summary_city"][city]["t"] += 1

def run_task(sub_id):
    config = load_config()
    sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running"): return

    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0,
        "sub_name": sub['name'], "logs": [], "summary_host": {}, "summary_city": {}
    }
    
    use_hw = os.getenv("USE_HWACCEL", "false").lower() == "true"
    raw_channels = []
    try:
        r = requests.get(sub["url"], timeout=15, verify=False)
        r.encoding = r.apparent_encoding
        text = r.text
        # M3U/TXT 解析逻辑保持不变
        if "#EXTINF" in text:
            for i, line in enumerate(text.split('\n')):
                if "#EXTINF" in line:
                    name = line.split(',')[-1].strip()
                    for j in range(i+1, min(i+5, len(text.split('\n')))):
                        u = text.split('\n')[j].strip()
                        if u.startswith("http"): raw_channels.append((name, u)); break
        else:
            for line in text.split('\n'):
                if "," in line and "http" in line:
                    p = line.split(',')
                    if len(p)>=2: raw_channels.append((p[0].strip(), p[1].strip()))
    except: pass

    raw_channels = list(set(raw_channels))
    total_count = len(raw_channels)
    subs_status[sub_id]["total"] = total_count
    
    # 2. 预估时间逻辑 (按平均每项8秒并发计算)
    thread_num = int(sub.get("threads", 5))
    est_min = round((total_count * 8) / (thread_num * 60), 1) if total_count > 0 else 0
    subs_status[sub_id]["logs"].append(f"🎬 任务启动时间: {get_now()} | 源数量: {total_count} | 预估耗时: ~{est_min} 分钟")

    valid_list = []
    with ThreadPoolExecutor(max_workers=thread_num) as executor:
        futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
        for f in futures:
            if subs_status[sub_id]["stop_requested"]:
                for fut in futures: fut.cancel()
                break
            try:
                res = f.result(timeout=25)
                if res: valid_list.append(res)
            except: pass

    status = subs_status[sub_id]
    update_time = get_now()
    
    # 生成报告
    try:
        status["logs"].append(" ")
        status["logs"].append("📊 --- 质量质量汇总 ---")
        sh = sorted([i for i in status["summary_host"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
        for h, d in sh: status["logs"].append(f"📡 {h:<28} | 有效率: {round(d['s']/d['t']*100, 1):>5}% ({d['s']}/{d['t']})")
        status["logs"].append(" ")
        status["logs"].append("🏙️ --- 区域连通汇总 ---")
        sc = sorted([i for i in status["summary_city"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
        for c, d in sc: status["logs"].append(f"📍 {c:<30} | 有效率: {round(d['s']/d['t']*100, 1):>5}% ({d['s']}/{d['t']})")
    except: pass

    # 3. 增加更新时间到网页日志
    status["logs"].append(" ")
    status["logs"].append(f"⏰ 更新时间: {update_time}")

    # 4. 保存结果 (增加文件内更新时间)
    try:
        m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
        txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
        with open(m3u_p, 'w', encoding='utf-8') as fm:
            fm.write(f"#EXTM3U\n# Updated: {update_time}\n")
            for c in valid_list: fm.write(f"#EXTINF:-1,{c['name']}\n{c['url']}\n")
        with open(txt_p, 'w', encoding='utf-8') as ft:
            ft.write(f"# Updated: {update_time}\n")
            for c in valid_list: ft.write(f"{c['name']},{c['url']}\n")
    except: pass

    status["logs"].append(f"🏁 任务结算完毕 (有效源: {len(valid_list)})")
    status["running"] = False

# --- 路由逻辑保持不变 ---
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
def start_api(sub_id):
    if subs_status.get(sub_id, {}).get("running"): return jsonify({"status": "running"})
    threading.Thread(target=run_task, args=(sub_id,)).start()
    return jsonify({"status": "ok"})

@app.route('/api/stop/<sub_id>')
def stop_api(sub_id):
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
    update_global_scheduler()
    app.run(host='0.0.0.0', port=5123)
