# 多租户 QA 性能压测

Locust 场景直接调用受 OIDC、知识库权限、租户限流和并发限制保护的
`POST /api/qa`，不会绕过正式业务链路。访问令牌只从环境变量读取，原始 CSV
写入已被 Git 忽略的 `data/load-tests`。

## 前置条件

1. 使用 `docker compose up -d --build` 启动完整环境。
2. 确认 `/health/ready` 返回 `status=ok`。
3. 获取 Alice 的短期 JWT，确保目标知识库已经完成文档索引。
4. 在当前 PowerShell 窗口设置配置：

```powershell
$env:ENTERPRISE_KB_ACCESS_TOKEN = $token
$env:LOAD_TEST_KNOWLEDGE_BASE_ID = `
  "8e4af664-22c1-480e-9393-36ac401da0f5"
$env:LOAD_TEST_CATEGORIES = "technical"
```

JWT 不应写入 `.env`、命令参数或报告文件。一次测试时间应短于令牌剩余有效期。

## 稳定负载基线

该模式下只有合法的 200 响应算成功，429 和其他非 200 状态都算失败：

```powershell
$env:LOAD_TEST_MODE = "steady"

python -m locust `
  -f tests/load/locustfile.py `
  --headless `
  -u 2 `
  -r 1 `
  -t 3m `
  --csv data/load-tests/qa_steady `
  --csv-full-history `
  --html data/load-tests/qa_steady.html
```

汇总并执行基线门禁：

```powershell
python scripts/summarize_load_test.py `
  --stats data/load-tests/qa_steady_stats.csv `
  --failures data/load-tests/qa_steady_failures.csv `
  --output evals/reports/load_test_steady_v1.json `
  --scenario steady `
  --min-requests 20 `
  --min-successful-requests 20 `
  --max-failure-rate 0.01 `
  --max-p95-ms 15000 `
  --max-server-errors 0
```

汇总命令在门槛未通过时返回非零退出码，因此可直接接入 CI 或发布门禁。

## 资源治理压力验证

该模式刻意制造超过租户并发上限的请求。200 和 429 都是预期业务结果，任何
5xx、非法响应结构或连接失败仍然记为失败：

Locust 会根据响应头把治理拒绝分别记录为
`/api/qa [429-concurrency]` 和 `/api/qa [429-rate]`，方便判断实际触发的是
并发槽位还是滑动窗口限流。

```powershell
$env:LOAD_TEST_MODE = "governance"
$env:LOAD_TEST_WAIT_MIN_SECONDS = "0.1"
$env:LOAD_TEST_WAIT_MAX_SECONDS = "0.2"

python -m locust `
  -f tests/load/locustfile.py `
  --headless `
  -u 8 `
  -r 8 `
  -t 45s `
  --csv data/load-tests/qa_governance `
  --html data/load-tests/qa_governance.html

python scripts/summarize_load_test.py `
  --stats data/load-tests/qa_governance_stats.csv `
  --failures data/load-tests/qa_governance_failures.csv `
  --output evals/reports/load_test_governance_v1.json `
  --scenario governance `
  --min-requests 8 `
  --min-successful-requests 1 `
  --max-failure-rate 0 `
  --max-p95-ms 15000 `
  --max-server-errors 0 `
  --min-throttled-requests 1
```

测试期间可同时查看 Grafana 的 `Enterprise Knowledge RAG` 看板，或直接查询
Prometheus 的请求耗时、QA 结果、限流和并发治理指标。测试结束后清理令牌：

```powershell
Remove-Item Env:ENTERPRISE_KB_ACCESS_TOKEN
Remove-Item Env:LOAD_TEST_KNOWLEDGE_BASE_ID
Remove-Item Env:LOAD_TEST_MODE
Remove-Item Env:LOAD_TEST_CATEGORIES
```
