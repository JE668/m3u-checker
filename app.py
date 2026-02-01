import os, subprocess, json, threading, time, socket, datetime, uuid
import requests
import urllib3
from flask import Flask, render_template, request, jsonify, send_from_directory, make_response, redirect
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

# 屏蔽 SSL 安全警告，保持日志清爽
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# --- 路径与存储配置 ---
DATA_DIR = "/app/data"
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# --- 全局状态记录 ---
# subs_status 存储结构: { sub_id: { running, stop_requested, total, current, success, logs, summary } }
subs_status = {}
ip_cache = {}
api_lock = threading.Lock()
log_lock = threading.Lock()
scheduler = BackgroundScheduler()
scheduler.start()

def load_config():
    if not os.path.exists(CONFIG_FILE): return {"subscriptions": []}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return {"subscriptions": []}

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def get_source_info(url):
    """解析 URL 返回 IP:端口"""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return f"{host}:{port}"
    except:
        return "未知接口"

def get_ip_info(url):
    """带频率限制的地理位置查询"""
    try:
        hostname = urlparse(url).hostname
        ip = socket.gethostbyname(hostname)
        if ip in ip_cache: return ip_cache[ip]
        with api_lock:
            time.sleep(1.33)
            res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5, verify=False).json()
            if res.get('status') == 'success':
                info = f"📍{res.get('city','')} | 🏢{res.get('isp','')}"
                ip_cache[ip] = info
                return info
        return "📍未知位置"
    except: return "📍解析失败"

def probe_stream(url, use_hw):
    """智能探测：尝试 GPU，失败则回退 CPU"""
    accel_type = os.getenv("HW_ACCEL_TYPE", "qsv").lower()
    device = os.getenv("QSV_DEVICE") or os.getenv("VAAPI_DEVICE") or "/dev/dri/renderD128"
    
    if use_hw:
        try:
            if accel_type in ["quicksync", "qsv"]:
                # QSV 初始化参数
                hw_args = ['-hwaccel', 'qsv', '-qsv_device', device, '-hwaccel_output_format', 'qsv']
                icon = "⚡"
            else:
                # VAAPI 初始化参数
                hw_args = ['-hwaccel', 'vaapi', '-hwaccel_device', device, '-hwaccel_output_format', 'vaapi']
                icon = "💎"

            cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0',
                   '-probesize', '10000000', '-analyzeduration', '10000000'] + hw_args + ['-i', url]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if 'streams' in data and len(data['streams']) > 0:
                    return data['streams'][0], icon
        except:
            pass 

    # 软件探测回退 (CPU)
    cmd_cpu = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0', '-i', url, '-timeout', '5000000']
    try:
        out = subprocess.check_output(cmd_cpu, stderr=subprocess.STDOUT).decode('utf-8')
        return json.loads(out)['streams'][0], "💻"
    except:
        return None, "❌"

def test_single_channel(sub_id, name, url, use_hw):
    """单频道检测核心函数"""
    status = subs_status[sub_id]
    if status["stop_requested"]: return None
    
    source_tag = get_source_info(url)
    
    # 初始化接口汇总统计
    with log_lock:
        if source_tag not in status["summary"]:
            status["summary"][source_tag] = {"total": 0, "success": 0}
        status["summary"][source_tag]["total"] += 1

    start_time = time.time()
    try:
        # 1. 测延迟
        resp = requests.get(url, stream=True, timeout=5, verify=False)
        latency = int((time.time() - start_time) * 1000)
        
        # 2. 测速 (下载2秒数据)
        total_data, speed_start = 0, time.time()
        for chunk in resp.iter_content(chunk_size=128*1024):
            if status["stop_requested"]: 
                resp.close()
                return None
            total_data += len(chunk)
            if time.time() - speed_start > 2: break
        speed = round((total_data * 8) / ((time.time() - speed_start) * 1024 * 1024), 2)
        resp.close()

        # 3. 分辨率探测
        video, icon = probe_stream(url, use_hw)
        if not video: raise Exception("Probe failed")
        
        res_str = f"{video.get('width')}x{video.get('height')}"
        geo = get_ip_info(url)
        
        with log_lock:
            if not status["stop_requested"]:
                status["success"] += 1
                status["current"] += 1
                status["summary"][source_tag]["success"] += 1
                status["logs"].append(f"✅ {name}: {icon}{res_str} | ⏱️{latency}ms | 🚀{speed}Mbps | {geo} | 🔌{source_tag}")
        return {"name": name, "url": url}
    except:
        with log_lock:
            if not status["stop_requested"]:
                status["current"] += 1
                status["logs"].append(f"❌ {name}: 连接失败 | 🔌{source_tag}")
        return None

