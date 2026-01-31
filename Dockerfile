FROM python:3.9-slim
# 安装 ffmpeg 用于探测视频流
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 修改为 5123 端口
EXPOSE 5123
CMD ["python", "app.py"]
