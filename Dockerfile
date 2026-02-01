FROM python:3.9-slim

# 1. 开启 Debian 的 contrib, non-free 和 non-free-firmware 软件源
# 兼容新旧两种 Debian 软件源格式
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/Components: main/Components: main contrib non-free non-free-firmware/g' /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's/main$/main contrib non-free non-free-firmware/g' /etc/apt/sources.list; \
    fi

# 2. 安装 ffmpeg、Intel 驱动和 VAAPI 运行环境
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libva-drm2 \
    libva2 \
    i965-va-driver \
    intel-media-va-driver-non-free \
    mesa-va-drivers \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5123
CMD ["python", "app.py"]
