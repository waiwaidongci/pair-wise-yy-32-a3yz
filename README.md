# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。质量人员可以拒绝、再取样、有条件放行或正式放行；关键偏差始终阻止正式放行，修改必须携带当前批次修订号。

批次放行时自动冻结一份质量摘要（偏差、检验、返工、供应商变更、稳定性和放行决定）。已放行批次可补录偏差、检验或稳定性数据：质量先做影响评估，合格即作为放行后补充证据并生成新摘要版本；不合格或触及关键偏差则批次进入召回待审，原放行记录与历史摘要版本全部保留。质量完成复核后批次确认召回或恢复放行，复核结论写入新摘要版本。

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
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查；放行时自动生成质量摘要 v1。
- `POST /api/batches/{id}/post-release`：为已放行批次补录偏差、检验或稳定性数据（进入待评估）。
- `POST /api/post-release/{id}/assess`：质量影响评估，合格作为补充证据并生成新摘要版本；不合格或关键偏差使批次进入召回待审。
- `POST /api/batches/{id}/recall-review`：召回待审复核，结论为确认召回或恢复放行，结论写入新摘要版本。
- `GET /api/batches/{id}/summaries`、`GET /api/post-release/pending`：摘要版本和待审事项。
- `GET /api/batches/{id}`、`GET /api/state`、`GET /api/health`：详情、状态和健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差和例外有效期为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。
