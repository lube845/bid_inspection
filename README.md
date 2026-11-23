# 投标文件智能比对与风险分析系统（V1 框架）

依据 [prd.md](prd.md) / [prd_v2.md](prd_v2.md) 实现的 Python 全栈项目框架，跑通 PRD 定义的完整业务闭环：

> 创建项目 → 上传招标文件 → 创建投标企业 → 上传企业文件 → 人工排序 → 解析（解析完成后自动开始两两比对）→ 查看比对进展 → 企业两两比对 → 三栏查看证据 → 人工审核 → 导出报告。

同时提供 PRD v2 定义的**系统级模块**，一级菜单为
`工作台 · 项目管理 · 系统配置 · 审计日志 · 用户管理`，登录后默认进入工作台。

> PRD §4.11 的「风险管理」已并入项目管理：审核是项目内的工作（`?tab=review`），
> 不再设独立的跨项目页面，避免与项目内的「比对分析」出现两套审核入口。

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
| http://localhost:8888 | 管理台（开发环境默认 `admin / admin123`，生产部署请改 `.env`） |
| http://localhost:8888/docs | Swagger API 文档 |
| http://localhost:8001/health | 后端健康检查 |

首次启动会创建管理员账号。`SEED_DEMO` 未设置时默认 `true`（`docker-compose.yml` 里的默认值），会生成演示项目「演示采购项目A」：3 家企业、招标文件、已解析文本、3 组两两比对结果，可直接进入三栏证据页体验。仓库里的 `.env.example` 显式写了 `SEED_DEMO=false`，复制成 `.env` 后即不再生成演示数据——正式部署请保持 `false`。

> 生产部署前请修改 `.env` 中的 `SECRET_KEY`、`ADMIN_PASSWORD`、`POSTGRES_PASSWORD`，并确认 `SEED_DEMO=false`（`.env.example` 已是 `false`）。
>
> **公网安全要求**：系统本身只监听 HTTP。暴露到公网前必须在反向代理（Nginx / Caddy / 云 LB）上
> 终止 TLS 并提供 HTTPS，否则登录口令、投标文件与会话令牌都会以明文传输。安全加固完成前
> 建议仅允许内网或白名单 IP 访问。
>
> 仍使用部署配置里的初始管理员口令登录时，登录响应会带 `warn_default_password` 标记，
> 前端会提示立即改密；退出登录 / 重置密码 / 禁用账号都会吊销该账号已签发的全部令牌。

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
| 表结构 | Alembic 迁移 | Alembic 迁移 |

> 显式指定 `-f` 时 `docker-compose.override.yml` 不会被自动加载，因此生产模式不会带上任何源码挂载。

### 表结构迁移（Alembic）

表结构由 Alembic 管理，启动时自动 `upgrade head`：

- **全新库**：直接建表到最新版本。
- **`create_all` 时代的旧库**（没有 `alembic_version` 表）：启动时自动标记到 `0001_baseline` 再从基线升级，**数据不丢**。若结构差异过大，会打印缺失列并拒绝启动，确认可继续时设置 `LEGACY_STAMP_OK=1`。
- **测试**：`SCHEMA_MANAGEMENT=create_all`，直接按模型建表；迁移脚本与模型的一致性由 `tests/test_migrations.py` 单独校验。

```bash
make migrate                        # 升级到最新迁移
make revision m="新增 xxx 字段"      # 按模型差异生成迁移
make schema-check                   # 检查模型与迁移是否有差异
make stamp rev=0001_baseline        # 手工把库标记到某版本
```

### 开发期注意事项

- **Worker 重启会中断正在跑的任务**：解析/分析中途改了代码，Worker 会被重启，任务状态可能停留在 `running`。开发模式已开启 `ALLOW_RERUN_RUNNING_TASKS`，Worker 重启后会把停留在 running 的任务重新入队。
- **改模型要生成迁移**：改完 `models/*.py` 执行 `make revision m="..."`，否则 `make schema-check` 会失败。`AUTO_SYNC_SCHEMA` 现在只用于旧库基线 stamp 前的补偿，不再是日常改表手段。
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

新增覆盖（`pytest` 共 271 项）：迁移脚本与模型一致性（`test_migrations.py`）、报价导入与快照优先级（`test_price_quotes.py`）、尾数参数与大小写一致性（`test_price_rules.py`）、证据聚类边界（`test_clusters.py`）、分派/审核/按线索审核（`test_review_workflow.py`）、结构指纹与错字一致性（`test_structure.py`）、经验模板库（`test_template_library.py`）、跨任务对比（`test_task_diff.py`）、QA 报告（`prd_kimi_test.md`）缺陷回归（`test_qa_1117_regressions.py`）：退出登录令牌吊销、重复分析只看最新任务、千分位/表格合计行报价识别（含「分项在合计之前」的布局）、合计候选矛盾时标记待确认、统一社会信用代码校验位、封面字段雷同降级、关键岗位范围参数生效、取整尾数不报异常、招标正文变化后旧任务标记基准过期、报价清单展示自动识别金额；第三轮 QA 回归（`test_qa_test3_regressions.py`）；项目权限（`test_project_access.py`）、价格矩阵（`test_price_matrix.py`）、单家价格文件（`test_company_price_file.py`）、模板命中计数（`test_template_hits_regression.py`）。

---

## 4. 实时结果展示

分析任务的进度通过 SSE 推送，不再依赖固定间隔轮询：

```
POST /api/v1/projects/{project_id}/events/ticket   → { "ticket": "...", "expires_in": 300 }
GET  /api/v1/projects/{project_id}/events?ticket=<scoped_ticket>
```

