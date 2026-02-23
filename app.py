import os, subprocess, json, threading, time, socket, datetime, uuid, csv
import requests
import urllib3
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response, redirect
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

# 屏蔽 SSL 安全警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# --- 路径与文件配置 ---
DATA_DIR = "/app/data"
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# --- 全局状态 ---
subs_status = {}
ip_cache = {}
api_lock = threading.Lock()
log_lock = threading.Lock()
file_lock = threading.Lock()
scheduler = BackgroundScheduler()
scheduler.start()

def get_now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def get_today():
    return datetime.datetime.now().strftime('%Y-%m-%d')

def format_duration(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))

def write_log_csv(data_dict):
    """
    按天生成 CSV 日志文件：log_YYYY-MM-DD.csv
    支持 Excel 直接打开，自动处理表头
    """
    file_path = os.path.join(DATA_DIR, f"log_{get_today()}.csv")
    headers = [
        "时间", "所属任务", "探测状态", "频道名称", "分辨率", 
        "视频编码", "音频编码", "帧率(FPS)", "延迟(ms)", 
        "网速(Mbps)", "地区", "运营商", "完整URL"
    ]
    
    try:
        with file_lock:
            file_exists = os.path.isfile(file_path)
            # 使用 utf-8-sig 编码，确保 Excel 打开不乱码
            with open(file_path, "a", encoding="utf-8-sig", newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(data_dict)
    except Exception as e:
        print(f"CSV Write Error: {e}")

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
                info = {"city": res.get('city', '未知城市'), "isp": res.get('isp', '未知网络')}
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
        if accel_type in ["qsv", "quicksync"]:
            hw_args = ['-hwaccel', 'qsv', '-qsv_device', device]
            icon = "⚡"
        else:
            hw_args = ['-hwaccel', 'vaapi', '-hwaccel_device', device]
            icon = "💎"
    cmd = ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-print_format', 'json',
           '-user_agent', 'Mozilla/5.0', '-probesize', '5000000', '-analyzeduration', '5000000'] + hw_args + ['-i', url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            streams = data.get('streams', [])
            fmt = data.get('format', {})
            v = next((s for s in streams if s['codec_type'] == 'video'), {})
            a = next((s for s in streams if s['codec_type'] == 'audio'), {})
            fps = "0"
            if v.get('avg_frame_rate') and '/' in v['avg_frame_rate']:
                try:
                    n, d = v['avg_frame_rate'].split('/')
                    if int(d) > 0: fps = str(round(int(n)/int(d)))
                except: pass
            rb = fmt.get('bit_rate') or v.get('bit_rate')
            br = f"{round(int(rb)/1024/1024, 2)}" if rb and str(rb).isdigit() else "UNK"
            return {"res": f"{v.get('width','?')}x{v.get('height','?')}", "h": v.get('height', 0), "v_codec": v.get('codec_name', 'UNK').upper(), "a_codec": a.get('codec_name', 'UNK').upper(), "fps": fps, "br": br, "icon": icon}
    except: pass
    return None

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    hp = get_source_info(url)
    if hp in status.get("blacklisted_hosts", set()): return None

    with log_lock:
        if hp not in status["summary_host"]: status["summary_host"][hp] = {"t": 0, "s": 0, "f": 0}
        if hp not in status["consecutive_failures"]: status["consecutive_failures"][hp] = 0

    log_data = {
        "时间": get_now(),
        "所属任务": status['sub_name'],
        "探测状态": "失败",
        "频道名称": name,
        "完整URL": url
    }

    try:
        start_time = time.time()
        resp = requests.get(url, stream=True, timeout=10, verify=False, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200: raise Exception(f"HTTP {resp.status_code}")
        latency = int((time.time() - start_time) * 1000)
        td, ss = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: resp.close(); return None
            td += len(chunk)
            if time.time() - ss > 2: break
        speed = round((td * 8) / ((time.time() - ss) * 1024 * 1024), 2)
        resp.close()

        meta = probe_stream(url, use_hw)
        if not meta: raise Exception("Probe Fail")
        
        geo = get_ip_info(url)
        city = geo['city'] if geo else "未知城市"
        isp = geo['isp'] if geo else "未知网络"
        
        # 填充 CSV 成功数据
        log_data.update({
            "探测状态": "成功",
            "分辨率": meta['res'],
            "视频编码": meta['v_codec'],
            "音频编码": meta['a_codec'],
            "帧率(FPS)": meta['fps'],
            "延迟(ms)": latency,
            "网速(Mbps)": speed,
            "地区": city,
            "运营商": isp
        })
        write_log_csv(log_data)

        with log_lock:
            status["consecutive_failures"][hp] = 0
            status["success"] += 1
            status["summary_host"][hp]["s"] += 1
            if city not in status["summary_city"]: status["summary_city"][city] = {"t": 0, "s": 0}
            status["summary_city"][city]["s"] += 1
            
            # 统计分布数据用于大屏
            h_val = int(meta.get('h', 0))
            res_tag = "4K" if h_val >= 2160 else "1080P" if h_val >= 1080 else "720P" if h_val >= 720 else "SD"
            status["analytics"]["res"][res_tag] = status["analytics"]["res"].get(res_tag, 0) + 1
            lat_tag = "<100ms" if latency < 100 else "<500ms" if latency < 500 else ">500ms"
            status["analytics"]["lat"][lat_tag] = status["analytics"]["lat"].get(lat_tag, 0) + 1

            detail = f"{meta['icon']}{meta['res']} | 🎬{meta['v_codec']} | 🚀{speed}Mbps | ⏱️{latency}ms | 📍{city}"
            status["logs"].append(f"✅ {name}: {detail}")
            
        return {"name": name, "url": url, "score": h_val + speed*5 - latency/10}
    except Exception as e:
        write_log_csv(log_data) # 写入失败记录
        with log_lock:
            status["consecutive_failures"][hp] = status.get("consecutive_failures", {}).get(hp, 0) + 1
            if hp in status["summary_host"]: status["summary_host"][hp]["f"] += 1
            if status["consecutive_failures"][hp] >= 10:
                if hp not in status["blacklisted_hosts"]:
                    status["blacklisted_hosts"].add(hp)
                    status["logs"].append(f"⚠️ 熔断: {hp} 连续失败，跳过后续。")
            if not status["stop_requested"]: status["logs"].append(f"❌ {name}: 失败({str(e)}) | 🔌{hp}")
        return None
    finally:
        with log_lock: 
            status["current"] += 1
            if hp in status["summary_host"]: status["summary_host"][hp]["t"] += 1

def run_task(sub_id):
    config = load_config(); sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running") or not sub.get("enabled", True): return
    
    start_ts = time.time()
    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0,
        "sub_name": sub['name'], "logs": [], "summary_host": {}, "summary_city": {}, 
        "consecutive_failures": {}, "blacklisted_hosts": set(), "analytics": {"res": {}, "lat": {}}
    }
    
    use_hw = config.get("settings", {}).get("use_hwaccel", True)
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
                    p = line.split(',')
                    if len(p)>=2: raw_channels.append((p[0].strip(), p[1].strip()))
    except: pass

    raw_channels = list(set(raw_channels))
    subs_status[sub_id]["total"] = len(raw_channels)
    thread_num = int(sub.get("threads", 10))
    subs_status[sub_id]["logs"].append(f"🎬 任务开始: {get_now()} | 源数量: {len(raw_channels)}")

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
    
    # 汇总报告
    try:
        status["logs"].append(" ")
        status["logs"].append("📊 --- 接口汇总报告 ---")
        sh = sorted([i for i in status["summary_host"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
        for h, d in sh: status["logs"].append(f"📡 {h:<28} | {round(d['s']/d['t']*100, 1)}% ({d['s']}/{d['t']})")
    except: pass

    # 保存文件
    try:
        m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
        txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
        valid_list.sort(key=lambda x: x['score'], reverse=True)
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
        save_config(config); update_global_scheduler(); return jsonify({"status": "ok"})
    return jsonify({"subs": config["subscriptions"], "settings": config["settings"]})

@app.route('/api/settings', methods=['POST'])
def save_global_settings():
    config = load_config(); config["settings"] = request.json; save_config(config); return jsonify({"status": "ok"})

@app.route('/api/hw_test')
def hw_test():
    try:
        result = subprocess.run(['vainfo'], capture_output=True, text=True, timeout=5)
        return jsonify({"status": "success" if result.returncode == 0 else "error", "output": result.stdout or result.stderr})
    except Exception as e: return jsonify({"status": "error", "output": str(e)})

@app.route('/api/status/<sub_id>')
def get_status(sub_id):
    info = subs_status.get(sub_id, {"running": False, "logs": [], "total":0, "current":0, "success":0, "analytics": {"res":{},"lat":{}}})
    return jsonify({"running": info.get("running", False), "logs": info.get("logs", []), "total": info.get("total", 0), "current": info.get("current", 0), "success": info.get("success", 0), "analytics": info.get("analytics", {"res":{},"lat":{}})})

@app.route('/api/start/<sub_id>')
def start_api(sub_id):
    if subs_status.get(sub_id, {}).get("running"): return jsonify({"status": "running"})
    threading.Thread(target=run_task, args=(sub_id,)).start(); return jsonify({"status": "ok"})

@app.route('/api/stop/<sub_id>')
def stop_api(sub_id):
    if sub_id in subs_status: subs_status[sub_id]["stop_requested"] = True
    return jsonify({"status": "ok"})

@app.route('/api/subs/delete/<sub_id>')
def delete_sub(sub_id):
    config = load_config(); config["subscriptions"] = [s for s in config["subscriptions"] if s["id"] != sub_id]
    save_config(config); update_global_scheduler(); return jsonify({"status": "ok"})

@app.route('/sub/<sub_id>.<ext>')
def get_sub_file(sub_id, ext): return send_from_directory(OUTPUT_DIR, f"{sub_id}.{ext}")

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
    update_global_scheduler(); app.run(host='0.0.0.0', port=5123)
