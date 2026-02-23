import os, subprocess, json, threading, time, socket, datetime, uuid, re
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

# --- 全局状态与缓存 ---
subs_status = {}
ip_cache = {}
api_lock = threading.Lock()
log_lock = threading.Lock()
file_lock = threading.Lock()
scheduler = BackgroundScheduler()
scheduler.start()

# --- 辅助函数 ---
def get_now(): return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def format_duration(seconds): return str(datetime.timedelta(seconds=int(seconds)))

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

# --- 核心：频道分组与评分引擎 ---
def get_group(name):
    """根据频道名自动归类"""
    if "CCTV" in name.upper(): return "中央台"
    if "卫视" in name: return "各省卫视"
    if any(x in name for x in ["电影", "剧场", "影院"]): return "电影频道"
    if any(x in name for x in ["体育", "足球", "赛事"]): return "体育竞技"
    if any(x in name for x in ["新闻", "资讯"]): return "新闻资讯"
    if any(x in name for x in ["综合", "公共"]): return "地方频道"
    return "其他频道"

def calculate_score(res_h, bitrate_mbps, latency_ms):
    """
    高质量评分算法：
    分辨率权重最高，码率次之，延迟作为负反馈
    """
    try:
        res_score = int(res_h) if res_h != '?' else 0
        br_score = float(bitrate_mbps) * 10
        lat_penalty = int(latency_ms) / 10
        return round(res_score + br_score - lat_penalty, 2)
    except: return 0

# --- 核心：硬件加速探测逻辑 ---
def probe_stream_metadata(url, use_hw):
    """
    针对 Intel UHD 620 优化的硬件加速探测
    """
    accel_type = os.getenv("HW_ACCEL_TYPE", "vaapi").lower()
    device = os.getenv("VAAPI_DEVICE") or os.getenv("QSV_DEVICE") or "/dev/dri/renderD128"
    
    # 基础参数
    hw_args = []
    icon = "💻"
    if use_hw:
        if accel_type in ["vaapi", "qsv", "quicksync"]:
            # 统一使用 VAAPI 映射进行元数据抓取，这是最稳的
            hw_args = ['-hwaccel', 'vaapi', '-hwaccel_device', device, '-hwaccel_output_format', 'vaapi']
            icon = "💎"

    cmd = ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-print_format', 'json',
           '-user_agent', 'Mozilla/5.0', '-probesize', '5000000', '-analyzeduration', '5000000'] + hw_args + ['-i', url]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            streams = data.get('streams', [])
            fmt = data.get('format', {})
            v = next((s for s in streams if s.get('codec_type') == 'video'), {})
            a = next((s for s in streams if s.get('codec_type') == 'audio'), {})
            
            rb = fmt.get('bit_rate') or v.get('bit_rate') or 0
            br_num = round(int(rb)/1024/1024, 2) if str(rb).isdigit() else 0
            
            res_h = v.get('height', 0)
            return {
                "res": f"{v.get('width','?')}x{res_h}",
                "res_h": res_h,
                "v_codec": v.get('codec_name', 'UNK').upper(),
                "a_codec": a.get('codec_name', 'UNK').upper(),
                "br": br_num,
                "icon": icon
            }
    except: pass
    return None

