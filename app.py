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
    """将秒数转为 时:分:秒 格式"""
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
    """深度探测流信息：分辨率、视频编码、音频编码、帧率"""
    accel_type = os.getenv("HW_ACCEL_TYPE", "qsv").lower()
    device = os.getenv("QSV_DEVICE") or os.getenv("VAAPI_DEVICE") or "/dev/dri/renderD128"
    
    hw_args = []
    if use_hw:
        if accel_type in ["quicksync", "qsv"]:
            hw_args = ['-hwaccel', 'qsv', '-qsv_device', device]
        else:
            hw_args = ['-hwaccel', 'vaapi', '-hwaccel_device', device]

    cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0',
           '-probesize', '5000000', '-analyzeduration', '5000000'] + hw_args + ['-i', url]
    
    # 获取音频信息的辅助命令（如果不带 v:0，默认会出所有流）
    cmd_all = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', 
               '-probesize', '5000000', '-analyzeduration', '5000000'] + hw_args + ['-i', url]

    try:
        result = subprocess.run(cmd_all, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            video = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
            audio = next((s for s in data['streams'] if s['codec_type'] == 'audio'), None)
            
            icon = ("⚡" if "qsv" in str(hw_args) else "💎") if use_hw else "💻"
            
            # 帧率计算
            fps = "0"
            if video and 'avg_frame_rate' in video:
                try:
                    num, den = video['avg_frame_rate'].split('/')
                    if int(den) > 0: fps = str(round(int(num)/int(den)))
                except: pass

            metadata = {
                "res": f"{video.get('width','?')}x{video.get('height','?')}" if video else "未知尺寸",
                "v_codec": video.get('codec_name', '未知视频').upper() if video else "无视频",
                "a_codec": audio.get('codec_name', '未知音频').upper() if audio else "无音频",
                "fps": f"{fps}fps",
                "icon": icon
            }
            return metadata
    except: pass
    return None

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

        meta = probe_stream(url, use_hw)
        if not meta: raise Exception("Probe Fail")
        
        geo = get_ip_info(url)
        city = geo['city'] if geo else "未知城市"
        isp = geo['isp'] if geo else "未知网络"
        
        with log_lock:
            if city not in status["summary_city"]: status["summary_city"][city] = {"t": 0, "s": 0}

        # 拼接详情：[图标][分辨率] | [视频编码] | [音频编码] | [FPS] | [延迟] | [网速] ...
        detail_msg = (f"{meta['icon']}{meta['res']} | 🎬{meta['v_codec']} | 🎵{meta['a_codec']} | 🎞️{meta['fps']} | "
                      f"⏱️{lat}ms | 🚀{speed}Mbps | 📍{city} | 🏢{isp} | 🔌{hp}")
        
        write_master_log(f"[{status['sub_name']}] ✅ {name}: {detail_msg} (URL: {url})")

        with log_lock:
            if not status["stop_requested"]:
                status["success"] += 1
                status["summary_host"][hp]["s"] += 1
                status["summary_city"][city]["s"] += 1
                status["logs"].append(f"✅ {name}: {detail_msg}")
        return {"name": name, "url": url}

    except:
        geo = get_ip_info(url); city = geo['city'] if geo else "未知城市"
        with log_lock:
            if city not in status["summary_city"]: status["summary_city"][city] = {"t": 0, "s": 0}
            if not status["stop_requested"]:
                status["logs"].append(f"❌ {name}: 连接失败 | 🔌{hp}")
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

    task_start_time = time.time()
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
    est_min = round((total_count * 8) / (thread_num * 60), 1) if total_count > 0 else 0
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

    # --- 结算逻辑 ---
    status = subs_status[sub_id]
    task_end_time = time.time()
    elapsed_time = format_duration(task_end_time - task_start_time)
    update_time_str = get_now()

    try:
        status["logs"].append(" ")
        status["logs"].append("======================================================")
        status["logs"].append("📊 --- 接口服务质量汇总 ---")
        sorted_host = sorted([i for i in status["summary_host"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
        for h, d in sorted_host:
            rate = round(d['s']/d['t']*100, 1)
            status["logs"].append(f"📡 {h:<28} | 有效率: {rate:>5}% ({d['s']}/{d['t']})")

        status["logs"].append(" ")
        status["logs"].append("🏙️ --- 城市连通性汇总 ---")
        sorted_city = sorted([i for i in status["summary_city"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
        for c, d in sorted_city:
            rate = round(d['s']/d['t']*100, 1)
            status["logs"].append(f"📍 {c:<30} | 有效率: {rate:>5}% ({d['s']}/{d['t']})")
        status["logs"].append("======================================================")
    except: pass

    # 保存文件
    try:
        m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
        txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
        with open(m3u_p, 'w', encoding='utf-8') as fm:
            fm.write(f"#EXTM3U\n# Updated: {update_time_str}\n# Duration: {elapsed_time}\n")
            for c in valid_list: fm.write(f"#EXTINF:-1,{c['name']}\n{c['url']}\n")
        with open(txt_p, 'w', encoding='utf-8') as ft:
            ft.write(f"# Updated: {update_time_str}\n# Duration: {elapsed_time}\n")
            for c in valid_list: ft.write(f"{c['name']},{c['url']}\n")
    except: pass

    status["logs"].append(" ")
    status["logs"].append(f"⏰ 更新时间: {update_time_str}")
    status["logs"].append(f"⌛ 任务总耗时: {elapsed_time}")
    
    final_msg = "🛑 任务已手动停止" if status["stop_requested"] else "🏁 任务圆满完成"
    status["logs"].append(f"{final_msg} (有效源: {len(valid_list)})")
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
