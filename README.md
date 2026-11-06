# 投标文件智能比对与风险分析系统（V1 框架）

依据 [prd.md](prd.md) / [prd_v2.md](prd_v2.md) 实现的 Python 全栈项目框架，跑通 PRD 定义的完整业务闭环：

> 创建项目 → 上传招标文件 → 创建投标企业 → 上传企业文件 → 人工排序 → 解析（解析完成后自动开始两两比对）→ 查看比对进展 → 企业两两比对 → 三栏查看证据 → 人工审核 → 导出报告。

同时提供 PRD v2 定义的**系统级三大模块**：**工作台 / 风险管理 / 系统管理**，一级菜单为
`工作台 · 项目管理 · 风险管理 · 系统管理`，登录后默认进入工作台。

V1 不接入真实模型或 OCR：文档解析、文本相似、人员重复、价格异常均为**可运行的确定性规则/轻量算法**，接口与数据结构按后续替换模型的方式预留。重点验证的是框架、数据模型、证据溯源和页面闭环。

---

## 1. 快速启动（Docker）

```bash
cd /home/ubuntu/Bid_Inspection
cp .env.example .env        # 可选，不复制也能用默认值启动
docker compose up --build -d
```

> 镜像构建默认使用清华 PyPI 镜像，海外环境可在 `.env` 中设置 `PIP_INDEX_URL=https://pypi.org/simple` 后重建。

启动后：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8888 | 管理台（默认 `admin / admin123`） |
| http://localhost:8888/docs | Swagger API 文档 |
| http://localhost:8001/health | 后端健康检查 |

首次启动会创建管理员账号，并在 `SEED_DEMO=true`（默认）时生成演示项目「演示采购项目A」：3 家企业、招标文件、已解析文本、3 组两两比对结果，可直接进入三栏证据页体验。

> 生产部署前请修改 `.env` 中的 `SECRET_KEY`、`ADMIN_PASSWORD`、`POSTGRES_PASSWORD`，并将 `SEED_DEMO` 设为 `false`。

```bash
docker compose ps                 # 查看服务
docker compose logs -f api worker # 查看日志
docker compose down               # 停止（保留数据）
docker compose down -v            # 停止并清空数据库/文件（演示数据会重新生成）
```

服务组成：

```
浏览器 ── Nginx :8888 ──┬── 静态管理台（工作台/项目/解析/比对/审核/风险/系统/报告）
                        └── /api ── FastAPI :8001 ── PostgreSQL :5432
                                          │
                                          ├── Redis :6379 ── Celery Worker（文件解析、分析任务）
                                          └── /data/storage（原始文件，只写不覆盖）
```

---

## 2. 实时开发模式（改代码即时生效）

后端依然跑在 Docker 里，但**源码直接挂载进容器**，配合热重载做到「保存 → 刷新浏览器就看到结果」，不用每次 `docker compose build`。

这是 `docker compose` 的默认行为：`docker-compose.override.yml` 会被自动加载。

| 改动位置 | 生效方式 | 延迟 |
| --- | --- | --- |
| `backend/app/**/*.py` | uvicorn `--reload` 自动重载 | 1～2 秒 |
| `backend/app/**/*.py` | watchmedo 自动重启 Celery Worker | 1～2 秒 |
| `backend/app/models/*.py` 新增字段 | 自动 `ALTER TABLE ADD COLUMN` 补齐 | 随重载完成 |
| `frontend/*` | nginx 直接读挂载目录，刷新浏览器即生效 | 即时 |
| `backend/requirements*.txt`、`Dockerfile*` | 需要重建镜像（`make watch` 可自动） | 数分钟 |

```bash
make dev          # 启动开发环境（等价于 docker compose up -d --build）
make logs         # 跟踪日志
make watch        # 另开一个终端：依赖文件变化时自动重建镜像
make test         # 在容器内跑后端测试
make reset        # 清库重来（删列 / 改字段类型等破坏性变更后使用）
```

热重载链路：

```
宿主机 backend/app/x.py  ──(bind mount)──▶ 容器 /app/app/x.py
                                             │
                          uvicorn --reload ───┴──▶ 自动重启进程（日志出现 "Reloading..."）
                          watchmedo ──────────┴──▶ 重启 Celery Worker
```

