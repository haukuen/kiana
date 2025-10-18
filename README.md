## 快速开始

### 前置要求

在开始之前，请确保已安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

### 源码部署

1. 安装 nb-cli
```bash
uv tool install nb-cli
```

2. 克隆项目并安装依赖
```bash
git clone https://github.com/HauKuen/kiana.git
cd kiana
uv sync
```

3. 配置和运行
```bash
# 1. 配置 .env 文件
# 2. 运行项目
nb run
```

### Docker 部署
创建 `.env.prod` 文件并根据需要修改配置。

```yaml
services:
  kiana:
    container_name: kiana
    image: ghcr.exusiai.top/haukuen/kiana:latest
    ports:
      - "${PORT:-8080}:${PORT:-8080}"
    environment:
      HOST: "${HOST:-0.0.0.0}"
    volumes:
      - ./data:/app/data
      - ./.env.prod:/app/.env.prod:ro
    restart: always
```


## 文档

详细信息请参考 [NoneBot 官方文档](https://nonebot.dev/)
