# WorkGuard 外部盲测协议

真正的盲测集不能提交到 WorkGuard 仓库，否则开发者可以读取案例或标签并针对性调参。本目录只保存协议和数据格式；明文数据由独立评测人保存在仓库外。

## 冻结流程

1. 独立评测人准备至少 30 个案例，优先使用匿名化真实会议纪要；开发者不得接触案例文本与标签。
2. 数据覆盖口语省略、跨段指代、多项目多日期、否定/反悔/转述、OCR 或 ASR 噪声、中英混合、无日期拒答等至少 8 类场景。
   应确认的日期尽量写明年份；相对日期仅用于测试安全拒答，避免评测年份漂移。
3. 评测人在代码冻结前只提供数据文件的 SHA-256、案例数和创建时间，不提供明文。
4. 冻结 WorkGuard 代码和 Prompt 后，在独立机器或干净 checkout 上执行一次评分。
5. 对外只分享 `eval/reports/blind_*.json`。报告不含案例 ID、原文、标签或逐例错误，防止结果重新进入调参循环。
6. 同一数据指纹与模式成功计分后会写入 `blind_receipts.json`，默认拒绝重复评分。最终答辩结束后，才由评测人决定是否解封数据。

## 数据格式

外部文件使用 JSON 数组或连续 JSON 对象，字段与 `eval/dataset/extraction_cases.jsonl` 一致：

```json
{
  "id": "由评测人分配",
  "group": "spoken_reference",
  "blocks": [
    {"location": "line_1", "text": "匿名化文本", "meta": {}}
  ],
  "expect": {
    "facts": [
      {"predicate": "release_date", "value": "2026-10-08", "unverified": false}
    ]
  }
}
```

允许的 MVP 谓词为 `release_date`、`regression_deadline`、`gray_release_date`、`announcement_date`。没有应抽取事实时，`facts` 必须是空数组。

## 一次性执行

评测人先在自己的环境计算哈希：

```bash
shasum -a 256 /secure/office_date_blind_v1.jsonl
```

然后在冻结的同一代码版本中分别执行三个模式；receipt 以“数据哈希 + 模式”为键，每个模式只允许成功计分一次：

```bash
WORKGUARD_LLM_PROVIDER=heuristic .venv/bin/python eval/blind_evaluator.py \
  --dataset /secure/office_date_blind_v1.jsonl \
  --sha256 <冻结的 SHA-256> --mode heuristic

set -a; source .env; set +a
.venv/bin/python eval/blind_evaluator.py \
  --dataset /secure/office_date_blind_v1.jsonl \
  --sha256 <冻结的 SHA-256> --mode llm

.venv/bin/python eval/blind_evaluator.py \
  --dataset /secure/office_date_blind_v1.jsonl \
  --sha256 <冻结的 SHA-256> --mode hybrid
```

若需要严格比较，三种模式应使用相同代码版本、阈值、Prompt、模型版本和数据哈希。连接失败必须计入首次结果，不能只重跑失败样本。
