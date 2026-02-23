import os, subprocess, json, threading, time, socket, datetime, uuid, csv
import requests, urllib3
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

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

subs_status = {}
ip_cache = {}
api_lock, log_lock, file_lock = threading.Lock(), threading.Lock(), threading.Lock()
scheduler = BackgroundScheduler(); scheduler.start()

# --- 核心辅助 ---
def get_now(): return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def format_duration(seconds): return str(datetime.timedelta(seconds=int(seconds)))

def load_config():
    default = {"subscriptions": [], "settings": {"use_hwaccel": True, "epg_url": "http://epg.51zmt.top:12489/e.xml", "logo_base": "https://live.fanmingming.com/tv/"}}
    if not os.path.exists(CONFIG_FILE): return default
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if "settings" not in data: data["settings"] = default["settings"]
            return data
    except: return default

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4, ensure_ascii=False)

def write_log_csv(data_dict):
    file_path = os.path.join(LOG_DIR, f"log_{datetime.datetime.now().strftime('%Y-%m-%d')}.csv")
    headers = ["时间", "任务", "状态", "频道", "分辨率", "视频编码", "音频编码", "FPS", "延迟(ms)", "网速(Mbps)", "地区", "运营商", "URL"]
    try:
        with file_lock:
            exists = os.path.isfile(file_path)
            with open(file_path, "a", encoding="utf-8-sig", newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers); 
                if not exists: writer.writeheader()
                writer.writerow(data_dict)
    except: pass