### 两种部署模式的区别

| | 开发模式（默认） | 生产模式 |
| --- | --- | --- |
| 启动命令 | `docker compose up -d --build` | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` |
| 代码位置 | 宿主机 bind mount | 打进镜像 |
| 热重载 | 开启（API + Worker） | 关闭 |
| 镜像 | `backend/Dockerfile.dev`（含 pytest/watchdog） | `backend/Dockerfile` |
| `ENVIRONMENT` | `development` | `production` |
| 表结构自动同步 | 开启（仅新增列） | 关闭 |

> 显式指定 `-f` 时 `docker-compose.override.yml` 不会被自动加载，因此生产模式不会带上任何源码挂载。

### 开发期注意事项

- **Worker 重启会中断正在跑的任务**：解析/分析中途改了代码，Worker 会被重启，任务状态可能停留在 `running`。开发模式已开启 `ALLOW_RERUN_RUNNING_TASKS`，Worker 重启后会把停留在 running 的任务重新入队。
- **表结构变更只支持「加列」**：删列、改字段类型、加唯一约束等需要 `make reset` 重建数据库。
- **新增依赖后要重建镜像**：`pip install` 装到了容器里，重启容器就丢了。正确做法是改 `backend/requirements.txt` 后执行 `make rebuild`（或用 `make watch` 自动重建）。
- **前端没有构建步骤**：直接改 `frontend/app.js` 等文件即可，浏览器强制刷新（Ctrl+Shift+R）避免缓存。

---

## 3. 本地开发（不用 Docker）

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# 本地没有 Postgres/Redis 时，可用 SQLite + 同步任务快速验证：
export DATABASE_URL="sqlite:///./dev.db"
export STORAGE_ROOT="./storage"
export SYNC_TASKS=true
export SEED_DEMO=true
.venv/bin/uvicorn app.main:app --reload --port 8000
```

前端为无构建依赖的 HTML/CSS/JS，直接用 Nginx 容器运行；本地调试时可在 `frontend/` 下启动任意静态服务器，并把 `/api` 反代到对应 API 端口（本地 8000 / Docker 宿主机 8001）。

运行测试：

```bash
cd backend
.venv/bin/pytest -q
```

测试覆盖：端到端种子流程（解析 → 3 组比对 → 三级证据 → Excel 导出）、审核记录追加、20 家企业上限、文本规范化偏移映射；文本引擎的结构化分块、MinHash/LSH 召回、Jaccard/有效相似度、招标文件基准排除、局部对齐与块对结果入库；以及系统级模块——工作台防重复计数与待办、风险清单筛选分页与证据编号、批量审核限制、规则版本生命周期与阈值真正生效、操作日志、用户管理与管理员鉴权。

---

## 4. 实时结果展示

分析任务的进度通过 SSE 推送，不再依赖固定间隔轮询：

```
GET /api/v1/projects/{project_id}/events?token=<access_token>
```

后端每 1 秒比对一次快照（解析进度、任务状态、各维度完成数、证据数、待审核数），有变化才推送 `snapshot` 事件。前端在任务详情页订阅该流，进度条、状态和计数就地刷新；SSE 不可用时自动退回 3 秒轮询。

---

## 5. 页面结构

系统级一级菜单（PRD v2 §3.1）：

| 一级菜单 | 路由 | 内容 |
| --- | --- | --- |
| 工作台 | `#/workbench` | 全局统计卡片（可点击跳转）、待办事项、最近项目、最近分析任务、风险统计（PRD §4.1） |
| 项目管理 | `#/projects` | 项目列表、创建/编辑/归档，进入项目后有 7 个子页（见下） |
| 风险管理 | `#/risks` | 跨项目异常证据清单、筛选分页、证据详情、集中审核、批量标记（PRD §4.11） |
| 系统管理 | `#/system` | 检测规则版本、操作日志、用户管理（PRD §4.13） |

### 5.1 工作台（PRD §4.1）

- **全局统计卡片**：项目总数 / 进行中项目 / 已完成分析任务 / 异常证据总数 / 待审核证据，点击可跳到对应筛选后的列表。
- **待办事项**：解析失败、部分解析成功、尚未确认分析范围、分析任务失败、证据待审核、报告尚未导出；每条给出所属项目、处理入口与状态。
- **最近项目 / 最近分析任务**：含规则版本与算法版本。
- **风险统计**：按证据类型、风险等级、审核状态三个维度分布。