def run_task(sub_id):
    """订阅源检测任务主流程"""
    config = load_config()
    sub = next((s for s in config["subscriptions"] if s["id"] == sub_id), None)
    if not sub: return

    # 状态初始化 & 防重复运行
    if subs_status.get(sub_id, {}).get("running"): return
    
    subs_status[sub_id] = {
        "running": True, "stop_requested": False, "total": 0, "current": 0, "success": 0,
        "logs": [f"🎬 [{datetime.datetime.now().strftime('%H:%M:%S')}] 任务启动..."],
        "summary": {}
    }
    
    use_hw = os.getenv("USE_HWACCEL", "false").lower() == "true"
    
    # 1. 解析源内容
    raw_channels = []
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
        subs_status[sub_id]["logs"].append(f"❌ 解析源失败: {str(e)}")
        subs_status[sub_id]["running"] = False
        return

    raw_channels = list(set(raw_channels))
    subs_status[sub_id]["total"] = len(raw_channels)
    
    # 2. 多线程检测
    valid_list = []
    thread_num = int(sub.get("threads", 5))
    with ThreadPoolExecutor(max_workers=thread_num) as executor:
        futures = [executor.submit(test_single_channel, sub_id, n, u, use_hw) for n, u in raw_channels]
        for f in futures:
            if subs_status[sub_id]["stop_requested"]:
                for fut in futures: fut.cancel() # 取消未开始的任务
                break
            try:
                res = f.result()
                if res: valid_list.append(res)
            except: pass

    # 3. 任务结束处理
    status = subs_status[sub_id]
    if status["stop_requested"]:
        status["logs"].append("🛑 任务已被用户手动强行停止。")
    else:
        # 生成接口汇总报告
        status["logs"].append(" ")
        status["logs"].append("📊 --- 接口探测汇总报告 ---")
        status["logs"].append(f"{'接口 (IP:端口)':<30} | {'探测数':<6} | {'有效数':<6} | {'有效率'}")
        status["logs"].append("-" * 65)
        
        sorted_summary = sorted(status["summary"].items(), key=lambda x: (x[1]['success']/x[1]['total']), reverse=True)
        for host, data in sorted_summary:
            rate = round((data['success'] / data['total']) * 100, 1)
            status["logs"].append(f"{host:<32} | {data['total']:<8} | {data['success']:<8} | {rate}%")
        status["logs"].append("-" * 65)

        # 保存纯净结果
        m3u_path = os.path.join(OUTPUT_DIR, f"{sub_id}.m3u")
        txt_path = os.path.join(OUTPUT_DIR, f"{sub_id}.txt")
        with open(m3u_path, 'w', encoding='utf-8') as fm, open(txt_path, 'w', encoding='utf-8') as ft:
            fm.write("#EXTM3U\n")
            for c in valid_list:
                fm.write(f"#EXTINF:-1,{c['name']}\n{c['url']}\n")
                ft.write(f"{c['name']},{c['url']}\n")
        status["logs"].append(f"🏁 任务正常完成，有效源: {len(valid_list)}")
    
    status["running"] = False

# --- Flask 路由控制 ---

@app.route('/')
def index(): return render_template('index.html')

@app.route('/live.m3u')
def legacy_m3u():
    config = load_config()
    if config["subscriptions"]:
        return redirect(f"/sub/{config['subscriptions'][0]['id']}.m3u")
    return "No subscription found", 404

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
    # 启动前严谨校验
    if subs_status.get(sub_id, {}).get("running"):
        return jsonify({"status": "error", "message": "Task already running"})
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
