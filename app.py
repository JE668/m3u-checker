import os, subprocess, json, threading, time, socket, datetime, uuid, csv, re
import requests, urllib3, psutil
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response, redirect
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = Flask(__name__)

# --- 路径配置 ---
DATA_DIR = "/app/data"
LOG_DIR = os.path.join(DATA_DIR, "log")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
os.makedirs(LOG_DIR, exist_ok=True); os.makedirs(OUTPUT_DIR, exist_ok=True)

subs_status, ip_cache = {}, {}
api_lock, log_lock, file_lock = threading.Lock(), threading.Lock(), threading.Lock()
scheduler = BackgroundScheduler(); scheduler.start()

def get_now(): return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def get_today(): return datetime.datetime.now().strftime('%Y-%m-%d')
def format_duration(seconds): return str(datetime.timedelta(seconds=int(seconds)))

def write_log_csv(d):
    path = os.path.join(LOG_DIR, f"log_{get_today()}.csv")
    headers = ["时间", "任务", "状态", "频道", "分辨率", "视频编码", "音频编码", "FPS", "延迟(ms)", "网速(Mbps)", "地区", "运营商", "URL"]
    try:
        with file_lock:
            exists = os.path.isfile(path)
            with open(path, "a", encoding="utf-8-sig", newline='') as f:
                w = csv.DictWriter(f, fieldnames=headers); (not exists) and w.writeheader(); w.writerow(d)
    except: pass

def load_config():
    default = {"subscriptions": [], "settings": {"use_hwaccel": True, "epg_url": "http://epg.51zmt.top:12489/e.xml", "logo_base": "https://live.fanmingming.com/tv/"}}
    if not os.path.exists(CONFIG_FILE): return default
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f); return d if "settings" in d else {**d, "settings": default["settings"]}
    except: return default

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4, ensure_ascii=False)