`EventSource` 无法设置请求头，因此先签一张**限定到该项目、默认 5 分钟**的短期票据，再拼进 SSE 地址；票据里带 `token_version`，退出登录 / 改密后立即失效，长期 JWT 不会出现在 URL 里。票据只证明「是谁」，项目可见性仍按项目权限再校验一次。

后端每 1 秒比对一次快照（解析进度、任务状态、各维度完成数、证据数、待审核数），有变化才推送 `snapshot` 事件，空闲时每 15 秒发一次心跳。前端在任务详情页订阅该流，进度条、状态和计数就地刷新；SSE 不可用时自动退回 3 秒轮询。

---

## 5. 页面结构

系统级一级菜单（PRD v2 §3.1）：

| 一级菜单 | 路由 | 内容 |
| --- | --- | --- |
| 工作台 | `#/workbench` | 全局统计卡片（可点击跳转）、待办事项、最近项目、最近分析任务、风险统计（PRD §4.1） |
| 项目管理 | `#/projects` | 项目列表、创建/编辑/归档，进入项目后有 10 个子页（见下），含审核页与权限管理 |
| 系统配置 | `#/settings` | 检测规则版本与参数、OCR 识别配置（PRD §4.13.3） |
| 审计日志 | `#/audit` | 全量操作留痕与筛选（PRD §4.13.4） |
| 用户管理 | `#/users` | 账号（用户名/工号/邮箱）、角色、启用状态与密码重置（PRD §4.13.1） |

### 5.1 工作台（PRD §4.1）

- **全局统计卡片**：项目总数 / 进行中项目 / 已完成分析任务 / 异常证据总数 / 待审核证据，点击可跳到对应筛选后的列表。
- **待办事项**：解析失败、部分解析成功、尚未确认分析范围、分析任务失败、证据待审核、报告尚未导出；每条给出所属项目、处理入口与状态。
- **最近项目 / 最近分析任务**：含规则版本与算法版本。
- **风险统计**：按证据类型、风险等级、审核状态三个维度分布。

> 统计口径：所有证据计数都带 `is_current` 过滤且只数 `comparison_evidence` 行，**不 JOIN 参与企业表**——一条涉及 3 家企业的项目级价格证据只算 1 条（PRD §4.1.2 要求同一证据不得重复计数）。

### 5.1.1 项目权限（路由 `?tab=permissions`）

- **默认隔离**：用户创建的项目只有创建者自己能看到；其他人在项目列表里看不到，直接访问详情返回 404（与「项目不存在」一致，避免探测）。
- **系统管理员**：对所有项目拥有管理权限，无需逐条授权。
- **分享**：管理者在「权限管理」页点「分享项目」，输入对方的**工号或邮箱**定位账号，选择权限级别，弹窗二次确认后生效。
- **三种权限**（逐级包含）：
  | 级别 | 能力 |
  | --- | --- |
  | 查看 `viewer` | 查看项目全部内容（含审核列表），不能审核、不能改动 |
  | 审批 `reviewer` | 查看 + 审核/分派证据 |
  | 管理 `manager` | 审批 + 改项目、管企业/文件/报价、发起分析、继续分享 |
- **防越权**：不能授予高于自己的级别；创建者不可被移除或降级；批量审核/分派会逐条校验所属项目，任一越权即整体拒绝，伪造证据 id 直接 404。
- **演示数据**：内置演示项目归初始管理员所有；历史无归属的项目按「仅管理员可见」处理。

### 5.2 项目审核页（PRD §4.11，路由 `?tab=review`）

- 证据列表字段：证据编号、分析任务编号、证据类型、涉及企业、风险等级、发现时间、审核状态、审核人、分派给、操作。
- 筛选：企业 / 证据类型 / 风险等级 / 审核状态 / 审核人员 / 审核流程 / 分派给 / 发现时间区间 / 仅最新任务或含历史任务。
- 证据编号按 PRD §5.7 计算：`项目编号-任务编号-证据类型-序号`，例 `DEMO-2026-001-T03-TEXT-0001`。序号在同一 (任务, 类型) 内跨历史行一起编号，重跑后编号不漂移。
- 详情弹窗展示证据基本信息、相关文件与原始页码、涉及企业、分派人、历史审核记录，并直接给出审核表单；需要对照原文时点行内「三栏原文」。
- 与「比对分析」的分工：比对分析按**企业组合**组织，适合逐对深看原文；审核页按**证据**组织，一个页面看全项目待审条目，并支持分派、批量标记与导出。
- **批量操作仅允许「批量标记为待进一步核查」**；批量确认 / 批量排除按 PRD §4.11.5 明确禁用并给出提示。

### 5.3 系统配置 / 审计日志 / 用户管理（PRD §4.13）