> 统计口径：所有证据计数都带 `is_current` 过滤且只数 `comparison_evidence` 行，**不 JOIN 参与企业表**——一条涉及 3 家企业的项目级价格证据只算 1 条（PRD §4.1.2 要求同一证据不得重复计数）。

### 5.2 风险管理（PRD §4.11）

- 证据列表字段：证据编号、所属项目、分析任务编号、证据类型、涉及企业、风险等级、发现时间、审核状态、审核人、操作。
- 筛选：项目 / 企业 / 证据类型 / 风险等级 / 审核状态 / 审核人员 / 发现时间区间。
- 证据编号按 PRD §5.7 计算：`项目编号-任务编号-证据类型-序号`，例 `DEMO-2026-001-T03-TEXT-0001`。序号在同一 (任务, 类型) 内跨历史行一起编号，重跑后编号不漂移。
- 详情弹窗展示证据基本信息、相关文件与原始页码、涉及企业、历史审核记录，并可跳转到所属项目的三栏比对详情。
- **批量操作仅允许「批量标记为待进一步核查」**；批量确认 / 批量排除按 PRD §4.11.5 明确禁用并给出提示。

### 5.3 系统管理（PRD §4.13）

- **检测规则版本**：按文本相似 / 人员重复 / 价格异常三个维度管理。每次修改**新建一个版本**（旧版本转 `archived` 保留），记录修改前/后内容、修改人、修改时间与修改原因；生效需要管理员权限 + 生效确认。
- **操作日志**：记录登录退出、项目增删改归档、文件上传下载替换删除、启动分析、重试任务、修改检测规则、审核证据、导出报告。操作结果 / 操作来源 / 变更前后信息写在 `audit_log.payload` JSON 中（不改表结构）。
- **用户管理**：新增 / 编辑 / 禁用 / 重置密码 / 分配角色。系统至少保留一个启用的管理员账号，管理员不能禁用自己。

### 5.4 规则版本真正生效

检测阈值存放在 `detection_rule_version` 表中，**分析任务启动时把生效版本固化进 `analysis_config`**：

```json
{
  "thresholds":   { "text_similarity": 0.6, "price_cv": 0.05, ... },
  "rule_versions": { "text": { "version_no": 3, "params": {...} }, ... }
}
```

运行期分析函数从任务快照读取阈值（`analysis/thresholds.py`），因此**规则被修改后，历史任务重跑仍使用启动时的那一版规则**。部署配置（`core/config.py`）只在首次初始化时作为出厂版本 v1 的种子。

### 5.5 项目内子页面

进入项目后有 7 个子页面（Tab）：

| 子页面 | 内容 |
| --- | --- |
| 项目概览 | 项目整体情况：企业/文件/解析进度、下一步操作推荐、最近分析任务 |
| 文件管理 | 招标文件（招标说明）与按公司管理的投标文件，支持排序、替换、重新解析 |
| 解析中心 | 招标说明与投标文件的解析进展、失败原因、重试 |
| 分析任务 | 项目内企业两两比对的进展状态：任务列表、维度实时进度、执行日志；文件解析完成后自动开始，无需人工勾选范围 |
| 比对分析 | **选取任意两家企业**查看三栏对比：左侧企业A原文、中间企业B原文、右侧证据列表与详情 |
| 比对结果 | 全部企业组合的统计概览：各维度证据数、风险等级分布、企业组合矩阵 |
| 分析报告 | 导出整体统计报告（Excel 四表） |

### 比对分析（三栏对比）

- **企业选择器**：任选两家企业切换组合，默认定位综合风险最高的组合。
- **右侧证据列**：通过颜色和标识区分两类信息——
  - 左侧色条表示人工审核状态（待审核 / 确认异常线索 / 正常排除 / 暂不判断）；
  - 「命中招标说明 · 公共内容已排除」标签表示该段雷同内容命中招标文件公共内容（已从有效结论中排除，但保留原始识别记录）。