def probe_stream(url, use_hw):
    accel_type = os.getenv("HW_ACCEL_TYPE", "vaapi").lower()
    device = os.getenv("VAAPI_DEVICE") or os.getenv("QSV_DEVICE") or "/dev/dri/renderD128"
    def run_f(hw, icon):
        cmd = ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-print_format', 'json', '-user_agent', 'Mozilla/5.0', '-probesize', '5000000', '-analyzeduration', '5000000'] + hw + ['-i', url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            if r.returncode == 0:
                data = json.loads(r.stdout); streams = data.get('streams', [])
                v = next((s for s in streams if s['codec_type'] == 'video'), {})
                a = next((s for s in streams if s['codec_type'] == 'audio'), {})
                fmt = data.get('format', {})
                rb = fmt.get('bit_rate') or v.get('bit_rate') or "0"
                fps = "?"
                if v.get('avg_frame_rate') and '/' in v['avg_frame_rate']:
                    try: 
                        n, d = v['avg_frame_rate'].split('/')
                        fps = str(round(int(n)/int(d))) if int(d)>0 else "?"
                    except: pass
                return {"res": f"{v.get('width','?')}x{v.get('height','?')}", "h": v.get('height', 0), "v_codec": v.get('codec_name', 'UNK').upper(), "a_codec": a.get('codec_name', 'UNK').upper() if a else "无音频", "fps": fps, "br": f"{round(int(rb)/1024/1024, 2)}Mbps", "icon": icon}
        except: pass
        return None
    if use_hw:
        hw_p = ['-hwaccel', 'vaapi', '-hwaccel_device', device, '-hwaccel_output_format', 'vaapi'] if accel_type == "vaapi" else ['-hwaccel', 'qsv', '-qsv_device', device]
        res = run_f(hw_p, "💎"); 
        if res: return res
    return run_f([], "💻")

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    parsed = urlparse(url); hp = f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme=='https' else 80)}"
    if hp in status["blacklisted_hosts"]: 
        with log_lock: status["analytics"]["stability"]["banned"] += 1
        return None
    with log_lock:
        if hp not in status["summary_host"]: status["summary_host"][hp] = {"t": 0, "s": 0, "f": 0}
        if hp not in status["consecutive_failures"]: status["consecutive_failures"][hp] = 0

    try:
        start_time = time.time()
        resp = requests.get(url, stream=True, timeout=8, verify=False, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200: raise Exception(f"HTTP {resp.status_code}")
        latency = int((time.time() - start_time) * 1000)
        td, ss = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: resp.close(); return None
            td += len(chunk); (time.time() - ss > 2) and True; break
        speed = round((td * 8) / ((time.time() - ss) * 1024 * 1024), 2)
        resp.close()
        meta = probe_stream(url, use_hw)
        if not meta: raise Exception("ProbeFail")
        
        geo = requests.get(f"http://ip-api.com/json/{socket.gethostbyname(urlparse(url).hostname)}?lang=zh-CN", timeout=5).json() if hp not in ip_cache else ip_cache[hp]
        city = geo.get('city', '未知') if isinstance(geo, dict) else "未知"
        
        with log_lock:
            status["consecutive_failures"][hp] = 0; status["success"] += 1; status["summary_host"][hp]["s"] += 1
            if city not in status["summary_city"]: status["summary_city"][city] = {"t": 0, "s": 0}
            status["summary_city"][city]["s"] += 1
            h = int(meta['h']); res_tag = "8K" if h>=4320 else "4K" if h>=2160 else "1080P" if h>=1080 else "720P" if h>=720 else "SD"
            status["analytics"]["res"][res_tag] += 1
            status["analytics"]["lat"]["<100ms" if latency<100 else "<500ms" if latency<500 else ">500ms"] += 1
            vc = meta['v_codec']; status["analytics"]["v_codec"][vc] = status["analytics"]["v_codec"].get(vc, 0) + 1
            ac = meta['a_codec']; status["analytics"]["a_codec"][ac] = status["analytics"]["a_codec"].get(ac, 0) + 1
            status["analytics"]["stability"]["success"] += 1
            msg = f"✅ {name}: {meta['icon']}{meta['res']} | 🎬{meta['v_codec']} | 🎵{meta['a_codec']} | 🎞️{meta['fps']}fps | 📊{speed}Mbps | ⏱️{latency}ms | 📍{city} | 🌐{hp}"
            status["logs"].append(msg)
            write_log_csv({"时间": get_now(), "任务": status['sub_name'], "状态": "成功", "频道": name, "分辨率": meta['res'], "视频编码": meta['v_codec'], "音频编码": meta['a_codec'], "FPS": meta['fps'], "延迟(ms)": latency, "网速(Mbps)": speed, "地区": city, "运营商": geo.get('isp','未知'), "URL": url})
        return {"name": name, "url": url, "score": h + speed*5 - latency/10, "res_tag": res_tag.lower()}
    except Exception as e:
        with log_lock:
            status["consecutive_failures"][hp] += 1; status["summary_host"][hp]["f"] += 1; status["analytics"]["stability"]["fail"] += 1
            if status["consecutive_failures"][hp] >= 10:
                if hp not in status["blacklisted_hosts"]: status["blacklisted_hosts"].add(hp); status["logs"].append(f"⚠️ 熔断激活: 接口 {hp} 连续失败10次，已跳过。")
            if not status["stop_requested"]: status["logs"].append(f"❌ {name}: 失败({str(e)}) | 🔌{hp}")
            write_log_csv({"时间": get_now(), "任务": status['sub_name'], "状态": "失败", "频道": name, "URL": url})
        return None
    finally:
        with log_lock: status["current"] += 1; status["summary_host"][hp]["t"] += 1

def run_task(sub_id):
    config = load_config(); sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running") or not sub.get("enabled", True): return
    start_ts = time.time(); use_hw = config["settings"]["use_hwaccel"]
    res_filter = [r.lower() for r in sub.get("res_filter", ["sd", "720p", "1080p", "4k", "8k"])]
    subs_status[sub_id] = {"running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0, "sub_name": sub['name'], "logs": [], "summary_host": {}, "summary_city": {}, "consecutive_failures": {}, "blacklisted_hosts": set(), "analytics": {"res": {"SD":0,"720P":0,"1080P":0,"4K":0,"8K":0}, "lat": {"<100ms":0,"<500ms":0,">500ms":0}, "v_codec": {}, "a_codec": {}, "stability": {"success":0, "fail":0, "banned":0}}}
    raw_channels = []
    try:
        r = requests.get(sub["url"], timeout=15, verify=False); r.encoding = r.apparent_encoding
        cn = "未知频道"
        for line in r.text.split('\n'):
            line = line.strip()
            if "#EXTINF" in line: cn = line.split(',')[-1].strip()
            elif line.startswith("http"): raw_channels.append((cn, line))
            elif "," in line and "http" in line:
                p = line.split(','); raw_channels.append((p[0].strip(), p[1].strip()))
    except: pass
    raw_channels = list(set(raw_channels)); total_num = len(raw_channels); subs_status[sub_id]["total"] = total_num
    thread_num = int(sub.get("threads", 10))
    subs_status[sub_id]["logs"].append(f"🚀 任务启动 | 总数: {total_num} | 线程: {thread_num}")
    valid_raw = []
    with ThreadPoolExecutor(max_workers=thread_num) as executor:
        futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
        for f in futures:
            if subs_status[sub_id]["stop_requested"]:
                for fut in futures: fut.cancel(); break
            try:
                res = f.result(timeout=45); (res) and valid_raw.append(res)
            except: pass
    valid_list = [c for c in valid_raw if c['res_tag'] in res_filter]; valid_list.sort(key=lambda x: x['score'], reverse=True)
    status = subs_status[sub_id]; update_ts = get_now()
    m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u"); txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
    epg = config["settings"]["epg_url"]; logo = config["settings"]["logo_base"]
    with open(m3u_p, 'w', encoding='utf-8') as fm:
        fm.write(f"#EXTM3U x-tvg-url=\"{epg}\"\n# Updated: {update_ts}\n")
        for c in valid_list: fm.write(f"#EXTINF:-1 tvg-logo=\"{logo}{c['name']}.png\",{c['name']}\n{c['url']}\n")
    with open(txt_p, 'w', encoding='utf-8') as ft:
        ft.write(f"# Updated: {update_ts}\n"); [ft.write(f"{c['name']},{c['url']}\n") for c in valid_list]
    status["running"] = False

@app.route('/')
def index(): return render_template('index.html')
@app.route('/api/sys_info')
def sys_info():
    try:
        gpu = 0
        if os.path.exists("/sys/class/drm/card0/device/gpu_busy_percent"):
            with open("/sys/class/drm/card0/device/gpu_busy_percent", 'r') as f: gpu = int(f.read().strip())
        return jsonify({"cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent, "gpu": gpu, "gpu_active": any(s.get("running") for s in subs_status.values())})
    except: return jsonify({"cpu": 0, "ram": 0, "gpu": 0})
@app.route('/api/network_test')
def network_test():
    res = {"v4": {"status": False, "ip": ""}, "v6": {"status": False, "ip": ""}}
    try:
        r4 = requests.get("https://api4.ipify.org?format=json", timeout=5).json()
        res["v4"] = {"status": True, "ip": r4['ip']}
    except: pass
    try:
        r6 = requests.get("https://api6.ipify.org?format=json", timeout=5).json()
        res["v6"] = {"status": True, "ip": r6['ip']}
    except: pass
    return jsonify(res)
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
    return jsonify({"subs": config["subscriptions"], "settings": config["settings"]})
@app.route('/api/status/<sub_id>')
def get_status(sub_id):
    info = subs_status.get(sub_id, {"running": False, "logs": [], "total":0, "current":0, "success":0, "blacklisted_hosts": set(), "analytics": {"res":{},"lat":{},"v_codec":{},"a_codec":{},"stability":{"success":0,"fail":0,"banned":0}}})
    return jsonify({"running": info.get("running", False), "logs": info.get("logs")[-100:], "total": info.get("total", 0), "current": info.get("current", 0), "success": info.get("success", 0), "banned_count": len(info.get("blacklisted_hosts", [])), "analytics": info.get("analytics")})
@app.route('/api/start/<sub_id>')
def start_api(sub_id):
    threading.Thread(target=run_task, args=(sub_id,)).start(); return jsonify({"status": "ok"})
@app.route('/api/stop/<sub_id>')
def stop_api(sub_id):
    if sub_id in subs_status: subs_status[sub_id]["stop_requested"] = True
    return jsonify({"status": "ok"})
@app.route('/api/settings', methods=['POST'])
def save_settings(): config = load_config(); config["settings"] = request.json; save_config(config); return jsonify({"status": "ok"})
@app.route('/api/hw_test')
def hw_test():
    try:
        r = subprocess.run(['vainfo'], capture_output=True, text=True, timeout=5)
        out = r.stdout + r.stderr; ready = "va_openDriver() returns 0" in out
        codecs = []
        mapping = {"H264":"H264","HEVC (H.265)":"HEVC|H265","VP9":"VP9","MPEG2":"MPEG2"}
        for k, v in mapping.items():
            if any(x in out.upper() for x in v.split('|')): codecs.append(k)
        return jsonify({"status": "success" if ready else "error", "message": "✅ GPU加速就绪" if ready else "❌ 驱动异常", "codecs": codecs, "raw": out})
    except Exception as e: return jsonify({"status": "error", "raw": str(e)})
@app.route('/api/subs/delete/<sub_id>')
def delete_sub(sub_id): config = load_config(); config["subscriptions"] = [s for s in config["subscriptions"] if s["id"] != sub_id]; save_config(config); return jsonify({"status": "ok"})
@app.route('/sub/<sub_id>.<ext>')
def get_sub_file(sub_id, ext): return send_from_directory(OUTPUT_DIR, f"{sub_id}.{ext}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5123)