- **系统配置**（`#/settings`）·**检测规则版本**：按文本相似 / 人员重复 / 价格异常 / OCR 识别四个维度管理。左侧切换维度，右侧按板块分组展示参数（每个参数附一句口径说明）与版本历史。每次修改**新建一个版本**（旧版本转 `archived` 保留），记录修改前/后内容、修改人、修改时间与修改原因；生效需要管理员权限 + 生效确认。
- **OCR 识别**：页面顶部提供「启用 OCR 识别」开关（改动直接生成并生效一个新版本），并展示调用方式（百度智能云 OCR）、接口文档链接、凭据配置状态、单次上传体积上限与「测试连接」按钮（校验能否换取 access_token）。API Key / Secret Key 属于敏感配置，列表页只显示「已配置 / 未配置」，编辑时留空表示不修改，真实值不返回浏览器。超过单次体积上限的 PDF 会在解析时自动拆分成多个文件逐份上传。
- **审计日志**（`#/audit`）：记录登录退出、项目增删改归档、文件上传下载替换删除、启动分析、重试任务、修改检测规则、审核证据、导出报告。操作结果 / 操作来源 / 变更前后信息写在 `audit_log.payload` JSON 中（不改表结构）。
- **用户管理**（`#/users`）：新增 / 编辑 / 禁用 / 重置密码 / 分配角色。系统至少保留一个启用的管理员账号，管理员不能禁用自己，也不能把最后一个管理员降级。
- **账号身份**：每个账号有用户名、工号、邮箱三项。**登录只认工号或邮箱**（邮箱不区分大小写）；用户名仅作展示，**允许重复**（同名同事）。工号与邮箱**至少填一项**、各自唯一且互不重复；用户管理页可以修改这三项，清空后仍需满足「至少一项」。
- **角色只有两种**：管理员（可进入系统配置 / 审计日志 / 用户管理）与用户（只做业务）。原先「项目分析人员 / 风险审核人员」的区分已取消，审核权限不再由角色决定，而是按证据逐一审核/分派。
- **自助修改密码**：侧边栏账户区的盾牌按钮可改自己的密码（需原密码）。改密后令牌版本号递增，旧会话立即失效。仍使用部署初始口令的账号登录后会被强制引导改密。
- **项目彻底删除**：删除项目默认是软删除（原始资料保留，便于恢复）；勾选「同时彻底删除全部资料」并输入项目名称确认后，磁盘文件、解析文本、分析任务、证据与审核记录、报价、项目级模板片段一并物理删除，审计日志保留删除动作本身。

### 5.3.1 第三轮 QA（`prd_kimi_test3.md`）修复说明

PDF 康熙部首/兼容汉字折叠（人员、报价、事实、逐项单价均在折叠文本上匹配并映射回原文偏移）、PDF 人员漏报、报价候选矛盾文案、页面详情按文本块分页、证据来源继承真实提取方式（OCR 页不再标 native）、导出时区统一、用户自助修改密码与初始口令强制改密、项目彻底删除（purge）及审计留痕、旧版 DOC 与二进制 WPS 上传即拒绝。

### 5.4 规则版本真正生效

检测阈值存放在 `detection_rule_version` 表中，**分析任务启动时把生效版本固化进 `analysis_config`**：

```json
{
  "thresholds":     { "text_similarity": 0.6, "price_cv": 0.05, "special_tail": ["888","999","998"], ... },
  "rule_versions":  { "text": { "version_no": 3, "params": {...} }, ... },
  "price_snapshot": { "captured_at": "...", "entries": { "<company_id>": { "amount": ..., "source_kind": "manual" } } }
}
```

运行期分析函数从任务快照读取阈值（`analysis/thresholds.py`），因此**规则被修改后，历史任务重跑仍使用启动时的那一版规则**。部署配置（`core/config.py`）只在首次初始化时作为出厂版本 v1 的种子。

`resolve_thresholds` 支持数值、布尔与列表三类参数：`special_tail` 这类列表值与 `price_amount_case_consistency` 布尔值此前被「只覆盖数值键」的旧逻辑静默丢弃，现在单独处理。

「OCR 识别」维度属于**解析期设置**，不进入分析任务快照（避免把解析参数与密钥写进任务配置）：文件解析时读取当前生效的 OCR 版本，决定是否启用 OCR 与调用方式。

### 5.4.1 价格比对矩阵

报价总览回答「每家报了多少」，价格比对回答「每一项谁和谁报得一样」。

- **数据来源与优先级**：企业价格 Excel（`import`）> 手工录入（`manual`）> 正文自动识别（`auto`）。上传过价格 Excel 的企业，矩阵完全以文件为准。
- **总价一览**：按总价排序的条形图，标出总价来源（价格 Excel / 手工录入 / 自动识别）。
- **逐项单价矩阵**：以企业为列、清单项为行。每个单元格按与同行价的关系着色：

  | 状态 | 含义 | 颜色 |
  | --- | --- | --- |
  | `identical` | 与其他至少一家单价完全相同 | 红 |
  | `near` | 与同行价偏差在容差内 | 橙 |
  | `outlier` | 与同项其他企业（≥2 家）的中位价偏差超过阈值 | 紫 |
  | `peer` | 有同行价但落在正常区间 | 白 |
  | `single` | 仅这一家报了这个分项 | 灰 |

- **悬浮提示**：同项其他企业的价格、本项中位价、来源文件与页码、是否 OCR 识别。
- **数据来源**：优先使用企业上传的价格 Excel 分项（`price_item` 表）；没有上传时回退到最新分析任务解析出的文本块（与「逐项单价雷同」证据同源）。
- **无分析任务也能看**：只上传了价格 Excel、还没跑分析的项目同样能生成矩阵。
- **阈值**：读取价格维度规则快照里的 `price_line_item_identical_tolerance` 与 `price_matrix_outlier_ratio`（默认 0.0001 / 0.3）。
  注意：这两个键目前只在 `analysis/thresholds.py` 里解析、**尚未登记进 `rule_service.PRICE_PARAMS`**，所以系统配置页改不到，只能用默认值——已列入待办，见 §10 第 15 条。

### 5.5 项目内子页面

进入项目后有 10 个子页面（Tab），其中「审核」按项目权限显示、「权限管理」仅管理者可见：

