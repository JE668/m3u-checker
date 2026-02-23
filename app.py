import os, subprocess, json, threading, time, socket, datetime, uuid
import requests
import urllib3
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response, redirect
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

# 屏蔽 SSL 安全警告
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

def format_duration(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))

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
    hw_args = []
    icon = "💻"
    if use_hw:
        if accel_type in ["quicksync", "qsv"]:
            hw_args = ['-hwaccel', 'qsv', '-qsv_device', device]
            icon = "⚡"
        else:
            hw_args = ['-hwaccel', 'vaapi', '-hwaccel_device', device]
            icon = "💎"

    cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0',
           '-probesize', '5000000', '-analyzeduration', '5000000'] + hw_args + ['-i', url]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if 'streams' in data and len(data['streams']) > 0:
                v = data['streams'][0]
                fps = "0"
                if v.get('avg_frame_rate') and '/' in v['avg_frame_rate']:
                    try:
                        n, d = v['avg_frame_rate'].split('/')
                        if int(d) > 0: fps = str(round(int(n)/int(d)))
                    except: pass
                return {"res": f"{v.get('width','?')}x{v.get('height','?')}", "v_codec": v.get('codec_name', 'UNK').upper(), "fps": f"{fps}fps", "icon": icon}
    except: pass
    return None

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    hp = get_source_info(url)
    
    # --- 熔断机制：检查该 Host 是否已被标记为失效 ---
    if hp in status.get("blacklisted_hosts", set()):
        return None

    with log_lock:
        if hp not in status["summary_host"]: status["summary_host"][hp] = {"t": 0, "s": 0, "f": 0}

    try:
        start_time = time.time()
        resp = requests.get(url, stream=True, timeout=8, verify=False)
        if resp.status_code != 200: raise Exception("HTTP Error")
        
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

        meta = probe_stream(url, use_hw)
        if not meta: raise Exception("Probe Fail")
        
        geo = get_ip_info(url)
        city = geo['city'] if geo else "未知城市"
        isp = geo['isp'] if geo else "未知网络"
        
        with log_lock:
            if city not in status["summary_city"]: status["summary_city"][city] = {"t": 0, "s": 0}
        
        detail_msg = f"{meta['icon']}{meta['res']} | 🎬{meta['v_codec']} | 🎞️{meta['fps']} | ⏱️{lat}ms | 🚀{speed}Mbps | 📍{city} | 🏢{isp} | 🔌{hp}"
        write_master_log(f"[{status['sub_name']}] ✅ {name}: {detail_msg}")

        with log_lock:
            if not status["stop_requested"]:
                status["success"] += 1
                status["summary_host"][hp]["s"] += 1
                status["summary_city"][city]["s"] += 1
                status["logs"].append(f"✅ {name}: {detail_msg}")
        return {"name": name, "url": url}

    except:
        with log_lock:
            if hp in status["summary_host"]:
                status["summary_host"][hp]["f"] += 1
                # 如果单个 Host 连续失败超过 10 次，加入熔断黑名单
                if status["summary_host"][hp]["f"] >= 10:
                    if hp not in status["blacklisted_hosts"]:
                        status["blacklisted_hosts"].add(hp)
                        status["logs"].append(f"⚠️ 熔断激活: 接口 {hp} 失败次数过多，已跳过后续所有链接。")

            if not status["stop_requested"]:
                status["logs"].append(f"❌ {name}: 连接失败 | 🔌{hp}")
        write_master_log(f"[{status['sub_name']}] ❌ {name}: 连接失败 | 🔌{hp}")
        return None
    finally:
        with log_lock:
            status["current"] += 1
            if hp in status["summary_host"]: status["summary_host"][hp]["t"] += 1

def run_task(sub_id):
    config = load_config()
    sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    # 检查是否存在、是否正在运行、是否被启用
    if not sub or subs_status.get(sub_id, {}).get("running"): return
    if not sub.get("enabled", True): return 

    start_ts = time.time()
    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0,
        "sub_name": sub['name'], "logs": [], "summary_host": {}, "summary_city": {},
        "blacklisted_hosts": set() # 熔断黑名单
    }
    
    use_hw = os.getenv("USE_HWACCEL", "false").lower() == "true"
    raw_channels = []
    try:
        r = requests.get(sub["url"], timeout=15, verify=False)
        r.encoding = r.apparent_encoding
        text = r.text
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
    
    thread_num = int(sub.get("threads", 5))
    est_min = round((total_count * 9) / (thread_num * 60), 1)
    subs_status[sub_id]["logs"].append(f"🎬 任务开始: {get_now()} | 源数量: {total_count} | 线程: {thread_num} | 预估: ~{est_min}min")

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
    duration_str = format_duration(time.time() - start_ts)
    update_ts = get_now()

    # 汇总
    try:
        status["logs"].append(" ")
        status["logs"].append("📊 --- 接口汇总报告 ---")
        sh = sorted([i for i in status["summary_host"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
        for h, d in sh: status["logs"].append(f"📡 {h:<28} | 有效率: {round(d['s']/d['t']*100, 1):>5}% ({d['s']}/{d['t']})")
        status["logs"].append(" ")
        status["logs"].append("🏙️ --- 城市汇总报告 ---")
        sc = sorted([i for i in status["summary_city"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
        for c, d in sc: status["logs"].append(f"📍 {c:<30} | 有效率: {round(d['s']/d['t']*100, 1):>5}% ({d['s']}/{d['t']})")
    except: pass

    # 保存文件
    try:
        m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
        txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
        with open(m3u_p, 'w', encoding='utf-8') as fm:
            fm.write(f"#EXTM3U\n# Updated: {update_ts}\n# Duration: {duration_str}\n")
            for c in valid_list: fm.write(f"#EXTINF:-1,{c['name']}\n{c['url']}\n")
        with open(txt_p, 'w', encoding='utf-8') as ft:
            ft.write(f"# Updated: {update_ts}\n# Duration: {duration_str}\n")
            for c in valid_list: ft.write(f"{c['name']},{c['url']}\n")
    except: pass

    status["logs"].append(" ")
    status["logs"].append(f"⏰ 更新时间: {update_ts}")
    status["logs"].append(f"⌛ 总耗时: {duration_str}")
    status["logs"].append(f"🏁 结算完毕 (有效源: {len(valid_list)})")
    status["running"] = False

# --- 路由逻辑 ---
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
    config = load_config()
    sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if sub and not sub.get("enabled", True): return jsonify({"status": "error", "message": "Subscription is disabled"})
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
        # 仅为启用的订阅添加定时任务
        if not sub.get("enabled", True): continue
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
