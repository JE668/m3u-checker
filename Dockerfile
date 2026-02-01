FROM python:3.9-slim

# 1. 开启非自由源
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/Components: main/Components: main contrib non-free non-free-firmware/g' /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's/main$/main contrib non-free non-free-firmware/g' /etc/apt/sources.list; \
    fi

# 2. 安装 QSV 和 VAAPI 完整驱动链
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libva-drm2 \
    libva2 \
    i965-va-driver \
    intel-media-va-driver-non-free \
    mesa-va-drivers \
    libmfx1 \
    libmfx-gen1.1 \
    vainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5123
CMD ["python", "app.py"]
