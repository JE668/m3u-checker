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
def get_today(): return datetime.datetime.now().strftime('%Y-%m-%d')
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
    file_path = os.path.join(LOG_DIR, f"log_{get_today()}.csv")
    headers = ["时间", "任务", "状态", "频道", "分辨率", "视频编码", "音频编码", "FPS", "延迟(ms)", "网速(Mbps)", "地区", "运营商", "URL"]
    try:
        with file_lock:
            exists = os.path.isfile(file_path)
            with open(file_path, "a", encoding="utf-8-sig", newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                if not exists: writer.writeheader()
                writer.writerow(data_dict)
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
                fps = "0"
                if v.get('avg_frame_rate') and '/' in v['avg_frame_rate']:
                    try:
                        n, d = v['avg_frame_rate'].split('/')
                        if int(d) > 0: fps = str(round(int(n)/int(d)))
                    except: pass
                fmt = data.get('format', {})
                rb = fmt.get('bit_rate') or v.get('bit_rate')
                br = f"{round(int(rb)/1024/1024, 2)}" if rb and str(rb).isdigit() else "UNK"
                return {"res": f"{v.get('width','?')}x{v.get('height','?')}", "h": v.get('height', 0), "v_codec": v.get('codec_name', 'UNK').upper(), "a_codec": a.get('codec_name', 'UNK').upper(), "fps": fps, "br": br, "icon": icon}
        except: pass
        return None
    if use_hw:
        hw_params = ['-hwaccel', 'vaapi', '-hwaccel_device', device, '-hwaccel_output_format', 'vaapi'] if accel_type == "vaapi" else ['-hwaccel', 'qsv', '-qsv_device', device]
        res = run_ffprobe(hw_params, "💎" if accel_type == "vaapi" else "⚡")
        if res: return res
    return run_ffprobe([], "💻")

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    hp = f"{urlparse(url).hostname}:{urlparse(url).port or (443 if urlparse(url).scheme=='https' else 80)}"
    if hp in status.get("blacklisted_hosts", set()): return None
    
    # 提前初始化统计信息，确保在 finally 中一定能计数
    with log_lock:
        if hp not in status["summary_host"]: status["summary_host"][hp] = {"t": 0, "s": 0, "f": 0}
        if hp not in status["consecutive_failures"]: status["consecutive_failures"][hp] = 0

    log_entry = {"时间": get_now(), "任务": status['sub_name'], "状态": "失败", "频道": name, "URL": url}

    try:
        start_time = time.time()
        resp = requests.get(url, stream=True, timeout=8, verify=False, headers={'User-Agent': 'Mozilla/5.0'})
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
        if not meta: raise Exception("ProbeFail")
        
        geo = get_ip_info(url); city = geo['city'] if geo else "未知城市"; isp = geo['isp'] if geo else "未知网络"
        
        log_entry.update({"探测状态": "成功", "分辨率": meta['res'], "视频编码": meta['v_codec'], "音频编码": meta['a_codec'], "FPS": meta['fps'], "延迟(ms)": latency, "网速(Mbps)": speed, "地区": city, "运营商": isp})
        write_log_csv(log_entry)

        with log_lock:
            status["consecutive_failures"][hp] = 0
            status["success"] += 1
            status["summary_host"][hp]["s"] += 1
            if city not in status["summary_city"]: status["summary_city"][city] = {"t": 0, "s": 0}
            status["summary_city"][city]["s"] += 1
            
            # 统计总延迟和码率（用于均值分析）
            status["perf"]["total_lat"] += latency
            status["perf"]["total_speed"] += speed

            h_val = int(meta.get('h', 0))
            res_tag = get_res_tag(h_val)
            status["analytics"]["res"][res_tag.upper()] = status["analytics"]["res"].get(res_tag.upper(), 0) + 1
            lat_tag = "<100ms" if latency < 100 else "<500ms" if latency < 500 else ">500ms"
            status["analytics"]["lat"][lat_tag] = status["analytics"]["lat"].get(lat_tag, 0) + 1
            
            detail = f"{meta['icon']}{meta['res']} | 🎬{meta['v_codec']} | 🚀{speed}Mbps | ⏱️{latency}ms | 📍{city}"
            status["logs"].append(f"✅ {name}: {detail}")
        return {"name": name, "url": url, "score": h_val + speed*5 - latency/10, "res_tag": res_tag}
    except Exception as e:
        write_log_csv(log_entry)
        with log_lock:
            status["consecutive_failures"][hp] += 1
            status["summary_host"][hp]["f"] += 1
            if status["consecutive_failures"][hp] >= 10:
                if hp not in status["blacklisted_hosts"]:
                    status["blacklisted_hosts"].add(hp); status["logs"].append(f"⚠️ 熔断: {hp} 连续失败，跳过。")
            if not status["stop_requested"]: status["logs"].append(f"❌ {name}: 失败({str(e)}) | 🔌{hp}")
        return None
    finally:
        with log_lock: 
            status["current"] += 1
            status["summary_host"][hp]["t"] += 1
            # 确保即使没 geo 也能统计城市总量
            try: city_key = city
            except: city_key = "未知城市"
            if city_key not in status["summary_city"]: status["summary_city"][city_key] = {"t": 0, "s": 0}
            status["summary_city"][city_key]["t"] += 1

def run_task(sub_id):
    config = load_config(); sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running"): return
    
    start_ts = time.time()
    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0, "sub_name": sub['name'], 
        "logs": [], "summary_host": {}, "summary_city": {}, "consecutive_failures": {}, "blacklisted_hosts": set(), 
        "analytics": {"res": {"SD":0,"720P":0,"1080P":0,"4K":0,"8K":0}, "lat": {}},
        "perf": {"total_lat": 0, "total_speed": 0}
    }
    
    use_hw = config["settings"]["use_hwaccel"]
    res_filter = sub.get("res_filter", ["sd", "720p", "1080p", "4k", "8k"])
    
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

    raw_channels = list(set(raw_channels)); total_num = len(raw_channels); subs_status[sub_id]["total"] = total_num
    thread_num = int(sub.get("threads", 10))
    subs_status[sub_id]["logs"].append(f"🚀 任务启动 | 总数: {total_num} | 线程: {thread_num} | 硬件加速: {'ON' if use_hw else 'OFF'}")

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

    # 结果过滤与结算
    valid_list = [c for c in valid_raw if c['res_tag'] in res_filter]
    valid_list.sort(key=lambda x: x['score'], reverse=True)
    
    status = subs_status[sub_id]
    duration = time.time() - start_ts
    update_ts = get_now()

    # --- 强化版报告生成 ---
    status["logs"].append(" ")
    status["logs"].append("📜 ==================== 探测结算报告 ====================")
    status["logs"].append(f"⏱️ 任务耗时: {format_duration(duration)} | 有效源: {len(valid_list)} / {status['success']} (已过滤 {status['success']-len(valid_list)} 个)")
    if status['success'] > 0:
        status["logs"].append(f"⚡ 平均延迟: {int(status['perf']['total_lat']/status['success'])}ms | 平均带宽: {round(status['perf']['total_speed']/status['success'], 2)}Mbps")

    status["logs"].append(" ")
    status["logs"].append("📡 --- 接口质量排名前 10 ---")
    sh = sorted([i for i in status["summary_host"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
    for h, d in sh[:10]:
        rate = round(d['s']/d['t']*100, 1)
        icon = "⭐️" if rate > 80 else "⚪"
        status["logs"].append(f"{icon} {h:<28} | 有效率: {rate:>5}% ({d['s']}/{d['t']})")

    status["logs"].append(" ")
    status["logs"].append("🏙️ --- 地区连通性汇总 ---")
    sc = sorted([i for i in status["summary_city"].items() if i[1]['t']>0], key=lambda x: x[1]['s']/x[1]['t'], reverse=True)
    for c, d in sc:
        rate = round(d['s']/d['t']*100, 1)
        status["logs"].append(f"📍 {c:<30} | 有效率: {rate:>5}% ({d['s']}/{d['t']})")
    status["logs"].append("======================================================")

    # 保存文件
    try:
        m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u"); txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
        with open(m3u_p, 'w', encoding='utf-8') as fm:
            fm.write(f"#EXTM3U\n# Updated: {update_ts}\n")
            for c in valid_list: fm.write(f"#EXTINF:-1,{c['name']}\n{c['url']}\n")
        with open(txt_p, 'w', encoding='utf-8') as ft:
            ft.write(f"# Updated: {update_ts}\n")
            for c in valid_list: ft.write(f"{c['name']},{c['url']}\n")
    except: pass
    
    status["logs"].append(f"⏰ 更新时间: {update_ts} | 🏁 任务结束")
    status["running"] = False

# --- 其余路由逻辑保持不变 ---
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
def save_global_settings():
    config = load_config(); config["settings"] = request.json; save_config(config); return jsonify({"status": "ok"})
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
@app.route('/api/status/<sub_id>')
def get_status(sub_id):
    info = subs_status.get(sub_id, {"running": False, "logs": [], "total":0, "current":0, "success":0, "analytics": {"res":{},"lat":{}}})
    return jsonify({"running": info.get("running", False), "logs": info.get("logs")[-150:], "total": info.get("total", 0), "current": info.get("current", 0), "success": info.get("success", 0), "analytics": info.get("analytics", {"res":{},"lat":{}})})
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
    save_config(config); return jsonify({"status": "ok"})
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