def test_single_channel(sub_id, name, url, use_hw):
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    parsed = urlparse(url)
    hp = f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme=='https' else 80)}"
    
    if hp in status.get("blacklisted_hosts", set()): return None

    with log_lock:
        if hp not in status["summary_host"]: 
            status["summary_host"][hp] = {"t": 0, "s": 0, "f": 0}
            status["consecutive_failures"][hp] = 0 

    try:
        # 1. 基础连接
        start_time = time.time()
        resp = requests.get(url, stream=True, timeout=8, verify=False, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200: raise Exception(f"HTTP {resp.status_code}")
        latency = int((time.time() - start_time) * 1000)
        
        # 2. 测速
        td, ss = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: break
            td += len(chunk)
            if time.time() - ss > 2: break
        speed = round((td * 8) / ((time.time() - ss) * 1024 * 1024), 2)
        resp.close()

        # 3. 硬件/软件探测
        meta = probe_stream_metadata(url, use_hw)
        if not meta: raise Exception("Probe Fail")
        
        # 获取位置
        ip_info = socket.gethostbyname(parsed.hostname)
        geo = requests.get(f"http://ip-api.com/json/{ip_info}?lang=zh-CN", timeout=2).json() if hp not in ip_cache else ip_cache[hp]
        city = geo.get('city', '未知') if isinstance(geo, dict) else "未知"
        
        # 4. 计算质量评分
        score = calculate_score(meta['res_h'], speed, latency)
        
        with log_lock:
            status["consecutive_failures"][hp] = 0
            status["success"] += 1
            status["summary_host"][hp]["s"] += 1
            
            detail = f"{meta['icon']}{meta['res']} | 🎬{meta['v_codec']} | 🚀{speed}Mbps | ⏱️{latency}ms | ⭐{score}"
            status["logs"].append(f"✅ {name}: {detail}")
            
        return {
            "name": name, "url": url, "group": get_group(name), 
            "score": score, "detail": detail, "host": hp
        }

    except Exception as e:
        with log_lock:
            status["consecutive_failures"][hp] = status.get("consecutive_failures", {}).get(hp, 0) + 1
            if hp in status["summary_host"]: status["summary_host"][hp]["f"] += 1
            if status["consecutive_failures"][hp] >= 10: status["blacklisted_hosts"].add(hp)
            if not status["stop_requested"]: status["logs"].append(f"❌ {name}: 失败 | 🔌{hp}")
        return None
    finally:
        with log_lock: status["current"] += 1; status["summary_host"][hp]["t"] += 1

def run_task(sub_id):
    config = load_config()
    sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running"): return

    start_ts = time.time()
    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0,
        "sub_name": sub['name'], "logs": [], "summary_host": {}, "summary_city": {},
        "consecutive_failures": {}, "blacklisted_hosts": set()
    }
    
    use_hw = os.getenv("USE_HWACCEL", "false").lower() == "true"
    
    # 解析逻辑 (M3U/TXT)
    raw_channels = []
    try:
        r = requests.get(sub["url"], timeout=15, verify=False)
        r.encoding = r.apparent_encoding
        lines = r.text.split('\n')
        for i, line in enumerate(lines):
            if "#EXTINF" in line:
                name = line.split(',')[-1].strip()
                for j in range(i+1, min(i+5, len(lines))):
                    if lines[j].strip().startswith("http"):
                        raw_channels.append((name, lines[j].strip())); break
            elif "," in line and "http" in line:
                p = line.split(',')
                if len(p)>=2: raw_channels.append((p[0].strip(), p[1].strip()))
    except: pass

    raw_channels = list(set(raw_channels))
    subs_status[sub_id]["total"] = len(raw_channels)
    thread_num = int(sub.get("threads", 5))
    subs_status[sub_id]["logs"].append(f"🚀 启动高质量检测任务 | 线程: {thread_num}")

    valid_list = []
    with ThreadPoolExecutor(max_workers=thread_num) as executor:
        futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
        for f in futures:
            if subs_status[sub_id]["stop_requested"]: break
            try:
                res = f.result(timeout=40)
                if res: valid_list.append(res)
            except: pass

    # --- 高质量排序与分组逻辑 ---
    # 1. 先按分数倒序排列
    valid_list.sort(key=lambda x: x['score'], reverse=True)
    
    # 2. 保存文件：增加分组标签
    update_ts = get_now()
    m3u_p = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
    txt_p = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
    
    with open(m3u_p, 'w', encoding='utf-8') as fm:
        fm.write(f"#EXTM3U x-tvg-url=\"http://epg.51zmt.top:12489/e.xml\"\n") # 附带通用的 EPG 地址
        for c in valid_list:
            fm.write(f"#EXTINF:-1 tvg-name=\"{c['name']}\" group-title=\"{c['group']}\",{c['name']}\n{c['url']}\n")
            
    with open(txt_p, 'w', encoding='utf-8') as ft:
        # 按组聚类输出 TXT
        current_group = ""
        for c in valid_list:
            if c['group'] != current_group:
                current_group = c['group']
                ft.write(f"{current_group},#genre#\n")
            ft.write(f"{c['name']},{c['url']}\n")

    status = subs_status[sub_id]
    status["logs"].append(f"🏁 任务圆满完成 | 有效源: {len(valid_list)} | 耗时: {format_duration(time.time()-start_ts)}")
    status["running"] = False

# --- Flask 路由 ---
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
    info = subs_status.get(sub_id, {"running": False, "logs": [], "total":0, "current":0, "success":0})
    return jsonify({"running": info.get("running", False), "logs": info.get("logs", []), "total": info.get("total", 0), "current": info.get("current", 0), "success": info.get("success", 0)})

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
    update_global_scheduler(); app.run(host='0.0.0.0', port=5123)
