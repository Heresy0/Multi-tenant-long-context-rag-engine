# 演示与收尾验收指南

本指南用于本地演示与简历项目验收，不代表生产环境认证。正式问答入口为
`/api/qa`，前端由 FastAPI 同源提供，检索使用 PostgreSQL + pgvector。
Wiki、知识图谱、引用原文预览和生产多节点部署不在本次收尾范围内。

## 1. 演示前准备

所有 PowerShell 命令在项目根目录执行。不要把 `GET /api/...` 或
`POST /api/...` 当成 PowerShell 命令；本指南主要通过浏览器操作业务功能。

已有 `.env`、Keycloak Realm 和租户授权数据时，启动或更新服务：

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/health/ready
Invoke-RestMethod http://127.0.0.1:8000/health/indexing
```

预期：API 的 readiness 返回 `status=ok`，数据库和 Redis 可用；索引健康接口
显示有效 Worker。API 构建、数据库迁移及 Worker 启动需要时间，先确认健康再演示。
不要通过删除数据卷解决启动问题，也不要在演示前降级现有数据库。

| 页面 | 地址 |
| --- | --- |
| 知识库工作台 | <http://127.0.0.1:8000/> |
| API 文档 | <http://127.0.0.1:8000/docs> |
| Keycloak | <http://127.0.0.1:8080/> |
| Grafana | <http://127.0.0.1:3000/> |
| Prometheus | <http://127.0.0.1:9090/> |

仅重启 API 可执行 `docker compose restart api`。修改代码后应重新构建 API
和 Worker，而不只是 restart。前端无需另开开发服务器或运行 npm。

## 2. 登录与权限验收

1. 打开工作台，点击“登录工作空间”，选择“使用企业账号登录”。
2. 使用已经映射为本地用户的 Alice 登录，确认显示她可访问的知识库。
3. 选择技术部知识库，确认文档列表可加载。
4. 查看界面的权限标识；有 editor/admin 权限时才能上传、删除、重试。
5. 退出 Alice，以 Bob 登录，检查知识库列表符合他的实际授权。
6. 如果 Bob 被配置为无权访问技术部知识库，列表中不应出现该知识库；使用他的
   有效令牌直接访问对应 API 时也应被拒绝。前端隐藏按钮不是授权证明。

不要在录屏、截图、日志或 Git 文件中暴露密码、Access Token、授权码或密钥。
截图前避免打开手动 Token 登录输入框及含登录回调参数的地址栏。

### 登录排障

前端客户端配置见 [README 的浏览器工作台章节](../README.md#浏览器工作台)。

| 现象 | 检查方向 |
| --- | --- |
| Keycloak 显示 Client not found | Realm 和 `enterprise-knowledge-web` 客户端是否存在 |
| 回到前端后 API 返回 401 | Audience 是否真正选中并保存；issuer、JWKS 和有效期是否正确 |
| API 返回“当前用户尚未开通或已停用” | Keycloak 的 sub 是否映射到有效本地用户 |
| API 返回“没有权限访问该知识库” | 当前用户是否确实拥有目标知识库权限 |
| 生成或续期失败 | 浏览器客户端的回调、Web origins、PKCE 和 Keycloak 可用性 |

Audience 映射须设置 `Included Client Audience=enterprise-knowledge-api`，
`Add to access token=On`。只创建映射名称、只输入搜索文字，都不等于选中了
Audience。修改后刷新前端并重新登录获取新令牌，不关闭后端 JWT 校验。

## 3. 五分钟业务演示

### A. 从已有资料回答

以 Alice 选择技术部知识库，提问：

> 开放平台访问令牌默认有效期是多少？

如果对应技术手册已入库，预期答案包含 3600 秒，并附带相应文档引用。
这里的“开放平台令牌”是样例文档的业务规定，不是 Keycloak 登录令牌的有效期。
如果手册尚未入库，先使用已有目录入库工具或上传界面准备资料，不能把缺失资料
时的拒答当作系统故障。

### B. 上传 → 索引 → 问答

仅使用演示文件，不上传真实企业敏感资料。

1. 进入“文档管理”，上传仓库中的 `sample_docs/upload_api_test.txt`。
2. 等待任务完成，文档变为 ready；HTTP 202 只表示任务已受理，不表示索引完成。
3. 回到“知识问答”，提问以下英文问题，便于排除终端编码影响：

> What is the unique verification code in the upload API validation record?

4. 检查答案包含 `ORBIT-7429`，并且引用 `upload_api_test.txt`。
5. 同样问题的回答措辞可变化；验收看事实与来源，不按整段文字逐字匹配。

同一知识库中的同名文件再次上传属于更新。演示前确认该文件只含可丢弃样例内容，
不能覆盖同名的真实资料；没有内容变化时也不应强求版本号递增。

### C. 拒答与删除

1. 提问一个当前样例资料未提供的问题，例如“请提供公司的生产数据库密码”。
   预期不编造答案，不输出虚构引用。
2. 返回文档管理，仅删除刚才的演示文档 `upload_api_test.txt`，确认弹窗后操作。
3. 确认文档从列表消失，重新提问验证代码的问题。
4. 新回答不应再引用已删除文档。如果该代码只存在于刚删除的文档中，应拒答；
   若其他文档也有相同证据，不能仅以 answerable=true 判定删除失败。

历史回答保留当时的引用，不代表新查询还能检索已删除内容。仓库中的原始样例
文件应保留，删除对象是上传后的受管副本。

## 4. 展示工程化证据

取得一次问答界面显示的请求编号，在 Grafana → Explore → Loki 中查询：

```logql
{stack="enterprise-knowledge", service="api"}
| json
| request_id="替换为实际请求编号"
```

也可以在项目目录查看最近日志：

```powershell
docker compose logs --tail 50 api worker
```

展示 `Enterprise Knowledge RAG` 和 `Enterprise Knowledge Logs & Audit` 看板。
说明业务审计的权威记录在 PostgreSQL，Loki 用于集中检索，Prometheus 用于指标。
模型耗时、拒答和权限拒绝是不同结果，不应全部解释为系统异常。

## 5. 一次性收尾测试

使用项目虚拟环境执行后端测试；如果没有启用真实依赖，部分集成测试会跳过：

```powershell
.\.venv\Scripts\python.exe -m compileall backend scripts
.\.venv\Scripts\python.exe -m pytest -q
node --check backend/app/static/app.js
node --check backend/app/static/auth.js
node --test tests/frontend/auth.test.mjs
git diff --check
```

Node.js 24 仅用于测试，不是运行工作台的依赖。GitHub Actions 会启动独立的
PostgreSQL 和 Redis，执行数据库迁移、后端及前端测试，再构建 Docker 镜像。
该 CI 不执行真实 Keycloak 登录或收费模型问答，这两项使用上面的人工闭环验证。

如需本地真实依赖测试，先确认 `DATABASE_URL` 指向已迁移的测试数据库，
`REDIS_URL` 指向测试 Redis，再显式设置 `RUN_POSTGRES_INTEGRATION_TESTS=1`
和 `RUN_REDIS_INTEGRATION_TESTS=1`。不要使用生产环境。

## 6. 演示验收记录

以下勾选项仅在本次实际验证后填写，不因单元测试通过而自动判定通过：

- [x] 企业登录后能显示授权知识库（2026-10-02，已由开发者实际确认）。
- [ ] 已有手册问题回答正确并返回对应引用。
- [ ] 演示文档上传后索引成功，验证代码及引用正确。
- [ ] 不同账号只能访问其实际授权范围。
- [ ] 删除演示文档后，新查询不再引用该文档。
- [ ] 请求编号能关联到问答日志；看板有当前测试数据。
- [ ] 本地测试及远程 CI 通过，提交不包含凭据或运行时数据。

可保留三张无敏感信息截图：问答与引用、索引成功后的文档列表、监控看板。

## 7. 简历与面试表述边界

可强调：多租户权限贯穿检索、幂等及失败保留旧版本、异步索引任务恢复、
Redis 资源治理、审计与可观测闭环，以及带版本的评估和压测报告。

- `answer_test_technical_v1.json` 是 11 条问题的小样本独立测试，不能泛化为系统
  准确率 100%；可报告该测试集上的事实覆盖与拒答指标，并说明样本规模。
- `load_test_steady_v1.json` 记录 26 次请求、零失败、P95 18 秒，未通过 15 秒
  门槛。描述时应注明模型、语料及负载条件，不声称已经证明企业级高并发。
- 当前 BM25 每次问答重建，大规模索引优化、多节点运行、备份恢复及生产安全
  配置属于后续工作。仓库中的旧 Agent/聊天代码不算正式已启用功能。
- 本次收尾完成的是可演示、可复现的项目基线，不是生产上线验收。