| 子页面 | 内容 |
| --- | --- |
| 项目概览 | 项目整体情况：企业/文件/解析进度、下一步操作推荐、最近分析任务 |
| 文件管理 | 招标文件（招标说明）与按公司管理的投标文件，支持排序、替换、重新解析；每家企业还可单独上传**价格 Excel**（总价 + 清单分项） |
| 解析中心 | 招标说明与投标文件的解析进展、失败原因、重试 |
| 分析任务 | 项目内企业两两比对的进展状态：任务列表、维度实时进度、执行日志；文件解析完成后自动开始，无需人工勾选范围 |
| 比对分析 | **选取任意两家企业**查看三栏对比：左侧企业A原文、中间企业B原文、右侧证据列表与详情 |
| 审核 | **按证据**列出本项目全部待审条目：筛选、逐条审核、分派、批量标记、导出；需看原文时跳转三栏 |
| 比对结果 | 全部企业组合的统计概览：各维度证据数、风险等级分布、企业组合矩阵、**与上次分析对比** |
| 价格比对 | **总价一览 + 逐项单价矩阵**：企业为列、清单项为行，单元格按「与其他企业相同 / 接近 / 偏离同项中位价 / 同行价 / 仅此一家」着色，悬浮显示同行价与原文位置 |
| 分析报告 | 导出整体统计报告（Excel，含文件取证与逐项单价表） |
| 权限管理 | 查看当前成员与权限，按工号/邮箱分享项目、调整级别、移除成员；仅管理者可见 |

### 报价清单与价格维度

> 现状说明：原先独立的「报价清单」页在菜单重构时被移除，报价录入现在走
> **文件管理 → 企业卡片 → 上传价格 Excel / 手工录入总价**。批量导入与模板下载的
> 后端接口（`/projects/{id}/quotes/import`、`/quotes/template`）仍然可用，但暂无界面入口，
> 见 §10 第 17 条。

价格维度不再「猜不到报价就当作没有异常」：

- **权威输入**：价格 Excel 与人工补录的报价优先于正文自动识别；自动识别只做兜底。
- **快照固化**：分析任务启动时把人工报价写进 `analysis_config.price_snapshot`；首次运行时把自动识别结果也补进快照。之后改报价不影响历史任务，任务详情会提示「报价已更新」。
- **覆盖情况**：结果页与任务详情显示「已识别 x / y 家」以及来源拆分（人工 / 正文识别），未识别时给出补录入口。
- **单家导入流程**（有界面）：企业卡片「上传价格 Excel」→ 解析预览（总价 / 分项条数 / 逐行错误与提示）→ 确认写入。支持 `.xlsx` / `.xlsm` / `.csv`，同时识别「清单 + 合计」与「只有总价」两种表；未写合计时按「数量 × 单价」推算并在提示里标明是推算值。
- **批量导入流程**（后端已实现、暂无界面）：下载模板 → 上传 → 校验预览（逐行错误/警告）→ 确认写入。按企业名称或统一社会信用代码匹配，大小写金额不一致会拒绝该行。
- **单家质量项**：大小写金额不一致记为 `amount_case_mismatch`，只作为报价文件质量提示，**不计入组合价格风险**。

### 证据线索（聚类）

同一段落的多处命中（同块对的多区间、跨页续接）在任务结束后聚成一条**线索**：

- 三栏页与风险清单可「按线索折叠」，只显示代表证据，展开看全部命中；
- 线索成员按严重度、命中字数与位置排序，代表证据取最严重的一条；
- 支持「整条线索确认 / 排除」，一次性覆盖全部成员，每条成员仍各留一条审核记录；
- `cluster_id` 由「企业组合 + 字典序最小的块对」派生，重跑后仍可复现。

### 审核分派与结论

- **分派**：风险清单支持勾选后分派给分析员/审核人，可按「分派给」筛选；可分派名单只需登录即可获取（不暴露账号敏感信息）。
- **单级审核**：一次审核即结论，没有二审。任何有「审批」权限的人都可以给出或改判结论，改判同样是追加一条审核记录，历史不覆盖。
- **待办口径**：工作台与项目待办按 `review_status` 统计（待审核），已给出结论的证据不再进入待办。

### 比对分析（三栏证据对比）

页面从「双文档全文对照」调整为「双侧证据对照」：左列与中列只展示命中片段及其前后必要上下文，完整原文按需打开。

- **企业选择器**：任选两家企业切换组合，默认定位综合风险最高的组合。
- **左列 / 中列（证据片段）**：每条证据一张卡片，展示命中片段、前后上下文和来源信息（投标人、文件、物理页码、章节）。
  - 绿色标记算法确认的相似文字，橙色标记双方不一致的文字，颜色只用于辅助阅读，不代表风险等级；
  - 「展开更多前文 / 展开更多后文」只按需拉取当前证据所需的文本范围，不重新加载整份文档；
  - 「查看完整原文」打开原文查看器，自动定位到证据所在页并高亮命中区间；
  - 两侧证据顺序一致，滚动一侧时另一侧跟随。
- **右侧证据列**：通过颜色和标识区分两类信息——
  - 左侧色条表示人工审核状态（待审核 / 确认异常线索 / 正常排除 / 暂不判断）；
  - 「命中招标说明 · 公共内容已排除」标签表示该段雷同内容命中招标文件公共内容（已从有效结论中排除，但保留原始识别记录）；
  - 「批次共同模板 · 不计入风险」标签表示该段雷同是多家企业共有的行业范本/通用套话（同样保留记录，但不抬高风险计数）。