- **筛选**：可按审核状态、风险等级、是否命中招标说明筛选，筛选只影响清单展示，不改变原始识别结果。
- **单独导出**：导出当前企业组合的证据 Excel（沿用四表结构，只含该组合的数据）。
- **人工审核**：审核动作直接在三栏右侧完成，不再单设「人工审核」页面；审核状态在证据清单、比对结果统计和 Excel 报告中统一体现。

## 6. 目录结构

```
Bid_Inspection/
├── docker-compose.yml            # 基础编排（服务、环境变量、数据卷）
├── docker-compose.override.yml   # 开发模式覆盖（自动加载：源码挂载 + 热重载）
├── docker-compose.prod.yml       # 生产模式覆盖（显式 -f 才生效）
├── Makefile                      # dev / prod / logs / watch / test / reset 等命令
├── .env.example
├── prd.md
├── prd_v2.md                      # 系统级三大模块（工作台/风险管理/系统管理）的产品需求
├── backend/
│   ├── Dockerfile                 # 生产镜像
│   ├── Dockerfile.dev             # 开发镜像（多装 pytest / watchdog）
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── entrypoint.sh
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口、异常处理、生命周期
│   │   ├── bootstrap.py            # 建表 + 管理员 + 演示数据（容器启动执行）
│   │   ├── seed.py                 # 演示项目（幂等）
│   │   ├── core/config.py          # 环境配置、阈值、上传限制
│   │   ├── core/security.py        # JWT / 密码哈希
│   │   ├── db/                     # Engine、Session、初始化（含开发期加列同步）
│   │   ├── models/                 # 全部 SQLAlchemy 实体（UUID 主键）
│   │   ├── schemas/                # 请求/响应 DTO
│   │   ├── api/routes/             # auth / projects / companies / files / analysis / results / events / workbench / risks / system
│   │   ├── services/               # 业务编排（文件、逻辑标书、分析、结果、报告、规则、工作台、风险、系统）
│   │   ├── analysis/               # 文本相似、人员、价格、风险规则 + 阈值读取（thresholds.py）
│   │   ├── workers/                # Celery app、任务、同步/异步分发
│   │   ├── storage/                # 存储抽象 + 本地实现（可换 MinIO）
│   │   ├── utils/text.py           # 规范化 + 原文偏移映射
│   │   └── services/parser.py      # PDF/DOCX/TXT 解析适配
│   └── tests/
└── frontend/
    ├── index.html                  # 管理台单页（hash 路由）
    ├── app.js
    ├── styles.css
    ├── nginx.conf
    └── Dockerfile
```

---

## 7. 已落地的 PRD 能力

### 7.1 业务闭环

| PRD 能力 | 实现位置 |
| --- | --- |
| 项目管理（创建/编辑/归档/删除/进度工作台） | `api/routes/projects.py`、`services/project_service.py` |
| 每项目最多 20 家企业、名称唯一 | `services/company_service.py` |
| 一企业多文件、文件替换版本化、软删除 | `services/file_service.py` |
| 人工拖动排序 → 逻辑标书版本快照 | `services/logical_document_service.py` |
| PDF / DOCX / TXT 解析、页码与文本块映射 | `services/parser.py`、`models/document.py` |
| 解析中心、失败/部分成功提示、重试 | `api/routes/files.py`、`GET /projects/{id}/parse-center` |
| 自动发起比对 + 启动校验 | `services/analysis_service.py#auto_start_if_ready/create_task` |
| N 家企业生成 N(N-1)/2 组组合 | `create_task` 中的 `combinations` |
| 文本相似 / 人员重复 / 价格异常 | `analysis/*.py` |
| 证据可回溯到文件、页码、文本块、偏移量 | `ComparisonEvidence.source_a/source_b` |
| 三栏原文联动（PDF iframe / 文本高亮） | `frontend/app.js#mountPairViewer` |
| 人工审核（四种状态、追加记录） | `services/results_service.py#apply_review`、`review_record` 表 |
| 命中招标文件公共内容标识 | 证据 `evidence_data.excluded_by_tender` |
| Excel 四表导出（项目级 / 单企业组合） | `services/results_service.py#export_excel`、`GET /pairs/{id}/reports/excel` |
| 分析快照、算法版本、结果历史 | `analysis_task`、`analysis_company_snapshot`、`analysis_pair_result` |
| 操作审计 | `audit_log` 表 |

