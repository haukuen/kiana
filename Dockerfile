FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.6.17 /uv /uvx /bin/

RUN pip3 --no-cache-dir install nb-cli

ENV TZ=Asia/Shanghai

WORKDIR /app

COPY . /app/

RUN uv sync --no-dev

RUN nb orm upgrade

RUN uv run playwright install chromium --with-deps

CMD ["nb", "run"]