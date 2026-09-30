# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`、`POST /api/clarifications/publish`
- `POST /api/bids/confirm`：澄清版本变更后确认旧投标继续有效，或按新要求重提
- `GET /api/bids/{id}`：供应商只能查看自己的密封投标
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

## 澄清版本与可恢复开标

澄清发布采用“先登记、后整套生效”的恢复流程：同一 `clarification_no` 的问题与答复生成不可修订版本；重试相同编号和内容时只补齐未完成步骤，不会产生重复版本或重复审计。澄清版本生效后，旧版本密封投标进入待确认状态；参数实际受影响的投标必须通过 `/api/bids/confirm` 重提，未受影响的投标确认后才恢复有效。仍存在待确认或待重提投标时不能开标或评分。

澄清发布与开标确认都以项目版本做乐观并发控制，并由数据库写锁串行化；后提交的一方只得到 409 版本冲突，开标结果不会半程泄露。旧数据库启动时自动补记投标的澄清版本为初始版本 1，保留 `submitted_at`，并在 `original_submitted_at` 中保留原始提交时间。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
