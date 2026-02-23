# 📺 m3u-checker Pro 

[![Docker Image CI](https://github.com/je668/m3u-checker/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/je668/m3u-checker)
![Version](https://img.shields.io/badge/version-2.5.0-blue.svg)
![Platform](https://img.shields.io/badge/platform-Docker-green.svg)

**m3u-checker Pro** 是一款专为 NAS 用户设计的**全能型本地 IPTV 自动化探测与监控仪表盘**。通过在本地运行，它能完美避开 GitHub Actions 等云端环境的网络限制，利用您真实的宽带环境筛选出画质最高、速度最快的直播源。

---

## ✨ 核心特性

- **🚀 工业级探测引擎**：支持多线程并发（1-20+），内置 `ffprobe` 深度分析及 `ffmpeg` 网络重连协议，确保探测结果 100% 真实。
- **📊 智能质量评分 (Ranking)**：独家评分算法：Score = 高度 + (实测码率*10) - (延迟 \10)。自动将画质最好、速度最快的源排在订阅文件首位。
- **📈 实时可视化分析 (Analytics)**：
  - **画质占比**：4K/1080P/720P 分布统计。
  - **连通稳定性**：成功、失败及熔断比例饼图。
  - **延迟分布**：连接响应速度阶梯统计。
  - **编码构成**：H.264 / HEVC / MPEG2 技术分布分析。
- **🛡️ 智能熔断机制 (Circuit Breaker)**：当某一 Host（IP:端口）连续失败超过 **10 次**时，自动判定该节点失效并跳过后续所有链接，极速缩短扫描任务耗时。
- **💎 硬件加速支持**：深度适配 **Intel UHD Graphics (QSV/VAAPI)**。支持在 Web 界面一键开启硬解探测，并内置硬件环境诊断分析仪。
- **📅 自动化维护**：支持多任务独立管理。可配置**定时点执行**（如每天 08:00/20:00）或**时间间隔循环**（如每 12 小时）。
- **🔍 灵活过滤系统**：支持按分辨率（SD、720P、1080P、4K、8K）进行勾选保留，不符合要求的源将不计入最终订阅。
- **🔗 完美订阅分发**：
  - 自动生成纯净的 `.m3u`（支持分组及台标）和 `.txt` 文件。
  - 支持自定义 **EPG 接口** 和 **高清台标库**。
- **📝 持久化日志库**：自动按天生成结构化 **CSV 日志**（`/data/log/log_YYYY-MM-DD.csv`），支持 Excel 直接处理。

---

## 🛠️ 快速部署

### 1. 准备 `docker-compose.yml`

```yaml
version: '3.8'
services:
  m3u-checker:
    image: ghcr.nju.edu.cn/je668/m3u-checker:latest
    container_name: m3u-checker
    restart: always
    network_mode: host # 推荐使用 host 模式以获得最佳网络探测精度
    user: "0:0"
    privileged: true   # 开启特权模式以调用硬件加速
    environment:
      - TZ=Asia/Shanghai
      - USE_HWACCEL=true 
      - LIBVA_DRIVER_NAME=iHD
      - HW_ACCEL_TYPE=vaapi # 可选 vaapi 或 qsv
      - VAAPI_DEVICE=/dev/dri/renderD128
    devices:
      - /dev/dri:/dev/dri # 映射 Intel 核显
    volumes:
      - /你的路径/data:/app/data
```

### 2. 启动容器
```bash
docker-compose up -d
```
访问地址：`http://NAS-IP:5123`

---

## 🖥️ 仪表盘说明

### 1. 任务监控矩阵
- **总频道数**：订阅源中解析出的原始链接总量。
- **已处理**：当前已完成探测的数量。
- **有效源数**：通过连通性及元数据校验的成功源。
- **已熔断数**：因连续失败被系统自动封禁的接口服务器数量。
- **当前效率**：有效源占已处理源的百分比。

### 2. 智能日志终端
- **✅ 绿色**：探测成功，展示：`分辨率 | 编码 | FPS | 码率 | 延迟 | 网速 | 地理位置 | 接口`。
- **❌ 红色**：探测失败，展示：`失败原因 | 接口`。
- **⚠️ 黄色**：熔断报警，展示：`接口 IP 因连续失败被禁封`。
- **智能滚动**：手动向上翻阅查看汇总报告时，日志会自动停止滚动；滚回底部时恢复自动追踪。

---

## ⚙️ 高级配置

### 硬件加速诊断
在 **[全局设置]** 菜单中，点击 **“运行 vainfo 诊断”**。
- 系统会自动分析驱动环境。
- 若显示 **“✅ GPU硬件加速已就绪”**，则代表您的显卡（如 UHD 620）已参与视频探测。
- 下方将列出支持的硬件编解码列表（H264, HEVC, VP9 等）。

### 分辨率过滤器
在 **[任务配置]** 中，您可以自由勾选需要保留的画质档位。例如，您可以专门创建一个“4K 极清任务”，只勾选 4K 和 8K，系统会自动为您剔除所有低画质链接。

---

## 📁 目录结构与数据
- `/data/config.json`：保存您的所有任务配置与系统全局设置。
- `/data/output/`：存放生成的 `.m3u` 和 `.txt` 订阅文件。
- `/data/log/`：按天存放 `.csv` 详细探测记录，方便离线分析。

---

## ⚠️ 注意事项
1. **API 频率限制**：地理位置查询使用 `ip-api.com` 免费版，限制为 45 次请求/分钟。程序内置了 1.35s 的节流保护及 IP 缓存机制，请勿频繁重启大型任务。
2. **硬件兼容性**：VAAPI 硬件加速在探测部分 AVS+ / AVS2 编码的源时可能会回退到 CPU 模式，这是驱动层面的正常现象。

---

## 🤝 贡献与支持
如果你在使用中发现 Bug 或有更好的功能建议，欢迎提交 Issues。

**享受属于你的纯净、高速 IPTV 体验！** 🍿