- **右侧分析区**：相似指标、命中区间数、差异说明、来源定位与章节定位，以及人工审核操作。
- **筛选**：可按审核状态、风险等级、是否命中招标说明筛选，筛选只影响清单展示，不改变原始识别结果。
- **单独导出**：导出当前企业组合的证据 Excel（沿用五表结构，只含该组合的数据）。
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
├── prd_v2.md                      # 系统级模块（工作台/风险管理/系统管理）的产品需求；风险管理已并入项目
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
│   │   ├── services/evidence_context.py # 证据命中片段、上下文读取与相似/差异标记
│   │   ├── analysis/               # 文本相似、人员、价格、风险规则 + 阈值读取（thresholds.py）
│   │   ├── workers/                # Celery app、任务、同步/异步分发
│   │   ├── storage/                # 存储抽象 + 本地实现（可换 MinIO）
│   │   ├── utils/text.py           # 规范化 + 原文偏移映射
│   │   └── services/parser.py      # PDF/DOCX/TXT 解析适配
│   │   └── services/ocr.py         # OCR 提供方（V1：百度智能云 OCR）
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
| Excel 五表导出（项目级 / 单企业组合） | `services/results_service.py#export_excel`、`GET /pairs/{id}/reports/excel` |
| 分析快照、算法版本、结果历史 | `analysis_task`、`analysis_company_snapshot`、`analysis_pair_result` |
| 操作审计 | `audit_log` 表 |

### 7.2 系统级模块（PRD v2）

