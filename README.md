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
- `POST /api/bids`、`POST /api/bids/confirm`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`、`GET /api/bids/{id}`
- `POST /api/tenders/open`：截止后开标；存在未确认或未重提的旧版本投标时拒绝，且不产生半场开标结果
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/publish`、`POST /api/clarifications/answer`
  - 每份发布澄清使用唯一 `clarification_no`，发布后生成不可修订的澄清版本；用相同编号重试会幂等补齐缺失的失效记录与审计。
  - 澄清版本变化后，旧密封投标进入待确认；`affected_vendor_ids` 列出的受影响供应商必须重提，其他供应商可调用确认接口。
  - 发布澄清与开标都基于项目版本做冲突检查，先提交者成功，后提交者仅得到版本冲突。
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、澄清版本失效/确认/幂等恢复、旧数据升级和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
