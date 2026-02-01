# 📺 m3u-checker

[![Docker Image CI](https://github.com/je668/m3u-checker/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/je668/m3u-checker)
![Docker Pulls](https://img.shields.io/docker/pulls/je668/m3u-checker?color=blue)
![License](https://img.shields.io/github/license/je668/m3u-checker)

**m3u-checker** 是一款专为 NAS 用户设计的本地 IPTV 自动化检测与汇总工具。它能有效解决 GitHub Actions 部署时因网络环境导致的测速不准、连接超时等问题。通过在本地运行，你可以获得最真实的连通性、延迟及下载速度数据。

---

## ✨ 功能特性

- **🚀 真实本地测速**：基于本地网络环境，真实探测 IPTV 源的下载速度（Mbps）与网络延迟（ms）。
- **🔍 深度元数据探测**：利用 `FFmpeg` 自动获取视频流分辨率（4K/1080p/720p等）。
- **💎 硬件加速支持**：支持调用 Intel 核显（QuickSync/VAAPI）进行硬件加速解码，大幅降低探测时的 CPU 负载。
- **📍 地理位置追踪**：集成 `ip-api` 自动识别源服务器的地理位置及所属运营商（ISP）。
- **⏰ 自动化计划**：支持 **定时点执行**（如每天 08:00）或 **固定间隔执行**（如每隔 12 小时）。
- **🧵 多线程并发**：支持自定义并发线程数（1-20），极速完成上千个链接的探测。
- **🛠️ 订阅分发**：自动汇总有效源，生成标准 `iptv.m3u` 和 `iptv.txt`，并提供可直接用于播放器的**线上订阅链接**。
- **🛑 手动控制**：提供 Web UI 界面，支持手动启动、实时监控进度及一键取消任务。

---

## 🛠️ 快速部署

### 1. 使用 Docker Compose (推荐)

在你的 NAS 上创建目录并保存以下 `docker-compose.yml`：

```yaml
services:
  m3u-checker:
    image: ghcr.io/je668/m3u-checker:latest
    container_name: m3u-checker
    restart: always
    ports:
      - "5123:5123"
    environment:
      - TZ=Asia/Shanghai
      - USE_HWACCEL=true  # 是否开启 Intel 核显加速 (需映射设备)
    devices:
      - /dev/dri:/dev/dri # 映射 Intel 集显设备节点
    volumes:
      - ./iptv_data:/app/data
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

执行启动命令：
```bash
docker-compose up -d
```

### 2. GPU 硬件加速说明
本项目支持双 GPU 分流策略：
- **Intel UHD Graphics (集显)**：通过映射 `/dev/dri` 开启 VAAPI 加速，专门用于视频流探测。
- **NVIDIA GPU (独显)**：本项目默认不占用 NVIDIA 显卡，将其完整预留给你的 AI 或其他计算任务。

---

## 🖥️ 使用指南

1. **访问界面**：浏览器打开 `http://NAS-IP:5123`。
2. **配置配置**：
   - 在左侧输入框粘贴你的原始 M3U 或 TXT 订阅链接（每行一个）。
   - 设置并发线程数（建议 5-10）。
   - 选择自动化模式（定时点或时间间隔）。
3. **开始检测**：点击 **保存配置** 后点击 **执行检测**。
4. **播放器订阅**：
   - 任务完成后，复制界面上的 **播放器订阅地址**。
   - 填入 Televizo, TiviMate, PotPlayer 或其他支持 M3U 的播放器中。

---

## 📝 日志说明

在监控终端中，你将看到以下格式的实时日志：

- ✅ **成功**：`✅ 频道名: 💎1920x1080 | ⏱️45ms | 🚀15.2Mbps | 📍中国 广东 广州 | 🔌1.2.3.4:80`
- ❌ **失败**：`❌ 频道名: 连接失败 | 🔌5.6.7.8:8080`

> **注**：💎 表示该探测由 Intel 硬件加速完成。

---

## ⚠️ 注意事项

1. **API 限制**：地理位置查询使用 `ip-api.com` 免费版，限制为 45 次请求/分钟。程序内置了 1.33 秒/个的节流保护及 IP 缓存机制，请勿频繁频繁重启任务以防 IP 被封禁。
2. **线程数**：线程数开启过多可能会导致小运营商宽带被临时限速或导致 NAS 系统 I/O 压力过大，请根据实际情况调整。

---

**享受你的本地 IPTV 自动化之旅吧！** 🍿
