FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.6.17 /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 --no-cache-dir install nb-cli

ENV TZ=Asia/Shanghai

WORKDIR /app

COPY . /app/

RUN uv sync --no-dev

CMD ["nb", "run"]