### 7.2 系统级模块（PRD v2）

| PRD 能力 | 实现位置 |
| --- | --- |
| 工作台：全局统计 / 待办 / 最近项目任务 / 风险统计 | `api/routes/workbench.py`、`services/workbench_service.py` |
| 跨项目待办清单（规则唯一真源） | `services/project_service.py#collect_todos` |
| 风险管理：证据清单 / 筛选 / 分页 / 详情 | `api/routes/risks.py`、`services/risk_service.py` |
| 证据编号（PRD §5.7） | `services/risk_service.py#evidence_code` |
| 批量审核（仅允许「待进一步核查」） | `services/risk_service.py#batch_review` |
| 检测规则版本管理 / 生效确认 | `services/rule_service.py`、`api/routes/system.py` |
| 规则版本真正作用于检测引擎 | `analysis/thresholds.py` + 各分析函数的 `thresholds` 参数 |
| 操作日志查询（管理员） | `services/system_service.py#list_logs`、`api/routes/system.py` |
| 用户管理（新增/禁用/重置密码/分配角色） | `services/system_service.py`、`api/routes/system.py` |
| 管理员鉴权 | `api/deps.py#require_admin` |

### 7.3 关键业务规则

- **BR-01** 企业上限 20 家：服务层校验，前端同步禁用按钮。
- **BR-03/05** 原始文件独立保存；文本块保存 `file_id / page_index / block_id / start_offset / end_offset`，不复制原始文件。
- **BR-06** 解析失败、部分成功在解析中心和企业状态中均可见。
- **BR-07** 全部文件解析完成后由 `auto_start_if_ready` 自动发起比对：参与企业、三个分析维度全部默认执行，无需人工勾选；解析范围与上次一致时不重复发起。
- **BR-08** 企业组合数量由后端按组合生成，不依赖前端。
- **BR-09** 招标文件未上传时，`tender_file_version_id` 为空，跳过公共内容排除。
- **BR-10** 证据 `source_a/source_b` 定位原始文件页；PDF 无坐标时明确标记 `locator_precision=block`。
- **BR-11** 审核只追加 `review_record`，证据原始识别结果保留。
- **BR-12** 文件变化生成新版本/新逻辑标书版本，旧分析任务仍引用快照。
- **BR-13** Excel 包含「投标人比对概览、文本雷同汇总表、人员姓名重复表、价格异常分析表」。
- **BR-14** 价格证据记录结构化金额、大写金额、偏差率、原文位置与全部参与企业。
- **BR-15** 证据编号格式 `项目编号-任务编号-证据类型-序号`（PRD §5.7），序号在同一 (任务, 类型) 内跨历史行编号。
- **BR-16** 全局证据统计与风险清单一律带 `is_current` 过滤，且**不 JOIN 参与企业表**——项目级价格证据只计 1 条。
- **BR-17** 批量审核只允许「待进一步核查」；批量确认 / 批量排除一律 400 拒绝。
- **BR-18** 检测规则修改生成新版本，旧版本转 `archived` 保留；生效需要管理员权限 + 生效确认。
- **BR-19** 分析任务启动时固化规则版本与阈值快照，运行期从快照取阈值，规则后续变更不影响历史任务。

### 7.4 核心数据模型

按 PRD 建议将业务、原始文件、解析、分析、审核分开存储：

```
project
company
original_file               # 招标文件 company_id 为空；替换生成新版本
logical_document
logical_document_version
logical_document_item       # 只存文件顺序，不复制文件
document_page               # page_index 与 printed_page_number 分离
document_block              # 原文 + 规范化文本 + 偏移 + bbox(预留)
analysis_task               # 配置/企业/文件版本/算法版本/进度/日志
analysis_company_snapshot
comparison_pair             # A-B 唯一，存三维风险 + 综合风险 + 原因
analysis_pair_result        # 每组合每维度执行结果
text_similarity_result      # 文本块对 Jaccard / 有效相似度 / 基准占比
comparison_evidence         # 证据 + 可回溯 source_a/source_b
evidence_participant        # 支持 3 家以上企业的价格异常
review_record               # 只追加
audit_log                   # 结果/来源/变更前后写在 payload JSON
app_user
detection_rule_version      # 检测规则版本：按维度存参数快照，旧版本转 archived 保留
```