| PRD 能力 | 实现位置 |
| --- | --- |
| 工作台：全局统计 / 待办 / 最近项目任务 / 风险统计 | `api/routes/workbench.py`、`services/workbench_service.py` |
| 跨项目待办清单（规则唯一真源） | `services/project_service.py#collect_todos` |
| 项目审核页：证据清单 / 筛选 / 分页 / 详情 | `api/routes/risks.py`、`services/risk_service.py` |
| 证据编号（PRD §5.7） | `services/risk_service.py#evidence_code` |
| 批量审核（仅允许「待进一步核查」） | `services/risk_service.py#batch_review` |
| 系统配置：检测规则版本管理 / 生效确认 | `services/rule_service.py`、`api/routes/system.py` |
| 规则版本真正作用于检测引擎 | `analysis/thresholds.py` + 各分析函数的 `thresholds` 参数 |
| 审计日志查询（管理员） | `services/system_service.py#list_logs`、`api/routes/system.py` |
| 用户管理（身份字段/角色/禁用/重置密码） | `services/system_service.py`、`api/routes/system.py` |
| 项目级权限（可见性、分享、三种级别） | `services/access_service.py`、`services/project_share_service.py`、`api/routes/projects.py` |
| 项目成员表（project_member） | `models/project_member.py`、迁移 `0007_project_member.py` |
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
- **BR-20** 人工报价（导入/补录）优先于正文自动识别；两者都在任务启动/首次运行时固化进 `analysis_config.price_snapshot`，之后改报价不影响历史任务。
- **BR-21** 只有「正常 / 排除」的结论才能同时记为模板；模板片段限 30～200 字，超长雷同不得被模板吞掉。
- **BR-22** 审核是单级的：一次审核即结论，不要求第二位审核人复核。
- **BR-23** 证据聚类只在同一企业组合内进行，`cluster_id` 由块对派生，重跑可复现。
- **BR-24** 项目默认只对创建者与系统管理员可见；不可见的项目在列表里不出现、直连详情返回 404（与「不存在」一致，避免探测）。
- **BR-25** 项目分享只能授予不高于自己的级别；创建者不可被移除或降级；批量审核/分派逐条校验所属项目，任一越权整体拒绝。

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
price_quote                 # 人工确认的投标报价（版本化，旧版本保留）
price_item                  # 价格 Excel 导入的清单分项单价（按清单项规范化对齐）
template_fragment           # 经验模板库：审核人确认的行业范本片段
project_member              # 项目授权：user × project → viewer/reviewer/manager
```

`comparison_evidence` 新增列：`cluster_id/cluster_size/cluster_index/cluster_lead_id`（线索）、
`assignee_id/assigned_at`（分派）。
`analysis_task.template_snapshot` 固化任务使用的模板片段 id。
`app_user` 的身份字段为 `username`（可重复，仅展示）/ `employee_no`（唯一，可空）/ `email`（唯一、小写，可空），登录只认后两项。
`project` 记录 `created_by`（创建者即项目管理者），`project.deleted_at` 为软删除标记。
表结构由 Alembic 管理（`backend/alembic/`），当前版本链为
`0001_baseline` → `0002_features` → `0003_token_revocation` → `0004_credit_code_type` →
`0005_price_item` → `0006_user_identity` → `0007_project_member` → `0008_drop_second_review`。

---

## 8. 主要 API

所有接口前缀 `/api/v1`，除登录外均需 `Authorization: Bearer <token>`。

| 模块 | 接口 |
| --- | --- |
| 认证 | `POST /auth/login`、`POST /auth/logout`、`GET /auth/me`、`POST /auth/change-password`（自助改密，需原密码） |
| 工作台 | `GET /workbench` |
| 风险 | `GET /risks`（支持 `assignee_id` / `review_queue` / `cluster_lead_only`）、`GET /risks/filters`、`GET /risks/{id}`、`POST /risks/batch-review`、`POST /risks/batch-assign`、`POST /risks/cluster-review` |
| 报价 | `GET /projects/{id}/quotes`、`GET /projects/{id}/quotes/template`、`POST /projects/{id}/quotes/import?dry_run=`、`PUT /companies/{id}/quote`、`DELETE /companies/{id}/quote` |
| 价格矩阵 | `GET /projects/{id}/price-matrix`（总价一览 + 逐项单价矩阵，无分析任务时只用价格 Excel） |
| 价格文件 | `POST /companies/{id}/price-file?dry_run=`（单家企业价格 Excel：总价 + 清单分项） |
| 系统 | `GET /system/rules`、`POST /system/rules/versions`、`POST /system/rules/versions/{id}/activate`、`POST /system/ocr/test`（校验百度 OCR 凭据）、`GET /system/logs`、`GET /system/log-options`、`GET/POST /system/users`、`PATCH /system/users/{id}`、`POST /system/users/{id}/password` |
| 项目 | `GET/POST /projects`、`GET/PATCH/DELETE /projects/{id}`（`?purge=true` 物理删除）、`POST /projects/{id}/archive`、`POST /projects/{id}/unarchive`、`GET /projects/{id}/dashboard` |
| 项目权限 | `GET /projects/{id}/permissions`、`POST /projects/{id}/permissions`（按工号/邮箱分享）、`PATCH /projects/{id}/permissions/{member_id}`、`DELETE /projects/{id}/permissions/{member_id}` |
| 企业 | `GET/POST /projects/{id}/companies`、`GET/PATCH/DELETE /companies/{id}`、`POST /companies/{id}/confirm`、`PUT /companies/{id}/file-order`、`GET /companies/{id}/logical-document` |
| 文件 | `POST /projects/{id}/tender-files`、`POST /companies/{id}/files`、`GET /projects/{id}/files`、`GET/DELETE /files/{id}`、`POST /files/{id}/replace`、`POST /files/{id}/parse`、`POST /projects/{id}/parse`、`GET /projects/{id}/parse-center`、`GET /files/{id}/pages`、`GET /files/{id}/pages/{page}?block_offset=&block_limit=`（文本块分页）、`POST /files/{id}/ticket`、`GET /files/{id}/original`、`GET /files/{id}/content`、`GET /files/{id}/download` |
| 实时 | `POST /projects/{id}/events/ticket`、`GET /projects/{id}/events?ticket=`（SSE，见 §4） |
| 分析 | `GET /projects/{id}/analysis/prepare`、`POST /projects/{id}/analysis/tasks`、`GET /projects/{id}/tasks`、`GET /tasks/{id}`、`POST /tasks/{id}/cancel`、`POST /tasks/{id}/pairs/{pairId}/retry` |
| 结果 | `GET /projects/{id}/results/overview`、`GET /pairs/{id}`、`GET /pairs/{id}/evidences`（证据 + 默认上下文，分页）、`GET /evidences/{id}`、`GET /evidences/{id}/context`（按需扩展上下文） |
| 审核 | `POST /evidences/{id}/review`（支持 `learn_as_template`）、`GET /projects/{id}/reviews` |
| 对比 | `GET /projects/{id}/tasks/{task_id}/diff?against=` |
| 报告 | `GET /projects/{id}/reports/excel`、`GET /pairs/{id}/reports/excel`（单组合证据导出） |

原文预览、下载与 SSE 这三个浏览器无法设置请求头的入口，使用**限定到单个文件 / 单个项目、默认 5 分钟有效**的短期票据（`POST /files/{id}/ticket`、`POST /projects/{id}/events/ticket`，参数名 `ticket`）；其他接口只接受 `Authorization: Bearer`。长期登录 JWT 不出现在 URL 中。

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

「价格比对」页的逐项单价矩阵由 `services/price_matrix_service.py` 生成：分项来源优先级为价格 Excel（`price_item`）> 正文自动识别，单元格状态 `identical / near / outlier / peer / single` 与证据识别同源，因此矩阵里的「一致」与证据里的「雷同」不会出现两套结论。

### 9.4 入口归一与噪声抑制

文本相似之前有三层处理，目标都是「让文字层与渲染结果可比，同时不把模板当成串通」：

- **入口归一（`utils/text.py`）**：丢弃零宽字符（U+200B / U+200C / U+200D / U+2060 / U+FEFF）、
  软连字符与组合用标记，并把西里尔/希腊同形字母映射回拉丁字母。往「投标报价」里插 1～3 个
  不可见字符，肉眼与打印毫无差别，但足以让字面相似度掉到接近 0；这一步专门堵这个口子。
  规范化版本随之为 `norm-v4`，历史证据上记录了各自使用的版本号。
- **跨章节类型命中（`text_cross_type_guard_chars`）**：技术标与商务标之间的零散命中多半是
  通用表述，不足 120 字不成立证据；但整段搬家仍保留——那反而说明两家连章节归属都抄错了。
- **批次共同模板（`analysis/template.py`）**：同一文本块在 N 家企业（默认 3 家）标书里都出现、
  且信息量（数字/型号/英文密度）低于 12%、且命中长度不足 200 字时，判为共同模板。这类证据
  **不删除**，只打上 `excluded_by_template` 标记并排除出风险计数，审核人能在清单里看到
  「为什么这条不算数」。几百字逐字雷同不受此限——那本身就是最直接的证据。

### 9.5 文件级取证（与文本正交）

改抬头、换措辞、重新排版都洗不掉这两类线索，因此它们独立于三个分析维度，文件解析完就产出：

- **文件指纹（`file`）**：两家企业的原始文件 SHA-256 相同 → 同一份文件被当作各自的标书提交。
- **文档元数据（`metadata`）**：PDF 的 `Producer`/`Creator`/`Author`/文档 ID、DOCX 的作者与
  最后修改人相同。字段按可靠性加权（文档 ID、作者权重高，生成工具权重低），只有权重合计
  ≥2 才成立证据——否则同用一款办公软件的用户会全部互相命中。

- **相同图片（`shared_image`）**：`analysis/image_hash.py` 对 PDF 内嵌图与 DOCX `word/media`
  计算 dHash + aHash。同一枚印章、同一张证书扫描件出现在两家标书里，比文字雷同更难解释。
  纯色/空白页会被低信息量保护跳过（它们的感知哈希彼此相同，没有鉴别力）；判定要求两把
  哈希同时通过——渐变、条纹图的 dHash 会退化成全 0，只看 dHash 会互相撞车。
- **修订标识同源（`shared_rsid`）**：`analysis/rsid.py` 读取 DOCX 的 `w:rsid*`。这些 ID 由
  Word 按编辑会话随机生成，只有从同一份底稿另存派生才会成批保留。比对时先扣除
  **批次共性 ID**（`_batch_common_rsid`）——程序化生成的 DOCX 会给所有文件写入同一套固定
  rsid，不扣除会把每对企业都误判同源。副作用是这类文件不会产生 rsid 证据，属有意的保守行为。

取证证据按 `high`/`medium` 计入组合的「取证风险」，并参与综合风险。元数据、图片指纹与
rsid 均在解析时一次性抽取（`services/document_metadata.py`、`services/document_forensics.py`）
并固化进任务快照，历史结果可复现；存量文件在创建分析任务时按需回填。

### 9.6 报价与关键事实

- **DOCX 表格（`services/parser.py`）**：按 body 顺序遍历段落与表格，表格转成「一行一条、
  单元格制表符分隔」的文本。此前只读 `document.paragraphs`，整张报价表被丢弃。
- **逐项单价雷同（`analysis/line_items.py`）**：从行式清单抽取「项目名称 + 单价」，以项目
  名称跨企业对齐。共同分项数、完全相同条数、相同比例三条门槛同时满足才报——只比比例会被
  两三行的小表刷成 100%。作为 `price/line_item_duplicate` 证据，计入价格维度。
- **关键事实冲突（`analysis/facts.py`）**：共用手机号/邮箱、非常规工期一致（整月整年属
  规定口径，不报）、项目班子整组一致（≥3 个岗位人员完全相同）。

### 9.7 风险等级

`analysis/risk.py`：确定性规则输出文本/人员/价格三个维度风险，加上文件级取证风险，再取综合风险，每个等级附带 `risk_reasons`。阈值集中在 `app/core/config.py`，等待业务确认后配置化。

### 9.8 OCR

`services/ocr.py` 定义 `OCRProvider` 接口，V1 接入**百度智能云 OCR**（通用文字识别 · 标准含位置版）：

- **识别接口**：`POST https://aip.baidubce.com/rest/2.0/ocr/v1/general`（`application/x-www-form-urlencoded`）
- **鉴权接口**：`GET https://aip.baidubce.com/oauth/2.0/token?grant_type=client_credentials&client_id=<API Key>&client_secret=<Secret Key>`，token 按有效期缓存
- **接口文档**：<https://cloud.baidu.com/doc/OCR/s/vk3h7y58v>

