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

# --- 路径与文件配置 ---
DATA_DIR = "/app/data"
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
MASTER_LOG = os.path.join(DATA_DIR, "log.txt")  # 物理日志文件
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

def write_master_log(content):
    """实时写入物理日志文件供宿主机查看"""
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
    """解析获取 IP:端口"""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return f"{host}:{port}"
    except: return "未知接口"

def get_ip_info(url):
    """带频率保护的地理位置抓取"""
    try:
        hostname = urlparse(url).hostname
        ip = socket.gethostbyname(hostname)
        if ip in ip_cache: return ip_cache[ip]
        with api_lock:
            time.sleep(1.33)
            res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5, verify=False).json()
            if res.get('status') == 'success':
                info = {
                    "city": res.get('city', '未知城市'),
                    "isp": res.get('isp', '未知网络')
                }
                ip_cache[ip] = info
                return info
        return None
    except: return None

def probe_stream(url, use_hw):
    """智能硬件加速探测 (支持 QSV/VAAPI 自动回退)"""
    accel_type = os.getenv("HW_ACCEL_TYPE", "qsv").lower()
    device = os.getenv("QSV_DEVICE") or os.getenv("VAAPI_DEVICE") or "/dev/dri/renderD128"
    
    if use_hw:
        try:
            if accel_type in ["quicksync", "qsv"]:
                hw_args = ['-hwaccel', 'qsv', '-qsv_device', device, '-hwaccel_output_format', 'qsv']
                icon = "⚡"
            else:
                hw_args = ['-hwaccel', 'vaapi', '-hwaccel_device', device, '-hwaccel_output_format', 'vaapi']
                icon = "💎"
            
            cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0',
                   '-probesize', '5000000', '-analyzeduration', '5000000'] + hw_args + ['-i', url]
            # 增加 15s 强制超时防止 ffprobe 进程僵死
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                return json.loads(result.stdout)['streams'][0], icon
        except: pass 

    # CPU 软件探测 (回退)
    try:
        cmd_cpu = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0', '-i', url]
        result = subprocess.run(cmd_cpu, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return json.loads(result.stdout)['streams'][0], "💻"
    except: pass
    return None, "❌"

def test_single_channel(sub_id, name, url, use_hw):
    """单个频道探测：测延迟、测速、探测分辨率、记录日志"""
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    
    host_port = get_source_info(url)
    
    try:
        start_time = time.time()
        # 1. 测延迟
        resp = requests.get(url, stream=True, timeout=8, verify=False)
        latency = int((time.time() - start_time) * 1000)
        
        # 2. 测速 (下载2秒数据块)
        total_data, speed_start = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: 
                resp.close()
                return None
            total_data += len(chunk)
            if time.time() - speed_start > 2: break
        speed = round((total_data * 8) / ((time.time() - speed_start) * 1024 * 1024), 2)
        resp.close()

        # 3. 探测视频元数据
        video, icon = probe_stream(url, use_hw)
        if not video: raise Exception("Probe Failed")
        
        res_str = f"{video.get('width')}x{video.get('height')}"
        geo = get_ip_info(url)
        city = geo['city'] if geo else "未知城市"
        isp = geo['isp'] if geo else "未知网络"
        
        detail = f"{icon}{res_str} | ⏱️{latency}ms | 🚀{speed}Mbps | 📍{city} | 🏢{isp} | 🔌{host_port}"
        
        # 写入物理日志
        write_master_log(f"[{status['sub_name']}] ✅ {name}: {detail} (URL: {url})")

        with log_lock:
            if not status["stop_requested"]:
                status["success"] += 1
                # 记录汇总数据
                status["summary_host"][host_port] = status["summary_host"].get(host_port, {"t":0, "s":0})
                status["summary_host"][host_port]["s"] += 1
                status["summary_city"][city] = status["summary_city"].get(city, {"t":0, "s":0})
                status["summary_city"][city]["s"] += 1
                status["logs"].append(f"✅ {name}: {detail}")
        return {"name": name, "url": url}

    except Exception as e:
        geo = get_ip_info(url)
        city = geo['city'] if geo else "未知城市"
        write_master_log(f"[{status['sub_name']}] ❌ {name}: 连接失败 (URL: {url})")
        with log_lock:
            if not status["stop_requested"]:
                status["summary_host"][host_port] = status["summary_host"].get(host_port, {"t":0, "s":0})
                status["summary_city"][city] = status["summary_city"].get(city, {"t":0, "s":0})
                status["logs"].append(f"❌ {name}: 连接失败 | 🔌{host_port}")
        return None
    finally:
        with log_lock:
            status["current"] += 1
            if host_port in status["summary_host"]: status["summary_host"][host_port]["t"] += 1
            if city in status["summary_city"]: status["summary_city"][city]["t"] += 1

def run_task(sub_id):
    """探测主任务流程"""
    config = load_config()
    sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub or subs_status.get(sub_id, {}).get("running"): return

    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0,
        "sub_name": sub['name'],
        "logs": [f"🎬 [{get_now()}] 探测任务启动..."],
        "summary_host": {}, "summary_city": {}
    }
    
    use_hw = os.getenv("USE_HWACCEL", "false").lower() == "true"
    raw_channels = []
    
    # 1. 获取并解析订阅源内容
    try:
        r = requests.get(sub["url"], timeout=15, verify=False)
        r.encoding = r.apparent_encoding
        text = r.text
        if "#EXTINF" in text:
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if "#EXTINF" in line:
                    name = line.split(',')[-1].strip()
                    for j in range(i+1, min(i+5, len(lines))):
                        u = lines[j].strip()
                        if u.startswith("http"):
                            raw_channels.append((name, u)); break
        else:
            for line in text.split('\n'):
                if "," in line and "http" in line:
                    p = line.split(',')
                    if len(p) >= 2: raw_channels.append((p[0].strip(), p[1].strip()))
    except Exception as e:
        subs_status[sub_id]["logs"].append(f"❌ 获取源失败: {e}")
        subs_status[sub_id]["running"] = False
        return

    raw_channels = list(set(raw_channels)) # 去重
    total_count = len(raw_channels)
    subs_status[sub_id]["total"] = total_count
    
    valid_list = []
    thread_num = int(sub.get("threads", 5))
    
    # 2. 多线程探测执行
    with ThreadPoolExecutor(max_workers=thread_num) as executor:
        futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
        for f in futures:
            if subs_status[sub_id]["stop_requested"]:
                for fut in futures: fut.cancel()
                break
            try:
                res = f.result(timeout=20)
                if res: valid_list.append(res)
            except: pass

    # 3. 结果汇总与文件保存 (无论正常结束还是手动停止都执行)
    status = subs_status[sub_id]
    
    # 接口汇总报告
    status["logs"].append(" ")
    status["logs"].append("======================================================")
    status["logs"].append("📊 --- 接口服务质量汇总 (IP:PORT) ---")
    sorted_host = sorted(status["summary_host"].items(), key=lambda x: x[1]['s']/x[1]['t'] if x[1]['t']>0 else 0, reverse=True)
    for h, d in sorted_host:
        rate = round(d['s']/d['t']*100, 1) if d['t']>0 else 0
        status["logs"].append(f"📡 {h:<28} | 有效率: {rate:>5}% ({d['s']}/{d['t']})")

    # 城市汇总报告
    status["logs"].append(" ")
    status["logs"].append("🏙️ --- 城市/区域连通性汇总 ---")
    sorted_city = sorted(status["summary_city"].items(), key=lambda x: x[1]['s']/x[1]['t'] if x[1]['t']>0 else 0, reverse=True)
    for c, d in sorted_city:
        rate = round(d['s']/d['t']*100, 1) if d['t']>0 else 0
        status["logs"].append(f"📍 {c:<30} | 有效率: {rate:>5}% ({d['s']}/{d['t']})")
    status["logs"].append("======================================================")

    # 保存 M3U 和 TXT 文件
    m3u_path = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
    txt_path = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
    with open(m3u_path, 'w', encoding='utf-8') as fm, open(txt_path, 'w', encoding='utf-8') as ft:
        fm.write("#EXTM3U\n")
        for c in valid_list:
            fm.write(f"#EXTINF:-1,{c['name']}\n{c['url']}\n")
            ft.write(f"{c['name']},{c['url']}\n")
            
    if status["stop_requested"]:
        status["logs"].append(f"🛑 [{get_now()}] 任务已手动强行停止。")
    else:
        status["logs"].append(f"🏁 [{get_now()}] 任务圆满完成。")
        
    status["running"] = False

# --- Flask 路由逻辑 ---

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
def start_task(sub_id):
    if subs_status.get(sub_id, {}).get("running"): return jsonify({"status": "running"})
    threading.Thread(target=run_task, args=(sub_id,)).start()