def probe_stream(url, use_hw):
    accel_type = os.getenv("HW_ACCEL_TYPE", "vaapi").lower()
    device = os.getenv("VAAPI_DEVICE") or os.getenv("QSV_DEVICE") or "/dev/dri/renderD128"
    def run_ffprobe(hw_args, icon):
        cmd = ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-print_format', 'json'] + hw_args + \
              ['-user_agent', 'Mozilla/5.0', '-probesize', '5000000', '-analyzeduration', '5000000', '-i', url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            if r.returncode == 0:
                data = json.loads(r.stdout); streams = data.get('streams', [])
                v = next((s for s in streams if s['codec_type'] == 'video'), {})
                a = next((s for s in streams if s['codec_type'] == 'audio'), {})
                fmt = data.get('format', {})
                rb = fmt.get('bit_rate') or v.get('bit_rate')
                br = f"{round(int(rb)/1024/1024, 2)}Mbps" if rb and str(rb).isdigit() else "UNK"
                return {"res": f"{v.get('width','?')}x{v.get('height','?')}", "h": v.get('height', 0), "v_codec": v.get('codec_name', 'UNK').upper(), "a_codec": a.get('codec_name', 'UNK').upper(), "fps": v.get('avg_frame_rate','?'), "br": br, "icon": icon}
        except: pass
        return None
    if use_hw:
        hw_params = ['-hwaccel', 'vaapi', '-hwaccel_device', device, '-hwaccel_output_format', 'vaapi'] if accel_type == "vaapi" else ['-hwaccel', 'qsv', '-qsv_device', device]
        res = run_ffprobe(hw_params, "💎")
        if res: return res
    return run_ffprobe([], "💻")

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    hp = f"{urlparse(url).hostname}:{urlparse(url).port or (443 if urlparse(url).scheme=='https' else 80)}"
    if hp in status.get("blacklisted_hosts", set()): return None
    
    with log_lock:
        if hp not in status["summary_host"]: status["summary_host"][hp] = {"t": 0, "s": 0, "f": 0}
        if hp not in status["consecutive_failures"]: status["consecutive_failures"][hp] = 0

    try:
        resp = requests.get(url, stream=True, timeout=8, verify=False, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200: raise Exception(f"HTTP {resp.status_code}")
        td, ss = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: resp.close(); return None
            td += len(chunk); 
            if time.time() - ss > 2: break
        speed = round((td * 8) / ((time.time() - ss) * 1024 * 1024), 2)
        resp.close()
        
        meta = probe_stream(url, use_hw)
        if not meta: raise Exception("ProbeFail")
        
        with log_lock:
            status["consecutive_failures"][hp] = 0; status["success"] += 1; status["summary_host"][hp]["s"] += 1
            h_v = int(meta.get('h', 0)); r_tag = "8k" if h_v>=4320 else "4k" if h_v>=2160 else "1080p" if h_v>=1080 else "720p" if h_v>=720 else "sd"
            status["analytics"]["res"][r_tag.upper()] = status["analytics"]["res"].get(r_tag.upper(), 0) + 1
            msg = f"✅ {name}: {meta['icon']}{meta['res']} | 🎬{meta['v_codec']} | 🎵{meta['a_codec']} | 🎞️{meta['fps']} | 📊{speed}Mbps | 🌐{hp}"
            status["logs"].append(msg)
        return {"name": name, "url": url, "score": h_v + speed*5, "res_tag": r_tag}
    except Exception as e:
        with log_lock:
            status["consecutive_failures"][hp] += 1; status["summary_host"][hp]["f"] += 1
            if status["consecutive_failures"][hp] >= 10:
                if hp not in status["blacklisted_hosts"]:
                    status["blacklisted_hosts"].add(hp)
                    status["logs"].append(f"⚠️ 熔断激活: 接口 {hp} 连续失败10次，后续跳过。")
            if not status["stop_requested"]: status["logs"].append(f"❌ {name}: 失败({str(e)}) | 🔌{hp}")
        return None
    finally:
        with log_lock: status["current"] += 1; status["summary_host"][hp]["t"] += 1

def run_task(sub_id):
    config = load_config(); sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running"): return
    start_ts = time.time(); use_hw = config["settings"]["use_hwaccel"]
    res_filter = sub.get("res_filter", ["sd", "720p", "1080p", "4k", "8k"])
    subs_status[sub_id] = {"running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0, "sub_name": sub['name'], "logs": [], "summary_host": {}, "summary_city": {}, "consecutive_failures": {}, "blacklisted_hosts": set(), "analytics": {"res": {"SD":0,"720P":0,"1080P":0,"4K":0,"8K":0}, "lat": {}}}
    
    raw_channels = []
    try:
        r = requests.get(sub["url"], timeout=15, verify=False); r.encoding = r.apparent_encoding
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
                    p = line.split(','); raw_channels.append((p[0].strip(), p[1].strip()))
    except: pass
    raw_channels = list(set(raw_channels)); subs_status[sub_id]["total"] = len(raw_channels)
    thread_num = int(sub.get("threads", 10))
    subs_status[sub_id]["logs"].append(f"🚀 任务启动 | 总数: {len(raw_channels)} | 线程: {thread_num}")
    
    valid_raw = []
    with ThreadPoolExecutor(max_workers=thread_num) as executor:
        futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
        for f in futures:
            if subs_status[sub_id]["stop_requested"]:
                for fut in futures: fut.cancel(); break
            try:
                res = f.result(timeout=45); 
                if res: valid_raw.append(res)
            except: pass

    valid_list = [c for c in valid_raw if c['res_tag'] in res_filter]; valid_list.sort(key=lambda x: x['score'], reverse=True)
    status = subs_status[sub_id]; update_ts = get_now()
    
    # 生成报告
    status["logs"].append(" "); status["logs"].append("📊 --- 接口连通性汇总报告 ---")
    sh = sorted([i for i in status["summary_host"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
    for h, d in sh[:15]: status["logs"].append(f"📡 {h:<28} | 有效率: {round(d['s']/d['t']*100, 1)}% ({d['s']}/{d['t']})")
    
    if status["blacklisted_hosts"]:
        status["logs"].append(" "); status["logs"].append("🚫 --- 已熔断(已跳过)的接口 ---")
        for bh in status["blacklisted_hosts"]: status["logs"].append(f"❌ {bh}")

    # 保存
    m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u"); txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
    epg = config["settings"]["epg_url"]; logo = config["settings"]["logo_base"]
    with open(m3u_p, 'w', encoding='utf-8') as fm:
        fm.write(f"#EXTM3U x-tvg-url=\"{epg}\"\n# Updated: {update_ts}\n")
        for c in valid_list: fm.write(f"#EXTINF:-1 tvg-logo=\"{logo}{c['name']}.png\",{c['name']}\n{c['url']}\n")
    with open(txt_p, 'w', encoding='utf-8') as ft:
        ft.write(f"# Updated: {update_ts}\n"); [ft.write(f"{c['name']},{c['url']}\n") for c in valid_list]
    
    status["logs"].append(f"🏁 任务结束 | 有效: {len(valid_list)}"); status["running"] = False

# --- 其余路由逻辑保持 ---
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
    return jsonify({"subs": config["subscriptions"], "settings": config["settings"]})
@app.route('/api/settings', methods=['POST'])
def save_settings():
    config = load_config(); config["settings"] = request.json; save_config(config); return jsonify({"status": "ok"})
@app.route('/api/status/<sub_id>')
def get_status(sub_id):
    info = subs_status.get(sub_id, {"running": False, "logs": [], "total":0, "current":0, "success":0, "analytics": {"res":{},"lat":{}}})
    return jsonify({"running": info.get("running", False), "logs": info.get("logs"), "total": info.get("total", 0), "current": info.get("current", 0), "success": info.get("success", 0), "analytics": info.get("analytics", {"res":{},"lat":{}})})
@app.route('/api/start/<sub_id>')
def start_api(sub_id):
    threading.Thread(target=run_task, args=(sub_id,)).start(); return jsonify({"status": "ok"})
@app.route('/api/stop/<sub_id>')
def stop_api(sub_id):
    if sub_id in subs_status: subs_status[sub_id]["stop_requested"] = True
    return jsonify({"status": "ok"})
@app.route('/api/subs/delete/<sub_id>')
def delete_sub(sub_id):
    config = load_config(); config["subscriptions"] = [s for s in config["subscriptions"] if s["id"] != sub_id]
    save_config(config); return jsonify({"status": "ok"})
@app.route('/api/hw_test')
def hw_test():
    try:
        r = subprocess.run(['vainfo'], capture_output=True, text=True, timeout=5)
        out = r.stdout + r.stderr; ready = "va_openDriver() returns 0" in out
        codecs = []
        mapping = {"H264":"H264","HEVC":"HEVC|H265","VP9":"VP9","MPEG2":"MPEG2"}
        for k, v in mapping.items():
            if any(x in out.upper() for x in v.split('|')): codecs.append(k)
        return jsonify({"status": "success" if ready else "error", "message": "✅ GPU加速就绪" if ready else "❌ 驱动未连接", "codecs": codecs, "raw": out})
    except Exception as e: return jsonify({"status": "error", "raw": str(e)})
@app.route('/sub/<sub_id>.<ext>')
def get_sub_file(sub_id, ext): return send_from_directory(OUTPUT_DIR, f"{sub_id}.{ext}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5123)