调用方式：

- 扫描版 PDF 用 `pdf_file`（base64）+ `pdf_file_num` 指定页码，与解析器「按页取文本」的接口一一对应；
- 图片文件用 `image`（base64）；
- 识别失败只记日志并把该页标记为未识别，不伪造识别文本。

**单次体积上限与自动拆分**：百度接口限制的是 base64 + urlencode 之后的请求体（8MB），换算成原始文件约 6MB。系统配置里的「单次 OCR 文件大小上限（MB）」就是按原始文件计的上限（1～6MB，默认 6）。识别时的处理顺序：

1. 把需要识别的页（只有没有文本层的扫描页）用 pypdf 拆成**单页 PDF 文件**，每个文件都在上限内；
2. 逐份上传，用 `pdf_file_num` 指定该文件里的页码（拆分后固定为 1）；
3. 单页仍超限（扫描图特别大）→ 取出该页内嵌的扫描图片，用 Pillow 先降质量、再等比缩小压到上限内，以 `image` 逐张上传，识别文本按顺序拼接。

按页拆分而不是整份上传，是因为接口一次只识别一页：整份上传会让 N 页传输 N 份完整文件，拆开后总传输量约等于原文件大小。原生文本页不参与拆分，因此不会浪费额度。

开关与凭据在**系统配置 → OCR 识别**里配置（走规则版本管理，改动有版本记录）；未启用时扫描页标记为空白/需 OCR，文件状态为部分成功。接入其他服务只需再实现一个 `OCRProvider` 子类并在 `build_ocr_provider()` 里注册。

### 9.9 对话式取证补充

这两类信号与文本相似度正交，都输出普通证据，合并进 `forensic_risk`：

- **结构指纹（`analysis/structure.py`）**：抽取章节标题序列、表格骨架（列数 + 表头集合 + 行数）、图片顺序。标题序列用 LCS 比较，表格按「列数与表头一致、行数差 ≤20%」配对，图片要求连续子序列 ≥3。
  - **最大误报来源是招标文件规定的章节结构**，比对前先建「招标标题基准」并扣除；真实数据验证过：只靠标题一致时，每对企业都会命中。
  - 因此**仅标题一致不成立证据**，必须至少命中表格骨架或图片顺序。
  - 阈值 `structure_min_titles` / `structure_title_ratio` / `structure_table_min` 已进规则版本。
- **错字 / 非规范写法一致（`analysis/anomalies.py`）**：词表驱动（`DEFAULT_TYPO_TERMS`，常见错别字 → 正确写法），扫出命中后取前后各 12 字的规范化上下文，两家同一错误写法且上下文一致（总分 ≥8 字）才成立证据。
  - 短命中（<40 字）本就不成立文本证据，这正是该维度要覆盖的盲区。
  - 同一错误写法在本批次 ≥3 家企业出现时视为行业通病，只记录不抬风险。

### 9.10 经验模板库

