# m3u-checker
🚀 部署说明 (Deployment Guide)
方法一：使用 Docker Compose (推荐)
下载本仓库的 docker-compose.yml。
在该文件所在目录执行：
code
Bash
docker-compose up -d
打开浏览器访问：http://NAS_IP:5123
方法二：手动 Docker Run
code
Bash
docker run -d \
  --name m3u-checker \
  -p 5123:5123 \
  -v /你的路径/data:/app/data \
  --restart always \
  ghcr.io/${YOUR_GITHUB_USERNAME}/m3u-checker:latest
📋 功能特性
本地网络探测：规避 GitHub Action 网络限制，真实测试本地到 IPTV 源的连通性。
FFmpeg 深度分析：获取每个链接的真实分辨率、连接延迟。
地理位置 & 运营商：集成 ip-api 获取服务器归属地及 ISP 信息。
智能节流：内置 1.33s 查询间隔及 IP 缓存机制，保护 API 不被封禁，同时大幅提升同服务器检测速度。
Web UI：支持在线编辑订阅源、实时查看检测日志、下载生成后的 M3U/TXT 文件。
