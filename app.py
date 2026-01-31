import os, subprocess, json, threading, time, socket, datetime
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory
from urllib.parse import urlparse
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# 配置路径
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
OUTPUT_M3U = os.path.join(DATA_DIR, "iptv.m3u")
OUTPUT_TXT = os.path.join(DATA_DIR, "iptv.txt")

# 全局状态
task_status = {
    "running": False,
    "total": 0,
    "current": 0,
    "success": 0,
    "logs": [],
    "next_run": "未设置"
}

ip_cache = {}
scheduler = BackgroundScheduler()
scheduler.start()

def get_ip_detailed_info(url):
    """获取IP位置及运营商 (1.33s 节流 + 缓存)"""
    try:
        hostname = urlparse(url).hostname
        ip = socket.gethostbyname(hostname)
        if ip in ip_cache: return ip_cache[ip]
        
        time.sleep(1.33) 
        res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5).json()
        if res.get('status') == 'success':
            info = f"{res.get('country','')} {res.get('regionName','')} {res.get('city','')} | {res.get('isp','')}"
            ip_cache[ip] = info
            return info
        return "位置频率受限 | 未知网络"
    except:
        return "IP解析失败 | 未知网络"

def run_task():
    """核心检测任务"""
    global task_status
    if task_status["running"]: return
    
    task_status["running"] = True
    task_status["logs"] = [f"⏰ [{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 任务自动启动..."]
    task_status["current"] = 0
    task_status["success"] = 0
    
    if not os.path.exists(CONFIG_FILE):
        task_status["running"] = False
        return

    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
        sub_urls = config.get("urls", [])

    all_channels = []
    for sub_url in sub_urls:
        try:
            resp = requests.get(sub_url, timeout=15)
            lines = resp.text.split('\n')
            temp_list = []
            for i, line in enumerate(lines):
                if line.startswith("#EXTINF"):
                    name = line.split(',')[-1].strip()
                    next_line = i + 1
                    while next_line < len(lines) and not lines[next_line].strip().startswith("http"):
                        next_line += 1
                    if next_line < len(lines):
                        temp_list.append((name, lines[next_line].strip()))
            
            task_status["total"] = len(temp_list)
            for name, url in temp_list:
                task_status["current"] += 1
                # 调用 ffprobe
                cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'v:0', '-i', url, '-timeout', '10000000']
                try:
                    result = subprocess.check_output(cmd).decode('utf-8')
                    video = json.loads(result)['streams'][0]
                    res_str = f"{video.get('width')}x{video.get('height')}"
                    geo = get_ip_detailed_info(url)
                    detail = f"{res_str} | {geo}"
                    task_status["success"] += 1
                    task_status["logs"].append(f"✅ [{task_status['current']}] {name}: {detail}")
                    all_channels.append({"name": name, "url": url, "detail": detail})
                except:
                    task_status["logs"].append(f"❌ [{task_status['current']}] {name}: 连接失败")
        except Exception as e:
            task_status["logs"].append(f"⚠️ 订阅拉取失败: {str(e)}")

    # 保存文件
    with open(OUTPUT_M3U, 'w', encoding='utf-8') as f_m3u, open(OUTPUT_TXT, 'w', encoding='utf-8') as f_txt:
        f_m3u.write("#EXTM3U\n")
        for c in all_channels:
            f_m3u.write(f"#EXTINF:-1,{c['name']} [{c['detail']}]\n{c['url']}\n")
            f_txt.write(f"{c['name']},{c['url']}\n")
    
    task_status["logs"].append("🏁 任务完成！")
    task_status["running"] = False

def update_scheduler():
    """更新定时任务设置"""
    if not os.path.exists(CONFIG_FILE): return
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    
    scheduler.remove_all_jobs()
    mode = config.get("schedule_mode", "none")
    
    if mode == "fixed":
        times = config.get("fixed_times", "").split(',')
        for t in times:
            t = t.strip()
            if t:
                # hh:mm 格式
                h, m = t.split(':')
                scheduler.add_job(run_task, 'cron', hour=h, minute=m, id=f"fixed_{t}")
    elif mode == "interval":
        hours = int(config.get("interval_hours", 12))
        scheduler.add_job(run_task, 'interval', hours=hours, id="interval_job")
    
    # 更新下次执行时间
    jobs = scheduler.get_jobs()
    if jobs:
        task_status["next_run"] = jobs[0].next_run_time.strftime('%Y-%m-%d %H:%M:%S')
    else:
        task_status["next_run"] = "未启用"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/settings', methods=['POST'])
def save_settings():
    data = request.json
    # data 包含: urls, schedule_mode, fixed_times, interval_hours
    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f)
    update_scheduler()
    return jsonify({"status": "success", "next_run": task_status["next_run"]})

@app.route('/status')
def get_status():
    return jsonify(task_status)

@app.route('/start')
def start_manual():
    threading.Thread(target=run_task).start()
    return "started"

@app.route('/download/<path:filename>')
def download(filename):
    return send_from_directory(DATA_DIR, filename)

if __name__ == '__main__':
    # 初始化启动定时器
    update_scheduler()
    app.run(host='0.0.0.0', port=5123)