---

## 8. 主要 API

所有接口前缀 `/api/v1`，除登录外均需 `Authorization: Bearer <token>`。

| 模块 | 接口 |
| --- | --- |
| 认证 | `POST /auth/login`、`POST /auth/logout`、`GET /auth/me` |
| 工作台 | `GET /workbench` |
| 风险 | `GET /risks`、`GET /risks/filters`、`GET /risks/{id}`、`POST /risks/batch-review` |
| 系统 | `GET /system/rules`、`POST /system/rules/versions`、`POST /system/rules/versions/{id}/activate`、`GET /system/logs`、`GET /system/log-options`、`GET/POST /system/users`、`PATCH /system/users/{id}`、`POST /system/users/{id}/password` |
| 项目 | `GET/POST /projects`、`GET/PATCH/DELETE /projects/{id}`、`POST /projects/{id}/archive`、`GET /projects/{id}/dashboard` |
| 企业 | `GET/POST /projects/{id}/companies`、`GET/PATCH/DELETE /companies/{id}`、`POST /companies/{id}/confirm`、`PUT /companies/{id}/file-order`、`GET /companies/{id}/logical-document` |
| 文件 | `POST /projects/{id}/tender-files`、`POST /companies/{id}/files`、`GET /projects/{id}/files`、`GET/DELETE /files/{id}`、`POST /files/{id}/replace`、`POST /files/{id}/parse`、`POST /projects/{id}/parse`、`GET /projects/{id}/parse-center`、`GET /files/{id}/pages/{page}`、`GET /files/{id}/content` |
| 分析 | `GET /projects/{id}/analysis/prepare`、`POST /projects/{id}/analysis/tasks`、`GET /projects/{id}/tasks`、`GET /tasks/{id}`、`POST /tasks/{id}/cancel`、`POST /tasks/{id}/pairs/{pairId}/retry` |
| 结果 | `GET /projects/{id}/results/overview`、`GET /pairs/{id}`、`GET /evidences/{id}` |
| 审核 | `POST /evidences/{id}/review`、`GET /projects/{id}/reviews` |
| 报告 | `GET /projects/{id}/reports/excel`、`GET /pairs/{id}/reports/excel`（单组合证据导出） |

PDF 预览接口支持 `?token=` 查询参数（浏览器 iframe 无法设置 Authorization 头）；其他接口只接受 Bearer 头。

---

## 9. 分析框架与扩展点

### 9.1 文本相似

`analysis/text_similarity.py` 及配套模块：

1. `chunking.py` 按章节、段落和长度规则生成结构化文本块（目标 300～500 字，最大 800 字，短块同章节合并），标题只作为结构元数据；
2. `utils/text.py` 做文本规范化（全角/半角、空白、标点）并保留 `规范化下标 → 原文下标` 映射；
3. `shingling.py` 生成字符级 3-gram Shingle、128 位 MinHash 签名，并用自动推导 bands/rows 的 LSH 召回候选；Shingle 指纹使用稳定的 crc32，跨进程可复现；
4. 对候选块计算精确 Jaccard，同时在剔除招标文件指纹后计算 `effective_jaccard`；
5. `difflib.SequenceMatcher` 做局部原文对齐，按局部密度合并相邻匹配，避免零散单字命中被串成伪片段；
6. 招标文件公共内容按局部覆盖率判断（默认 ≥90%），保留证据但标记 `excluded_by_tender`，并保存 `excluded_ranges` 和 `baseline_source`；
7. 证据同时保存原文偏移、文本块、章节、页码、文件版本、算法版本和 Shingle/MinHash 参数；块对级 Jaccard 结果写入 `text_similarity_result`。

项目级 LSH 索引在一次分析任务内只构建一次，所有企业组合复用；候选对按 LSH 命中 band 数排序并去重。

替换模型时只需保持 `find_text_matches` / `build_text_evidences` 的输出结构（`EvidenceDraft`），风险计算与页面无需修改。

### 9.2 人员重复

`analysis/personnel.py`：岗位关键词 + 姓名候选（常见姓氏约束）抽取，跨企业同名/近名（`SequenceMatcher >= 0.85`）生成线索。结果明确标注“同名不代表同一人”。

