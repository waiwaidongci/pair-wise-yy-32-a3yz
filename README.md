# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。质量人员可以拒绝、再取样、有条件放行或正式放行；关键偏差始终阻止正式放行，修改必须携带当前批次修订号。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8214`。身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/batches/{id}/stability`：关联供应链和稳定性记录。
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查；放行或有条件放行时自动生成第一版质量摘要。
- `POST /api/batches/{id}/post-release`：已放行批次补录偏差、检验或稳定性数据，进入待评估。
- `POST /api/post-release/{id}/assess`：质量影响评估。合格数据作为放行后补充证据并生成新摘要版本；不合格数据或关键偏差（即使评估人认为可接受）使批次进入召回待审。
- `POST /api/batches/{id}/recall-review`：召回复核，`decision` 为 `recall`（确认召回）或 `clear`（解除待审并恢复原放行状态），结论写入摘要新版本。
- `GET /api/batches/{id}/summary`：查看质量摘要版本、补录事项、待审事项和处理结论；原放行决定始终保留在批次详情中供追溯。
- `GET /api/batches/{id}`、`GET /api/state`、`GET /api/health`：详情、状态（含待审队列）和健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差和例外有效期为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。
