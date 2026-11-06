# 开发模式：源码热重载（uvicorn --reload / watchmedo / nginx 直读挂载目录）
DEV_COMPOSE ?= docker compose
# 生产模式：显式指定 -f 后不会自动加载 docker-compose.override.yml
PROD_COMPOSE ?= docker compose -f docker-compose.yml -f docker-compose.prod.yml

.DEFAULT_GOAL := help

.PHONY: help dev prod up down restart logs ps watch rebuild reset test shell db-shell

help: ## 显示可用命令
	@echo "投标文件智能比对系统 —— 常用命令"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "示例："
	@echo "  make dev          # 启动开发环境（改代码即时生效）"
	@echo "  make logs         # 跟踪全部日志"
	@echo "  make watch        # 另开一个终端：依赖/Dockerfile 变化时自动重建镜像"

dev: ## 启动开发模式（热重载，源码直挂）
	$(DEV_COMPOSE) up -d --build
	@echo "开发环境已启动："
	@echo "  管理台   http://localhost:8888"
	@echo "  API 文档 http://localhost:8888/docs"

prod: ## 以生产模式启动（代码打进镜像，无热重载）
	$(PROD_COMPOSE) up -d --build
	@echo "生产环境已启动：http://localhost:8888"

up: dev ## 同 dev

down: ## 停止所有服务（保留数据）
	$(DEV_COMPOSE) down

restart: ## 重启后端两个服务（改到容器外的配置时使用）
	$(DEV_COMPOSE) restart api worker

logs: ## 跟踪全部服务日志
	$(DEV_COMPOSE) logs -f

ps: ## 查看服务状态
	$(DEV_COMPOSE) ps

watch: ## 监听依赖文件与 Dockerfile 变化并自动重建（另开终端运行）
	$(DEV_COMPOSE) watch

rebuild: ## 强制重建镜像并重启（改了 requirements.txt 后使用）
	$(DEV_COMPOSE) build --no-cache api worker
	$(DEV_COMPOSE) up -d

reset: ## 清空数据库与上传文件并重新初始化（会删除所有数据）
	$(DEV_COMPOSE) down -v
	$(DEV_COMPOSE) up -d --build

test: ## 在容器内运行后端测试
	$(DEV_COMPOSE) exec api pytest -q

shell: ## 进入后端容器
	$(DEV_COMPOSE) exec api bash

db-shell: ## 进入数据库容器
	$(DEV_COMPOSE) exec db psql -U bid -d bid_inspection