### 9.3 价格异常

`analysis/price.py`：提取结构化总报价与大写金额，按项目级规则识别：

- 变异系数低于 5%；
- ≥3 家偏差率小于 1%；
- ≥3 家报价构成等差数列（允许 0.5% 误差）；
- 特殊尾数（888/999/998）与尾数集中。

价格证据通过 `evidence_participant` 记录全部参与企业，企业组合页面只展示与该组合相关的结果，不把项目级异常拆成单家企业结论。

### 9.4 风险等级

`analysis/risk.py`：确定性规则输出文本/人员/价格三个维度风险，再取综合风险，每个等级附带 `risk_reasons`。阈值集中在 `app/core/config.py`，等待业务确认后配置化。

### 9.5 OCR

`services/ocr.py` 定义 `OCRProvider` 接口，默认 `NoopOCRProvider`：扫描页会被标记为空白/需 OCR，文件状态为部分成功，不伪造识别文本。接入 PaddleOCR/Tesseract 时替换 `get_ocr_provider()` 即可。

### 9.6 存储

`storage/base.py` 定义存储接口，当前为本地卷 `/data/storage`。替换 MinIO 时实现同一接口即可，业务层只使用 `storage_key`。

---

## 10. 与 PRD 的差异 / 后续迭代

本版本有意保持“框架优先”，以下项已预留但未完全实现：

1. **数据库迁移**：当前用 `Base.metadata.create_all` 建表，正式迭代建议接入 Alembic。
2. **分片/断点续传**：上传按 1MB 流式落盘并限制 500MB，尚未实现分片上传协议。
3. **PDF 文本坐标与印刷页码**：解析器保存页面尺寸和 `bbox` 字段，但 pypdf 不提供字符级坐标；当前定位精度为文本块级，并在证据中明确标记。印刷页码（printed_page_number）字段已预留但尚未自动识别。生产环境建议接入 PyMuPDF/OCR 坐标输出。
4. **DOCX 页码**：DOCX 当前按单页组织提取文本，分页与印刷页码不精确；投标文件建议以 PDF 提交。
5. **OCR**：仅接口占位。
6. **任务恢复**：Celery 任务失败可重试，但容器重启时运行中任务不会自动恢复；当前由任务日志和“重跑任务”兜底。
7. **项目级数据权限**：已有用户/JWT/角色字段、`require_admin` 管理员鉴权和完整审计日志，但**尚未实现按项目的数据隔离**（PRD §7.2「不同用户只能访问授权项目」）。当前任何已登录用户可见全部项目，`/workbench` 与 `/risks` 返回全量数据。接入时需新增 `project_member` 表，并在这两个服务里追加项目过滤条件。多人审批流程亦未实现。
8. **大数据量性能**：企业/文件/文本块查询为基础实现；20 家企业 × 大文本时建议对文本块加倒排索引与分批任务。
9. **分项报价**：当前只提取投标总报价并做项目级规则分析，分项报价（工程量清单/报价明细）提取尚未实现。
10. **PDF 报告**：PRD 中列为可后置，本版本只实现 Excel 四工作表导出。
11. **批量分配审核人员**（PRD §4.11.5）：`comparison_evidence` 没有审核人指派列，加列属于表结构变更，本版本只实现「批量标记为待进一步核查」。
12. **规则参数覆盖度**：人员维度的关键岗位范围、价格维度的大小写金额一致性规则在规则版本里**只存储与展示**，尚未接入检测引擎（页面上有「V1.0 暂未接入检测引擎」标注）。文本相似度、最小匹配字数、招标公共内容排除阈值、价格三类阈值、以及各维度风险分级阈值均已真正生效。
13. **报告导出待办的初值**：工作台的「报告尚未导出」依赖新增的 `report.export` 审计记录，历史导出无该记录的项目会多显示一次此待办，导出一次后即消失。

---

## 11. 演示账号与重置

- 账号：`admin` / `admin123`（`.env` 可改）
- 演示项目在首次启动时自动生成，重复启动不会重复创建
- 彻底重置：`docker compose down -v && docker compose up --build -d`

> 系统输出的是疑似异常线索，不构成围标串标认定结论。所有证据均保留原始文件、页码与文本位置，人工审核结论只追加、不覆盖。
