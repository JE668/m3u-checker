import os, subprocess, json, threading, time, socket, datetime, uuid, csv, re
import requests, urllib3, psutil
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response, redirect
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = Flask(__name__)

# --- 路径与文件系统配置 ---
DATA_DIR = "/app/data"
LOG_DIR = os.path.join(DATA_DIR, "log")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
os.makedirs(LOG_DIR, exist_ok=True); os.makedirs(OUTPUT_DIR, exist_ok=True)

subs_status, ip_cache = {}, {}
api_lock, log_lock, file_lock = threading.Lock(), threading.Lock(), threading.Lock()
scheduler = BackgroundScheduler(); scheduler.start()

# --- 辅助工具 ---
def get_now(): return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def get_today(): return datetime.datetime.now().strftime('%Y-%m-%d')
def format_duration(seconds): return str(datetime.timedelta(seconds=int(seconds)))

def load_config():
    default = {"subscriptions": [], "settings": {"use_hwaccel": True, "epg_url": "http://epg.51zmt.top:12489/e.xml", "logo_base": "https://live.fanmingming.com/tv/"}}
    if not os.path.exists(CONFIG_FILE): return default
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f); return d if "settings" in d else {**d, "settings": default["settings"]}
    except: return default

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4, ensure_ascii=False)

def write_log_csv(d):
    path = os.path.join(LOG_DIR, f"log_{get_today()}.csv")
    headers = ["时间", "任务", "状态", "频道", "分辨率", "视频编码", "音频编码", "FPS", "延迟(ms)", "网速(Mbps)", "地区", "运营商", "URL"]
    try:
        with file_lock:
            exists = os.path.isfile(path)
            with open(path, "a", encoding="utf-8-sig", newline='') as f:
                w = csv.DictWriter(f, fieldnames=headers); (not exists) and w.writeheader(); w.writerow(d)
    except: pass

def get_res_tag(h):
    try:
        h = int(h)
        if h >= 4320: return "8k"
        if h >= 2160: return "4k"
        if h >= 1080: return "1080p"
        if h >= 720: return "720p"
        return "sd"
    except: return "sd"

