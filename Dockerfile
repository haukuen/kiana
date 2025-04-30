FROM python:3.12-slim-bookworm

# 安装中文字体包
RUN apt-get update && apt-get install -y fonts-noto-cjk && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.6.17 /uv /uvx /bin/

RUN pip3 --no-cache-dir install nb-cli

ENV TZ=Asia/Shanghai

WORKDIR /app

COPY . /app/

RUN uv sync --no-dev

CMD ["nb", "run"]
