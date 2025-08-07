FROM --platform=$BUILDPLATFORM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.5 /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 --no-cache-dir install nb-cli

ENV TZ=Asia/Shanghai
ENV LOCALSTORE_USE_CWD=true

WORKDIR /app

COPY . /app/

RUN uv sync --locked

CMD ["nb", "run"]