审核「排除」时勾选「同时记为模板」，系统会学习**命中片段**（不是整块）：

- 只存 30～200 字的片段；过长（整段雷同是铁证）与过短（不足以区分）都会被拒绝；
- 片段按 `scope` 区分：`project` 只对本项目生效，`global` 需管理员；
- 匹配用「Shingle 倒排 + 包含度 ≥0.8」，双侧都要命中；
- 片段 id 在任务启动时固化进 `analysis_task.template_snapshot`，撤销后旧任务不受影响（新任务不再命中）；
- 命中统计在任务结束时批量累加，不逐条写。

这条闭环让误报率随使用下降，也是后续算法评测工作台的数据来源。

### 9.11 跨任务结果对比

`GET /projects/{id}/tasks/{task_id}/diff` 比较同一项目的两个任务，输出四块：

- **输入变化**：文件版本/哈希、规则版本差异；
- **证据变化**：按稳定内容键（企业组合 + 块对 / 姓名 / 文件哈希 / 错字词）匹配，得到新增 / 减少 / 风险等级变化；
- **风险变化**：逐组合的综合风险迁移；
- **上次已排除、本次重新出现**：审计最关心的一类（文件替换后文本块重切会让行 id 变化，因此用摘要前缀匹配兜底）。

只比较风险等级，不把「新任务里证据一律回到待审核」当成变化，否则每次对比都会刷屏。

### 9.12 存储

`storage/base.py` 定义存储接口，当前为本地卷 `/data/storage`。替换 MinIO 时实现同一接口即可，业务层只使用 `storage_key`。

---

## 10. 与 PRD 的差异 / 后续迭代

本版本有意保持“框架优先”，以下项已预留但未完全实现：

1. ~~数据库迁移~~：已接入 Alembic（`backend/alembic/`），旧库自动 stamp 到基线。
2. **分片/断点续传**：上传按 1MB 流式落盘并限制 500MB，尚未实现分片上传协议。
3. **PDF 文本坐标与印刷页码**：解析器保存页面尺寸和 `bbox` 字段，但 pypdf 不提供字符级坐标；当前定位精度为文本块级，并在证据中明确标记。印刷页码（printed_page_number）字段已预留但尚未自动识别。生产环境建议接入 PyMuPDF/OCR 坐标输出。
4. **DOCX 页码**：DOCX 当前按单页组织提取文本，分页与印刷页码不精确；投标文件建议以 PDF 提交。
5. ~~**OCR**~~：已接入百度智能云 OCR 并跑通（含按页拆分与体积压缩），见 §9.8。仍缺的是：只处理「无文本层的扫描页」，图片型表格的结构还原（行列关系）依赖 OCR 返回的位置信息，尚未做表格重建。
6. **任务恢复**：Celery 任务失败可重试，但容器重启时运行中任务不会自动恢复；当前由任务日志和“重跑任务”兜底。
7. ~~**项目级数据权限**~~：已实现，见 §5.1.1「项目权限」。项目默认只有创建者与系统管理员可见，可按工号/邮箱分享为查看 / 审批 / 管理三级；`/workbench` 与 `/risks` 也只统计当前用户可见的项目。
8. **大数据量性能**：企业/文件/文本块查询为基础实现；20 家企业 × 大文本时建议对文本块加倒排索引与分批任务。
9. **分项报价**：已支持人工导入 / 补录投标总报价，并有逐项单价雷同识别；**工程量清单的结构化导入**（多行分项 + 合计校验）尚未实现。
10. **PDF 报告**：PRD 中列为可后置，本版本只实现 Excel 导出（含逐项单价对比表）。
11. ~~批量分配审核人员~~：已实现证据级分派（`POST /risks/batch-assign`）与按指派人筛选。
12. **规则参数覆盖度**：~~大小写金额一致性~~ 已接入（`amount_case_mismatch`，质量项）；`special_tail` / `tail_concentration_min_count` / `price_line_item_min_*` 均已真正生效；人员维度的关键岗位范围（`key_positions`）自 2026-11-17 起也已接入检测引擎（非空时只比对命中岗位的人员）。
13. **报告导出待办的初值**：工作台的「报告尚未导出」依赖新增的 `report.export` 审计记录，历史导出无该记录的项目会多显示一次此待办，导出一次后即消失。
14. **语义相似（embedding）**：当前仍是字面 + 结构 + 事实三层；同义改写只能靠结构指纹与错字一致部分覆盖。接入向量召回时必须保留原文偏移回填，否则三栏高亮与报告定位会失效。
15. **价格矩阵两项阈值未进规则注册表**：`price_line_item_identical_tolerance` / `price_matrix_outlier_ratio` 只能吃默认值。
16. **合计金额未与分项之和校验**：目前只做「合计行优先 + 候选矛盾标记待确认」，表格错位时仍可能采用错误总价（QA 第二轮实测 1,450,000 被取成 145,000）。
17. **批量报价导入没有界面**：`/projects/{id}/quotes/import` 与模板下载接口仍在，但前端入口（原「报价清单」页）已随菜单重构移除；现在只能逐家企业上传价格 Excel。
18. **大文件性能**：页详情已支持文本块分页与文本截断，但实现仍先把整页文本块全部读进内存再切片，单页上万块时首屏依然偏慢。

---

## 11. 演示账号与重置

- 账号：`admin` / `admin123`（`.env` 可改）
- 演示项目在首次启动时自动生成，重复启动不会重复创建
- 彻底重置：`docker compose down -v && docker compose up --build -d`

> 系统输出的是疑似异常线索，不构成围标串标认定结论。所有证据均保留原始文件、页码与文本位置，人工审核结论只追加、不覆盖。