# --- 【解耦】阶段 1: IP定位检测逻辑 ---
def fetch_ip_locations_sync(sub_id, host_list):
    status = subs_status[sub_id]
    total = len(host_list)
    status["logs"].append(f"🌐 阶段 1/2: 正在检索 {total} 个独立服务器节点的地理位置...")
    for idx, host in enumerate(host_list):
        if status["stop_requested"]: break
        try:
            if host in ip_cache: continue
            ip_addr = socket.gethostbyname(host)
            if ip_addr in ip_cache:
                ip_cache[host] = ip_cache[ip_addr]; continue
            with api_lock:
                time.sleep(1.35) # 频率限制
                r = requests.get(f"http://ip-api.com/json/{ip_addr}?lang=zh-CN", timeout=3, verify=False).json()
                if r.get('status') == 'success':
                    info = {"city": r.get('city', '未知'), "isp": r.get('isp', '未知')}
                    ip_cache[ip_addr] = info; ip_cache[host] = info
                    status["logs"].append(f"📍 定位中 [{idx+1}/{total}]: {host} -> {info['city']}")
        except: pass
    status["logs"].append(f"✅ 阶段 1/2: 服务器定位预检已完成。")

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
                afps = v.get('avg_frame_rate', '0/0')
                if '/' in afps:
                    num, den = afps.split('/')
                    if int(den) > 0: fps = str(round(int(num)/int(den)))
                return {"res": f"{v.get('width','?')}x{v.get('height','?')}", "h": v.get('height', 0), "v_codec": v.get('codec_name', 'UNK').upper(), "a_codec": a.get('codec_name', 'UNK').upper() if a else "无音频", "fps": fps, "br": f"{round(int(rb)/1024/1024, 2)}Mbps", "icon": icon}
        except: pass
        return None
    if use_hw:
        hw_p = ['-hwaccel', 'vaapi', '-hwaccel_device', device, '-hwaccel_output_format', 'vaapi'] if accel_type == "vaapi" else ['-hwaccel', 'qsv', '-qsv_device', device]
        res = run_f(hw_p, "💎")
        if res: return res
    return run_f([], "💻")

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    parsed = urlparse(url); host = parsed.hostname; hp = f"{host}:{parsed.port or (443 if parsed.scheme=='https' else 80)}"
    if hp in status["blacklisted_hosts"]: 
        with log_lock: status["analytics"]["stability"]["banned"] += 1
        return None
    with log_lock:
        if hp not in status["summary_host"]: status["summary_host"][hp] = {"t": 0, "s": 0, "f": 0, "lat_sum":0, "speed_sum":0, "score_sum":0}
        if hp not in status["consecutive_failures"]: status["consecutive_failures"][hp] = 0

    city_found = "未知城市"
    try:
        start_time = time.time()
        resp = requests.get(url, stream=True, timeout=8, verify=False, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200: raise Exception(f"HTTP {resp.status_code}")
        latency = int((time.time() - start_time) * 1000)
        td, ss = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: resp.close(); return None
            td += len(chunk); 
            if time.time() - ss > 2: break
        speed = round((td * 8) / ((time.time() - ss) * 1024 * 1024), 2)
        resp.close()
        meta = probe_stream(url, use_hw)
        if not meta: raise Exception("ProbeFail")
        geo = ip_cache.get(host) or {"city": "未知", "isp": "未知"}
        city_found = geo['city']
        with log_lock:
            status["consecutive_failures"][hp] = 0; status["success"] += 1; status["summary_host"][hp]["s"] += 1
            if city_found not in status["summary_city"]: status["summary_city"][city_found] = {"t": 0, "s": 0}
            status["summary_city"][city_found]["s"] += 1
            status["summary_host"][hp]["lat_sum"] += latency; status["summary_host"][hp]["speed_sum"] += speed
            h = int(meta['h']); res_tag = "8K" if h>=4320 else "4K" if h>=2160 else "1080P" if h>=1080 else "720P" if h>=720 else "SD"
            status["analytics"]["res"][res_tag] += 1
            status["analytics"]["lat"]["<100ms" if latency<100 else "<500ms" if latency<500 else ">500ms"] += 1
            vc, ac = meta['v_codec'], meta['a_codec']
            status["analytics"]["v_codec"][vc] = status["analytics"]["v_codec"].get(vc, 0) + 1
            status["analytics"]["a_codec"][ac] = status["analytics"]["a_codec"].get(ac, 0) + 1
            status["analytics"]["stability"]["success"] += 1
            score = h + speed*5 - latency/10; status["summary_host"][hp]["score_sum"] += score
            msg = f"✅ {name}: {meta['icon']}{meta['res']} | 🎬{vc} | 🎵{ac} | 🎞️{meta['fps']} | 📊{speed}Mbps | ⏱️{latency}ms | 📍{city_found} | 🌐{hp}"
            status["logs"].append(msg)
            write_log_csv({"时间": get_now(), "任务": status['sub_name'], "状态": "成功", "频道": name, "分辨率": meta['res'], "视频编码": vc, "音频编码": ac, "FPS": meta['fps'], "延迟(ms)": latency, "网速(Mbps)": speed, "地区": city_found, "运营商": geo['isp'], "URL": url})
        return {"name": name, "url": url, "score": score, "res_tag": res_tag.lower()}
    except Exception as e:
        with log_lock:
            status["consecutive_failures"][hp] += 1; status["summary_host"][hp]["f"] += 1; status["analytics"]["stability"]["fail"] += 1
            if status["consecutive_failures"][hp] >= 10:
                if hp not in status["blacklisted_hosts"]: status["blacklisted_hosts"].add(hp); status["logs"].append(f"⚠️ 熔断激活: 接口 {hp} 连续失败10次，已跳过。")
            if not status["stop_requested"]: status["logs"].append(f"❌ {name}: 失败({str(e)}) | 🔌{hp}")
        return None
    finally:
        with log_lock: 
            status["current"] += 1; status["summary_host"][hp]["t"] += 1
            if city_found not in status["summary_city"]: status["summary_city"][city_found] = {"t": 0, "s": 0}
            status["summary_city"][city_found]["t"] += 1

def run_task(sub_id):
    config = load_config(); sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running"): return
    start_ts = time.time(); use_hw = config["settings"]["use_hwaccel"]
    res_filter = [r.lower() for r in sub.get("res_filter", ["sd", "720p", "1080p", "4k", "8k"])]
    subs_status[sub_id] = {"running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0, "sub_name": sub['name'], "logs": [], "summary_host": {}, "summary_city": {}, "consecutive_failures": {}, "blacklisted_hosts": set(), "analytics": {"res": {"SD":0,"720P":0,"1080P":0,"4K":0,"8K":0}, "lat": {"<100ms":0,"<500ms":0,">500ms":0}, "v_codec": {}, "a_codec": {}, "stability": {"success":0, "fail":0, "banned":0}}}
    
    # 强力解析引擎
    raw_channels = []
    try:
        r = requests.get(sub["url"], timeout=15, verify=False); r.encoding = r.apparent_encoding
        last_name = "未知频道"
        for line in r.text.split('\n'):
            line = line.strip()
            if not line: continue
            if "#EXTINF" in line: last_name = line.split(',')[-1].strip()
            elif "://" in line:
                if "," in line: # Name,URL format
                    parts = line.split(','); raw_channels.append((parts[0].strip(), parts[1].strip()))
                else: raw_channels.append((last_name, line))
    except Exception as e:
        subs_status[sub_id]["logs"].append(f"❌ 订阅拉取失败: {e}"); subs_status[sub_id]["running"] = False; return

    raw_channels = list(set(raw_channels)); subs_status[sub_id]["total"] = len(raw_channels)
    
    if len(raw_channels) > 0:
        unique_hosts = list(set([urlparse(c[1]).hostname for c in raw_channels if c[1]]))
        fetch_ip_locations_sync(sub_id, unique_hosts)
        with ThreadPoolExecutor(max_workers=int(sub.get("threads", 10))) as executor:
            futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
            valid_raw = [f.result() for f in futures if not subs_status[sub_id].get("stop_requested") and f.result()]
    else:
        valid_raw = []

    valid_list = [c for c in valid_raw if c['res_tag'] in res_filter]; valid_list.sort(key=lambda x: x['score'], reverse=True)
    status = subs_status[sub_id]; duration = format_duration(time.time() - start_ts); update_ts = get_now()
    
    # 最终汇总报告
    status["logs"].append(" "); status["logs"].append("📜 ==================== 探测结算报告 ====================")
    status["logs"].append(f"⏱️ 任务总耗时: {duration} | 有效源: {len(valid_list)} / 成功探测: {status['success']}")
    status["logs"].append("🏙️ --- 地区连通汇总 ---")
    sc = sorted([i for i in status["summary_city"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
    for c, d in sc: status["logs"].append(f"📍 {c:<30} | 有效率: {round(d['s']/d['t']*100, 1)}% ({d['s']}/{d['t']})")
    status["logs"].append("📡 --- 接口质量全表 (按评分) ---")
    ah = {k: v for k, v in status["summary_host"].items() if k not in status["blacklisted_hosts"] and v['t']>0}
    sh = sorted(ah.items(), key=lambda x: x[1]['score_sum']/x[1]['s'] if x[1]['s']>0 else 0, reverse=True)
    for h, d in sh:
        al, aspd = (int(d['lat_sum']/d['s']), round(d['speed_sum']/d['s'], 2)) if d['s']>0 else (0,0)
        status["logs"].append(f"{'⭐️' if d['s']/d['t']>0.8 else '📡'} {h:<24} | ⏱️{al}ms | 🚀{aspd}Mbps | 有效率: {round(d['s']/d['t']*100, 1)}%")
    if status["blacklisted_hosts"]:
        status["logs"].append("🚫 --- 已熔断的接口清单 ---")
        for bh in status["blacklisted_hosts"]: status["logs"].append(f"❌ {bh} (连续10次失败)")
    status["logs"].append("======================================================")
    status["logs"].append(f"🏁 任务完成时间: {get_now()}")

    # 存档
    arch = {"update_time": update_ts, "duration": duration, "logs": status["logs"], "stats": {"total": status["total"], "current": status["current"], "success": status["success"], "banned": len(status["blacklisted_hosts"])}, "analytics": status["analytics"]}
    with open(os.path.join(OUTPUT_DIR, f"last_status_{sub_id}.json"), "w", encoding="utf-8") as f: json.dump(arch, f, ensure_ascii=False)

    try:
        m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u"); txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
        epg = config["settings"]["epg_url"]; logo = config["settings"]["logo_base"]
        with open(m3u_p, 'w', encoding='utf-8') as fm:
            fm.write(f"#EXTM3U x-tvg-url=\"{epg}\"\n# Updated: {update_ts}\n# Duration: {duration}\n")
            for c in valid_list: fm.write(f"#EXTINF:-1 tvg-logo=\"{logo}{c['name']}.png\",{c['name']}\n{c['url']}\n")
        with open(txt_p, 'w', encoding='utf-8') as ft:
            ft.write(f"# Updated: {update_ts}\n# Duration: {duration}\n"); [ft.write(f"{c['name']},{c['url']}\n") for c in valid_list]
    except: pass
    status["running"] = False

@app.route('/')
def index(): return render_template('index.html')
@app.route('/api/sys_info')
def sys_info():
    try:
        gpu = 0
        if os.path.exists("/sys/class/drm/card0/device/gpu_busy_percent"):
            with open("/sys/class/drm/ card0/device/gpu_busy_percent", 'r') as f: gpu = int(f.read().strip())
        return jsonify({"cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent, "gpu": gpu, "gpu_active": any(s.get("running") for s in subs_status.values())})
    except: return jsonify({"cpu": 0, "ram": 0, "gpu": 0})
@app.route('/api/network_test')
def network_test():
    res = {"v4": {"status": False, "ip": ""}, "v6": {"status": False, "ip": ""}}
    try:
        r4 = requests.get("https://api4.ipify.org?format=json", timeout=5).json(); res["v4"] = {"status": True, "ip": r4['ip']}
    except: pass
    try:
        r6 = requests.get("https://api6.ipify.org?format=json", timeout=5).json(); res["v6"] = {"status": True, "ip": r6['ip']}
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
    if sub_id in subs_status:
        s = subs_status[sub_id]
        return jsonify({"running": s["running"], "logs": s["logs"][-2000:], "total": s["total"], "current": s["current"], "success": s["success"], "banned_count": len(s.get("blacklisted_hosts", [])), "analytics": s["analytics"]})
    archive_path = os.path.join(OUTPUT_DIR, f"last_status_{sub_id}.json")
    if os.path.exists(archive_path):
        with open(archive_path, 'r', encoding='utf-8') as f:
            d = json.load(f); return jsonify({"running": False, "logs": d["logs"][-2000:], "total": d["stats"]["total"], "current": d["stats"]["current"], "success": d["stats"]["success"], "banned_count": d["stats"]["banned"], "analytics": d["analytics"]})
    return jsonify({"running": False, "logs": [], "total":0, "current":0, "success":0, "banned_count": 0, "analytics": {}})
@app.route('/api/start/<sub_id>')
def start_api(sub_id): threading.Thread(target=run_task, args=(sub_id,)).start(); return jsonify({"status": "ok"})
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
