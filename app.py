import os, subprocess, json, threading, time, socket, datetime, uuid
import requests
import urllib3
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response, redirect
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

# 屏蔽 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# --- 路径与文件配置 ---
DATA_DIR = "/app/data"
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
MASTER_LOG = os.path.join(DATA_DIR, "log.txt")
os.makedirs(OUTPUT_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# --- 全局变量 ---
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
    default_config = {"subscriptions": [], "settings": {"use_hwaccel": True}}
    if not os.path.exists(CONFIG_FILE): return default_config
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if "settings" not in data: data["settings"] = default_config["settings"]
            return data
    except: return default_config

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def get_source_info(url):
    try:
        parsed = urlparse(url)
        p = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return f"{parsed.hostname}:{p}"
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
                info = {"city": res.get('city', '未知城市'), "isp": res.get('isp', '未知网络')}
                ip_cache[ip] = info
                return info
        return None
    except: return None

def probe_stream(url, use_hw):
    """
    终极稳健探测逻辑：
    1. 采用最稳的参数结构。
    2. 优先 CPU 探测，以保证海量任务下的稳定性。
    3. 如果 CPU 失败且开启了硬解，则尝试硬解。
    """
    accel_type = os.getenv("HW_ACCEL_TYPE", "vaapi").lower()
    device = os.getenv("VAAPI_DEVICE") or os.getenv("QSV_DEVICE") or "/dev/dri/renderD128"
    
    # 定义核心探测动作
    def run_ffprobe(extra_args, icon_tag):
        # 严格遵守成功版本的参数顺序和 probesize
        cmd = [
            'ffprobe', '-v', 'error', 
            '-show_format', '-show_streams', 
            '-print_format', 'json',
            '-user_agent', 'Mozilla/5.0',
            '-probesize', '5000000', 
            '-analyzeduration', '5000000'
        ] + extra_args + ['-i', url]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                streams = data.get('streams', [])
                fmt = data.get('format', {})
                v = next((s for s in streams if s.get('codec_type') == 'video'), None)
                a = next((s for s in streams if s.get('codec_type') == 'audio'), None)
                if not v: return None
                
                fps = "?"
                if v.get('avg_frame_rate') and '/' in v['avg_frame_rate']:
                    try:
                        n, d = v['avg_frame_rate'].split('/')
                        if int(d) > 0: fps = str(round(int(n)/int(d)))
                    except: pass
                
                rb = fmt.get('bit_rate') or v.get('bit_rate')
                br = f"{round(int(rb)/1024/1024, 2)}Mbps" if rb and str(rb).isdigit() else "UNK"
                
                return {
                    "res": f"{v.get('width','?')}x{v.get('height','?')}",
                    "h": v.get('height', 0),
                    "v_codec": v.get('codec_name', 'UNK').upper(),
                    "a_codec": a.get('codec_name', '无').upper() if a else "无音频",
                    "fps": f"{fps}fps",
                    "br": br,
                    "icon": icon_tag
                }
        except: pass
        return None

    # 第一优先级：CPU 探测（探测元数据其实不需要硬解，CPU 最稳）
    res = run_ffprobe([], "💻")
    if res: return res

    # 第二优先级：如果 CPU 失败，尝试硬解探测
    if use_hw:
        hw_args = ['-hwaccel', 'qsv', '-qsv_device', device] if accel_type in ["qsv", "quicksync"] else ['-hwaccel', 'vaapi', '-hwaccel_device', device]
        res = run_ffprobe(hw_args, "💎")
        if res: return res

    return None

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    hp = get_source_info(url)
    
    # 熔断黑名单检查
    if hp in status.get("blacklisted_hosts", set()): return None

    with log_lock:
        if hp not in status["summary_host"]: status["summary_host"][hp] = {"t": 0, "s": 0, "f": 0}
        if hp not in status["consecutive_failures"]: status["consecutive_failures"][hp] = 0

    try:
        # 1. 基础 HTTP 连通性
        start_time = time.time()
        resp = requests.get(url, stream=True, timeout=8, verify=False, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200: raise Exception(f"HTTP {resp.status_code}")
        latency = int((time.time() - start_time) * 1000)
        
        # 2. 测速
        td, ss = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: resp.close(); return None
            td += len(chunk)
            if time.time() - ss > 2: break
        speed = round((td * 8) / ((time.time() - ss) * 1024 * 1024), 2)
        resp.close()

        # 3. 探测元数据 (必须探测成功才叫 ✅)
        meta = probe_stream(url, use_hw)
        if not meta: raise Exception("Probe Fail")
        
        geo = get_ip_info(url)
        city = geo['city'] if geo else "未知城市"
        isp = geo['isp'] if geo else "未知网络"
        
        with log_lock:
            # 成功则清零该 Host 的连续失败计数
            status["consecutive_failures"][hp] = 0
            status["success"] += 1
            status["summary_host"][hp]["s"] += 1
            if city not in status["summary_city"]: status["summary_city"][city] = {"t": 0, "s": 0}
            status["summary_city"][city]["s"] += 1
            
            # 更新大屏分析数据
            h = int(meta.get('h', 0))
            res_tag = "4K" if h >= 2160 else "1080P" if h >= 1080 else "720P" if h >= 720 else "SD"
            status["analytics"]["res"][res_tag] = status["analytics"]["res"].get(res_tag, 0) + 1
            lat_tag = "<100ms" if latency < 100 else "<500ms" if latency < 500 else ">500ms"
            status["analytics"]["lat"][lat_tag] = status["analytics"]["lat"].get(lat_tag, 0) + 1

            detail = f"{meta['icon']}{meta['res']} | 🎬{meta['v_codec']} | 🎵{meta['a_codec']} | 🎞️{meta['fps']} | 📊{meta['br']} | ⏱️{latency}ms | 🚀{speed}Mbps | 📍{city} | 🔌{hp}"
            status["logs"].append(f"✅ {name}: {detail}")
            write_master_log(f"[{status['sub_name']}] ✅ {name}: {detail} (URL: {url})")
            
        return {"name": name, "url": url}

    except Exception as e:
        with log_lock:
            status["consecutive_failures"][hp] = status.get("consecutive_failures", {}).get(hp, 0) + 1
            if hp in status["summary_host"]: status["summary_host"][hp]["f"] += 1
            
            # 触发连续熔断
            if status["consecutive_failures"][hp] >= 10:
                if hp not in status["blacklisted_hosts"]:
                    status["blacklisted_hosts"].add(hp)
                    status["logs"].append(f"⚠️ 熔断激活: 接口 {hp} 已连续失败10次，后续跳过。")

            if not status["stop_requested"]:
                status["logs"].append(f"❌ {name}: 探测失败({str(e)}) | 🔌{hp}")
                write_master_log(f"[{status['sub_name']}] ❌ {name}: 失败({str(e)}) | 🔌{hp}")
        return None
    finally:
        with log_lock:
            status["current"] += 1
            if hp in status["summary_host"]: status["summary_host"][hp]["t"] += 1

def run_task(sub_id):
    config = load_config()
    sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running") or not sub.get("enabled", True): return
    
    start_ts = time.time()
    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0,
        "sub_name": sub['name'], "logs": [], "summary_host": {}, "summary_city": {},
        "consecutive_failures": {}, "blacklisted_hosts": set(),
        "analytics": {"res": {}, "lat": {}}
    }
    
    use_hw = config.get("settings", {}).get("use_hwaccel", True)
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
    subs_status[sub_id]["total"] = len(raw_channels)
    thread_num = int(sub.get("threads", 5))
    est_min = round((len(raw_channels) * 11) / (thread_num * 60), 1)
    subs_status[sub_id]["logs"].append(f"🎬 任务开始: {get_now()} | 源数量: {len(raw_channels)} | 线程: {thread_num} | 硬件加速: {'开启' if use_hw else '关闭'}")
    
    valid_list = []
    with ThreadPoolExecutor(max_workers=thread_num) as executor:
        futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
        for f in futures:
            if subs_status[sub_id]["stop_requested"]:
                for fut in futures: fut.cancel()
                break
            try:
                res = f.result(timeout=40)
                if res: valid_list.append(res)
            except: pass

    status = subs_status[sub_id]
    duration_str = format_duration(time.time() - start_ts)
    update_ts = get_now()
    
    try:
        status["logs"].append(" ")
        status["logs"].append("📊 --- 接口汇总报告 ---")
        sh = sorted([i for i in status["summary_host"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
        for h, d in sh: status["logs"].append(f"📡 {h:<28} | 有效率: {round(d['s']/d['t']*100, 1)}% ({d['s']}/{d['t']})")
    except: pass

    try:
        m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
        txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
        with open(m3u_p, 'w', encoding='utf-8') as fm:
            fm.write(f"#EXTM3U\n# Updated: {update_ts}\n")
            for c in valid_list: fm.write(f"#EXTINF:-1,{c['name']}\n{c['url']}\n")
        with open(txt_p, 'w', encoding='utf-8') as ft:
            ft.write(f"# Updated: {update_ts}\n")
            for c in valid_list: ft.write(f"{c['name']},{c['url']}\n")
    except: pass
    
    status["logs"].append(f"⏰ 更新时间: {update_ts} | ⌛ 总耗时: {duration_str}")
    status["logs"].append(f"🏁 结算完毕 (有效源: {len(valid_list)})")
    status["running"] = False

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
    return jsonify({"subs": config["subscriptions"], "settings": config.get("settings", {"use_hwaccel": True})})

@app.route('/api/settings', methods=['POST'])
def save_global_settings():
    config = load_config(); config["settings"] = request.json; save_config(config)
    return jsonify({"status": "ok"})

@app.route('/api/hw_test')
def hw_test():
    try:
        result = subprocess.run(['vainfo'], capture_output=True, text=True, timeout=5)
        return jsonify({"status": "success" if result.returncode == 0 else "error", "output": result.stdout or result.stderr})
    except Exception as e: return jsonify({"status": "error", "output": str(e)})

@app.route('/api/subs/delete/<sub_id>')
def delete_sub(sub_id):
    config = load_config(); config["subscriptions"] = [s for s in config["subscriptions"] if s["id"] != sub_id]
    save_config(config); update_global_scheduler(); return jsonify({"status": "ok"})

@app.route('/api/status/<sub_id>')
def get_status(sub_id):
    info = subs_status.get(sub_id, {"running": False, "logs": [], "total":0, "current":0, "success":0, "analytics": {"res":{},"lat":{}}})
    return jsonify({"running": info.get("running", False), "logs": info.get("logs", []), "total": info.get("total", 0), "current": info.get("current", 0), "success": info.get("success", 0), "analytics": info.get("analytics", {"res":{},"lat":{}})})

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
