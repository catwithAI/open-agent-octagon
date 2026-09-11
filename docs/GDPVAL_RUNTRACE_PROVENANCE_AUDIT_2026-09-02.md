# GDPval prepaid amortization runtrace provenance audit

日期：2026-09-02  
状态：已找到候选真实 runtrace；尚未将其直接纳入正式 capability pilot。

## 1. 结论

此前“没有找到 GDPval runtrace”的判断不准确。重新检查
`/home/yang/agent-octagon/data` 后，确认存在多条 Aurisic prepaid amortization
任务的历史执行记录。

这些记录的 runtrace 不是存放在任务环境内的 `amortization.db`，而是以：

```text
/home/yang/agent-octagon/data/attempts/<attempt_id>/trace.jsonl
```

形式保存；同一 attempt 还可能包含 `events.jsonl`、`blade_history.json`、
`skill_workspace/`、`private_eval/` 等辅助产物。

## 2. GDPval task provenance

环境：

```text
gdpval-prepaid-amortization-official
```

官方来源：

```text
dataset: openai/gdpval
source_task_id: 7d7fc9a7-21a7-4b83-906f-416dea5ad04f
dataset_revision: 11e7900cdcac61bc4daf59e65feb238acda98fbf
```

本地 task 文件：

```text
/home/yang/agent-octagon-envs/gdpval-prepaid-amortization-official/tasks/gdpval_prepaid_amortization_official.json
```

官方 rubric：

```text
/home/yang/agent-octagon-envs/gdpval-prepaid-amortization-official/private/official_rubric.json
```

该 official rubric 包含 56 条 criterion。

## 3. 已发现的候选 attempt

已发现以下 attempt 的 trace 或事件记录中包含 Aurisic prepaid amortization
任务内容：

```text
att_2a8d84473af9
att_c1661e565809
att_8e46de855ad4
att_1beed11e04fb
att_85b40de9a9fa
att_be9c86075125
att_869a93f9b7f9
att_ae49fe9c6347
att_26ef28cde592
att_8839a2aa1a0a
att_7573730c4d15
att_7fa9cfd58857
att_5299dd98d01c
att_5a47b56ba0f5
att_5f9dc34b001d
att_4daa55068088
att_5f7c0bf7eda1
att_a643df9a078c
att_11f468c43946
att_b651931de332
```

候选 attempt 不能仅凭文件名直接视为同一 frozen run；必须保留
`attempt_id` 并在正式分析中逐条标记 provenance confidence。

## 4. 当前优先候选

优先检查：

```text
/home/yang/agent-octagon/data/attempts/att_869a93f9b7f9/
```

该 attempt 包含：

```text
trace.jsonl
events.jsonl
skill_workspace/Aurisic_Prepaid_Expenses_*.pdf
skill_workspace/Aurisic_Prepaid_Insurance.pdf
skill_workspace/COA.xlsx
skill_workspace/Aurisic_Prepaid_Amortization_Through_Apr2025.xlsx
private_eval/gdpval_rubric_judge/
```

并且该 attempt 的输入文件与官方环境输入文件逐一匹配。已核对的 SHA-256 前 16 位如下：

| 文件 | 官方环境 | attempt skill workspace |
|---|---|---|
| `Aurisic_Prepaid_Expenses_Apr25.pdf` | `97bc326172d029a0` | `97bc326172d029a0` |
| `Aurisic_Prepaid_Expenses_Feb25.pdf` | `ca54cb87100f534e` | `ca54cb87100f534e` |
| `Aurisic_Prepaid_Expenses_Jan25.pdf` | `c00c4b44a0b5ba14` | `c00c4b44a0b5ba14` |
| `Aurisic_Prepaid_Expenses_Mar25.pdf` | `10a2082e4dc87fc3` | `10a2082e4dc87fc3` |
| `Aurisic_Prepaid_Insurance.pdf` | `da0efd444c9be29c` | `da0efd444c9be29c` |
| `COA.xlsx` | `7018fed4750a883d` | `7018fed4750a883d` |

这说明 `att_869a93f9b7f9` 至少在输入材料层面与
`gdpval_prepaid_amortization_official` 高度一致。

## 5. central octagon.db 检查结果

检查的数据库：

```text
/home/yang/agent-octagon/data/e2e-audio-luna-8101/octagon.db
```

其中存在 `tasks`、`runs`、`attempts` 等表，但没有检索到：

```text
gdpval
gdpval_prepaid_amortization_official
Aurisic
```

因此不能把该 central database 当成 GDPval attempt 的唯一索引。历史 GDPval
runtrace 目前主要通过 `data/attempts/` 文件树发现。

## 6. 正式 pilot 前仍需完成的校验

在 capability alignment 中使用该 trace 前，还需冻结并记录：

```text
task file SHA-256
official rubric SHA-256
trace.jsonl SHA-256
events.jsonl SHA-256
artifact SHA-256
attempt_id
env_session_id
provenance confidence
```

尤其需要确认：

1. trace 中的输入环境确实是该 official task；
2. trace 与生成的最终 artifact 属于同一次 attempt；
3. 不把其他 Aurisic 变体或仅完成 `amortization.db` 的 run 与完整 Excel run 混合；
4. `O`（trace-observed capabilities）只从冻结 trace/artifact 中建立，不能从 official rubric 反推；
5. `R_H` 只来自 official rubric，不能用 Judge score 反推。

## 7. 当前决定

目前可以继续 GDPval capability alignment pilot，但应以：

```text
att_869a93f9b7f9
```

作为第一候选 frozen run，先完成 provenance 冻结和 capability mapping，再决定是否
运行 A/C/D generated rubric 或 B Judge 下游实验。
