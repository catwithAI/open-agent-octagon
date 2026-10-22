# Skill Eval · 可逐步实施与验收的开发任务

版本：v1.0 · 2026-10-22 · 状态：首期实施与验收完成

依据：[design.md](design.md) v0.5、[requirement.md](requirement.md) v1.2、[Pencil 调整清单](ui-change-list.md)。本文保留逐项开发操作与验收规格，完成状态及实施收敛以 [acceptance.md](acceptance.md) 的真实证据为准；测试场景不是对全部生产故障的穷举承诺。

## 1. 执行约定

- 按任务编号推进，满足列出的依赖后再开始。每项完成实现、对应测试和验收证据后才勾选；涉及真实能力或 catalog 确认的子项未完成时整项保持未完成；一个任务可以分多次提交，不将多个阶段的工作塞进一次不可验证的大改动。
- 每项采用“补充失败用例 → 实现最小闭环 → 指定测试通过 → 检查影响范围”的顺序。测试覆盖用户可观察行为、事务和故障边界，不为简单文案写镜像实现的测试。
- 文中新增模块和测试路径均为计划产物，执行对应任务时创建；在创建前运行命令报缺文件，不算验收。已有 Blade 升级不重复实施，但需记录基线。
- 本地测试不得连接真实 Blade 或使用真实用户文件。SDK 边界用 respx／可控 Socket 替身，运行服务用 FakeBladeRuntime；仅 T04、T46 的真实联调显式访问目标服务。
- `U` 表示纯逻辑单测，`I` 表示临时 DB／文件系统／HTTP／事件流集成测试，`C` 表示前端组件测试，`L` 表示真实服务验收。每项标明层级，不能以 L 跳过 U/I，也不能以 U/I 宣称 L 完成。
- 版本保存与试跑严格分离；每次运行固定 skill 快照、支持多轮交互，主动结束才固定最终结果。这些是测试断言，不再作为待讨论方案。
- 未决协议／catalog／限额的处理见第 3 节，不擅自改变已确认需求来规避实现问题。

测试均从仓库根目录执行。后端统一使用当前 worktree 的 uv 环境；前端命令使用 `npm --prefix web`，避免工作目录歧义。

```sh
uv sync --locked --extra dev --extra blade
uv run pytest -q tests/test_blade_sdk_compat.py tests/test_blade_cli_adapter.py tests/test_blade_priority_recovery.py
npm --prefix web ci
npm --prefix web run build
```

任务完成时在其下记录：代码提交／变更范围、准确命令与结果、fixture 来源、未覆盖项；真实服务验收另记录服务／SDK／CLI 版本、时间及无敏感信息的 run/session 关联证据。无凭据或依赖条件缺失时保持未勾选，注明阻塞与可继续任务。

## 2. 阶段与完成门槛

| 阶段 | 任务 | 阶段可验证产出 |
| --- | --- | --- |
| A 基线与协议 | T01–T04 | 基线、测试替身、SDK 契约及真实能力报告；真实能力未验证时不开放执行 |
| B 数据与包 | T05–T16 | 导入、多文件编辑、未发布保存、正式发布、临时快照、导出、物料、实验与能力配置均可通过 API 验证 |
| C 过程证据 | T17–T20 | 有效采集策略、持久事件、行动／返回／react 投影及断线续读可本地验收 |
| D 会话闭环 | T21–T29 | 幂等创建、多轮消息／附件／应答、停止、主动结束、最终回收、恢复可本地验收 |
| E 查询与比较 | T30–T34 | 历史、备注、重跑确认、跨实验最终结果比较及完整 API 契约 |
| F 页面闭环 | T35–T44 | 新页面与交互、独立前端测试、设计同步及浏览器冒烟 |
| G 总体验收 | T45–T46 | 故障闭环、回归、CI 门禁与目标 Blade 真实联调证据 |

编号是建议主路径。T04 缺凭据时不阻塞 B/C 和基于替身的 D/E/F；T23–T25 的真实协议映射必须最终用 T04 证据校正，T46 完成前不将功能标为生产可用。前端 T35 可在 T02 的类型／fixture 基础上提前搭建，真实接线仍遵循对应 API 依赖。

## 3. 开工前需形成的实施记录

由 T03/T04 填写本目录 `implementation-decisions.md`，每项注明“已验证／建议／待验证”和证据，不要求在未掌握事实时让用户猜 SDK 行为。

| 问题 | 收敛方式 | 未完成时的边界 |
| --- | --- | --- |
| 无提示、无物料启动 skill | 核对 beta.41 启动与激活契约；如需系统指令，记录可见文案和实际请求 | API 可支持空用户输入；远端执行保持未验证 |
| 提问、普通追问、附件及 pause/resume | 查本地 SDK 和现有 askuser_answer 构造，真实服务验证 | 使用替身推进状态机，不虚构远端字段 |
| 上传脚本执行位 | 验证 upload_session_skill 是否保留／可恢复权限 | 不把 ZIP 权限往返通过当作远端执行通过 |
| 默认正文采集档位 | fixture 验证 parsed/full 的真实字段，遵守部署上限 | 能力降档明确可见，不用原始日志旁路 |
| 带全用户输入的重跑表达 | 有序携带全部用户输入和附件，不机械重发旧指令、不带旧模型输出 | 不静默摘要／截断；协议未验证不宣称兼容 |
| 旧 skill catalog 来源（已确认） | `skill_eval.catalog_roots` 配置服务器只读目录，仅复制、不写回、不接受客户端任意路径 | 未配置明确提示，上传独立可用 |
| 限额、生成超时、空闲会话保留 | design 第 10 节作为可配置建议，确认远端上限和默认值 | 测试注入小限额验证；不把建议值写成已确认承诺 |

## 4. 逐项任务

### T01 · 记录工作区与测试基线

- [x] 完成 T01

实施记录（2026-10-22）：本地验收与变更范围见 [implementation-progress.md](implementation-progress.md)、[baseline.md](baseline.md) 和 [implementation-decisions.md](implementation-decisions.md)。

**依赖**：无。**范围**：现有配置、依赖、CI 和测试；新增 `baseline.md`。

**操作**：核对 SDK/CLI 实际版本、锁文件与配置入口；运行现有 Blade 聚焦测试和前端 build；记录后端全量与前端测试现状。核对 CI 注释中历史前端测试问题，不直接把注释数字当现状。禁止输出密钥。

**验收**：执行第 1 节命令；将通过、失败、skip 和环境条件分开记录。`git diff --check` 通过。原有失败有定位记录，新功能失败不得混入基线忽略。此任务不要求重装全局 CLI 或再次升级版本。

**完成条件**：baseline.md 明确实际版本、准确命令、当前失败／skip 与未验证条件；后续任务能据此判断是否引入回归。

### T02 · 建立独立测试夹具与 FakeBladeRuntime

- [x] 完成 T02

实施记录（2026-10-22）：本地验收与变更范围见 [implementation-progress.md](implementation-progress.md)、[baseline.md](baseline.md) 和 [implementation-decisions.md](implementation-decisions.md)。

**依赖**：T01。**范围**：`tests/skill_eval/__init__.py`、`tests/skill_eval/conftest.py`、`tests/skill_eval/fakes.py`、`tests/fixtures/skill_eval/`。

**操作**：建立临时 SQLite、临时 data_path、可控时钟、可暂停的远端替身与故障注入点；准备含 SKILL.md／Python／Shell／二进制／执行位的最小包，以及损坏包。记录 fake 的 create/chat/answer/stop/upload 次数与顺序。测试需在无 key、禁外网的条件运行。

**单测 U/I**：`uv run pytest -q tests/skill_eval/test_harness.py`。验证两个测试互不共享 DB／文件／事件；能用 barrier 精确制造并发和超时；测试失败不会留下后台 task。不要用真实 sleep 等待远端。

**完成条件**：后续任务可通过调用记录判断“没有发起远端动作”和“只发送一次”，无需访问服务。

### T03 · 固化 SDK 契约和待验证能力

- [x] 完成 T03

实施记录（2026-10-22）：本地验收与变更范围见 [implementation-progress.md](implementation-progress.md)、[baseline.md](baseline.md) 和 [implementation-decisions.md](implementation-decisions.md)。

**依赖**：T02。**范围**：`tests/skill_eval/test_sdk_contract.py`、fixture、`implementation-decisions.md`。

**操作**：读取已安装 SDK 的真实签名；以 respx 和事件 fixture 固化 create_session、upload_session_skill、upload_file、chat、subscribe、get_history、stop 与 askuser_answer 使用契约。将采集字段、会话自动启动、执行位等 unknown 单列，不凭方法存在判断全能力可用。

**单测 I**：`uv run pytest -q tests/skill_eval/test_sdk_contract.py tests/test_blade_sdk_compat.py`。断言二进制编码、模型字段、session/chat_run_id、暂停问题、历史关联 ID 与异常响应；请求不含场景回调／评分 token；契约变动明确失败。

**完成条件**：新增协议测试使用真实 SDK 编解码；SDK fixture 与平台 FakeRuntime 测试分层，不用同一个 mock 掩盖映射错误。

### T04 · 验证目标 Blade 的最小真实协议

- [x] 完成 T04

**依赖**：T03、可用的测试鉴权。**范围**：新增 `scripts/verify-skill-eval-blade.py`、`blade-verification.md`；脚本必须显式启用才运行。

**操作**：在用户指定预览服务执行小型自包含 skill，分别验证空用户输入和带提示／附件、实际 skill 激活、公开事件首段、提问应答、后续追问、补文件、停止、历史、最终文件与执行位。读取当时实际版本，保留脱敏 fixture，记录是否存在后台活动和回收静止确认方式。只清理本次创建的测试会话。

**验收 L**：预检模式只检查配置／版本，不创建会话；执行模式记录请求／响应身份和缺口。计划脚本命令为 `uv run python scripts/verify-skill-eval-blade.py --preflight`（只读预检）和 `uv run python scripts/verify-skill-eval-blade.py --execute`（真实执行），两者互斥，额外参数在实现时写入其 `--help`；测试 key 通过既有环境配置注入。脚本的 U/I 入口：`uv run pytest -q tests/skill_eval/test_verification_script.py`，断言未显式开启执行时没有创建会话。

**完成条件**：能力逐项记录 verified/unsupported/unknown；缺凭据保持未完成。unsupported 必须对应设计处理或后续修复，不用“上传成功”替代“实际执行通过”。

### T05 · 新增 Skill／版本／草稿／实验存储迁移

- [x] 完成 T05

实施记录（2026-10-22）：本地验收与变更范围见 [implementation-progress.md](implementation-progress.md)、[baseline.md](baseline.md) 和 [implementation-decisions.md](implementation-decisions.md)。

**依赖**：T02。**范围**：`backend/skill_eval/schema.py`、`repository.py`、`backend/db.py` 的迁移调用。

**操作**：落地 skills、releases、imports、drafts、versions、blobs、inputs、experiments 表；规范化 name/version key；创建唯一约束、外键和版本修订字段。使用现有 SQLite/WAL 和幂等迁移约定，不修改旧 experiments/runs 的语义。

**单测 I**：`uv run pytest -q tests/skill_eval/test_schema_assets.py`。新 DB、含旧业务记录的 DB、重复迁移、迁移失败回滚；同作用域同名／同 skill 同版本冲突；不同作用域隔离；旧任务行数及值不变。

**完成条件**：迁移后可用独立 repository 操作新表；不创建任何旧 task/attempt/scoring 行。

### T06 · 新增会话／交互／任务／事件存储迁移

- [x] 完成 T06

实施记录（2026-10-22）：本地验收与变更范围见 [implementation-progress.md](implementation-progress.md)、[baseline.md](baseline.md) 和 [implementation-decisions.md](implementation-decisions.md)。

**依赖**：T05。**范围**：schema/repository 扩展。

**操作**：落地 runs、messages、interactions、generations、rerun_plans、notes、jobs、requests、events、artifacts 表和 design 规定索引；实验 skill 一致性、run 内消息／事件序号、operation 幂等和最终产物路径唯一约束。

**单测 I**：`uv run pytest -q tests/skill_eval/test_schema_runs.py`。跨 skill 实验关联被拒绝；重复 event／operation 被拒绝；两个连接同时领取消息序号不重号；不同 run 的相同序号允许；重复初始化不丢消息。

**完成条件**：后续事件与状态测试可以直接 seed 合法运行，无需启动 SDK。

### T07 · 实现不可变 blob、manifest 与路径解析

- [x] 完成 T07

**依赖**：T05。**范围**：`storage.py`、原子文件写入与 manifest 构造。

**操作**：内容寻址存字节，校验后原子发布；manifest 保存路径、file_id、hash、编码、mode；内容 hash 排除 file_id／时间／显示名。所有路径经单一解析入口，API 不接受任意绝对文件路径。

**单测 U/I**：`uv run pytest -q tests/skill_eval/test_storage.py`。相同字节去重、不同执行位影响包 hash、不同 file_id 不影响内容 hash；写入中断不被 DB 引用；读后修改草稿不改变旧 blob；路径逃逸被拒绝。

**完成条件**：清理无引用临时对象不删除仍被草稿／快照／输入／产物引用的内容；清理与提交竞态有可控测试。

### T08 · 实现 SKILL.md／ZIP 校验与解包

- [x] 完成 T08

**依赖**：T07。**范围**：`packages.py`、测试包生成器。

**操作**：识别单正文、唯一外层根和完整包；校验 UTF-8 正文、frontmatter、目录、重复路径、符号链接、大小／数量／压缩比例；规范化 0644/0755；保留二进制与相对目录。

**单测 U/I**：`uv run pytest -q tests/skill_eval/test_packages.py`。缺正文／多个根、非法编码、绝对路径、`..`、规范化路径冲突、符号链接、压缩炸弹、逐项限额，以及合法脚本／二进制。错误指出具体条目，不遗留部分成功包。

**完成条件**：解析只产出受校验 manifest，不执行脚本、不覆盖 catalog 来源。

### T09 · 实现暂存导入、重名确认与 catalog 接口

- [x] 完成 T09

**依赖**：T08。**范围**：service/api 的 import/from-catalog/commit/get-import，`pyproject.toml`、锁文件。

**操作**：将 python-multipart 纳入运行依赖；上传只创建待确认 import；首次新名缺版本默认 0.0.1；同名缺号／重复号必须改名或换号；同名新号归入原 skill，创建未发布版本。实现已确认的服务器只读目录 catalog，使用部署配置而非用户请求指定目录。

**单测 I**：`uv run pytest -q tests/skill_eval/test_imports.py`。覆盖名称×版本状态矩阵，特别断言重复 beta 也拒绝；两个连接提交同号只有一个成功；重复 commit 返回原结果；失败保留 staging；过期重传、作用域隔离、catalog 内容变更指纹冲突。

**完成条件**：HTTP 测试真实 multipart → 临时文件 → DB；没有半成功 skill，无 catalog 时上传仍可独立完成，未配置目录时明确返回 unconfigured。

### T10 · 实现多文件草稿与原子暂存

- [x] 完成 T10

**依赖**：T09。**范围**：draft 查询／文件读取／PATCH、资源引用验证。

**操作**：changeset 支持新建、修改、重命名、删除、mode、二进制替换；校验最终整棵树后一次提交 draft revision，不更新 release。文本保留编码，无法无损编辑时按二进制处理。

**单测 I**：`uv run pytest -q tests/skill_eval/test_drafts.py`。一个 batch 同改正文和两个脚本；任一非法则全部回滚；不允许删除最终正文；revision 冲突保留原内容；跨作用域 blob 引用、未完成上传被拒绝；重命名与删除顺序按最终树处理。

**完成条件**：返回新 revision 与全包变更摘要；后台暂存不等于用户版本已保存。

### T11 · 实现未发布版本保存与正式版派生

- [x] 完成 T11

**依赖**：T10。**范围**：draft save service、release 乐观锁、历史派生草稿。

**操作**：保存全部编辑内容，更新未发布 current_version_id；同号可反复保存。正式版派生草稿没有可覆盖目标，首次保存要求新号并建立未发布版本；分别检查 draft/release revision。

**单测 I**：`uv run pytest -q tests/skill_eval/test_save_versions.py`。同一 beta 保存两次、旧 snapshot 字节保持；旧 run 引用不漂移；正式版覆盖拒绝；新号冲突拒绝；派生首次新号保存、第二次原号保存；并发保存仅一个版本指针更新成功。

**完成条件**：上传重复版本和编辑保存是不同入口，不能把 save 接口当 import 覆盖通道。

### T12 · 实现另建正式版的发布事务

- [x] 完成 T12

**依赖**：T11。**范围**：draft publish service/API。

**操作**：校验新版本号和期望修订，冻结全部确认内容，新建 published release，保留来源版本。目标号不能与任何已有版本冲突，发布不启动模型或外部同步。

**单测 I**：`uv run pytest -q tests/skill_eval/test_publish_versions.py`。空号／重复号／并发同号；发布后来源 beta 可改、正式版字节和指针不变；包含未保存脚本改动的发布确实冻结全包；发布失败不改变任何 release。

**完成条件**：正式版后续所有编辑入口均拒绝原地写入；服务端强制而非仅按钮置灰。

### T13 · 实现不保存版本的快照与完整导出

- [x] 完成 T13

**依赖**：T10。**范围**：snapshots/exports/version download、ZIP comment 元数据。

**操作**：接收完整 changeset+revision，冻结当前整包但不更新 release；导出相同快照，保留文件字节／目录／执行位及平台建议元数据；历史下载只读取 snapshot。

**单测 I**：`uv run pytest -q tests/skill_eval/test_snapshots_exports.py`。未保存正文+脚本均进入快照／ZIP，release 不变；ZIP 往返内容／mode 一致；不混入提示／备注／物料／产物；伪造导出元数据不绕过导入冲突；下载失败可重试。

**完成条件**：运行可引用无用户版本的新快照；跨标签编辑不改变已返回 snapshot_id。

### T14 · 实现独立任务物料与附件上传

- [x] 完成 T14

**依赖**：T07、T09。**范围**：uploads/inputs API、文件预览描述。

**操作**：区分 resource/input，流式大小限制、完整上传状态、归属校验与文件信息；输入独立存字节及 hash，不依赖外部路径。实现同名文件显式替换或重命名，生成远端稳定映射。

**单测 I**：`uv run pytest -q tests/skill_eval/test_inputs.py`。多文件、空文件、二进制、重复名、断传、超限、未完成引用和越权引用；删除本地原文件后历史 input 仍可读。无物料合法。

**完成条件**：供初次运行和后续消息共用；重复名不能静默改写旧输入字节。

### T15 · 实现命名实验的创建、查询与改名

- [x] 完成 T15

实施记录（2026-10-22）：本地验收与变更范围见 [implementation-progress.md](implementation-progress.md)、[baseline.md](baseline.md) 和 [implementation-decisions.md](implementation-decisions.md)。

**依赖**：T05。**范围**：experiment repository/service/API。

**操作**：用户新建命名实验，一个实验固定 skill；支持列表、详情、改名、skill 筛选；与旧平台实验 API 隔离。

**单测 I**：`uv run pytest -q tests/skill_eval/test_experiments.py`。同 skill 多实验、实验改名、禁止更换 skill、跨作用域访问、未知实验；不因实验同名误合并身份。

**完成条件**：新运行必须有合法 experiment_id；上传 skill 不隐式建立不可见实验。

### T16 · 实现功能配置、模型目录与能力报告

- [x] 完成 T16

**依赖**：T03、T06。**范围**：`backend/config.py`、capabilities/models API、`octagon.yaml.example`、生命周期注册。

**操作**：新增 skill_eval 开关、限额／并发／超时／空闲保留配置；复用模型目录和既有鉴权。报告 SDK/CLI/服务实际版本及上传／事件／历史／交互能力，区分 unavailable 和 unknown。配置上限不写死到组件，新功能执行开关默认关闭，直到最终验收明确通过。

**单测 I**：`uv run pytest -q tests/skill_eval/test_capabilities.py`。缺 SDK／CLI、401、不可达、目录失败、未知模型、版本不兼容、降级；仅有 key 不等于全能力 verified；无 CLI 不误判 SDK 通道不可用。禁用功能保留历史读路径。

**完成条件**：后端只接受 blade-agent；服务地址不由用户请求任意指定；display_only 下无探测型执行和后台启动。

### T17 · 实现正文采集策略与脱敏边界

- [x] 完成 T17

**依赖**：T03、T07、T16。**范围**：`capture.py`、复用 wire policy/redaction。

**操作**：默认请求可获得的公开思考／参数／返回正文；按部署与来源能力求 effective，保留降档原因；任何 raw、blob、日志、SSE 都执行相同策略，不能隐式套场景 metadata 默认值压低请求。

**单测 U/I**：`uv run pytest -q tests/skill_eval/test_capture_policy.py`。四档与上限组合、缺字段原因、敏感头脱敏、超长正文截断；off/metadata 后遍历全部落盘文件与日志，确认不含专门放入的正文 canary。不能仅断言 API 隐藏正文。

**完成条件**：capture manifest 可解释 requested/effective/coverage；不生成模型未提供的思考。

### T18 · 实现持久来源事件、去重与投影提交

- [x] 完成 T18

**依赖**：T06、T17。**范围**：事件 repository、capture writer、来源段提交。

**操作**：来源证据按策略持久化，已提交事件分配 run-local seq，事务后发布通知；保存原始来源和 ID／stream 实例。为重放与断流补缺保留 provenance。实施收敛：来源事实与 seq 在 SQLite 同事务存储，无独立 raw/JSONL 文件。

**单测 I**：`uv run pytest -q tests/skill_eval/test_event_store.py`。同 source_event_id／sequence 重放、序号重置、新 stream、相同文本不同消息；来源事实/seq 提交失败回滚、DB 已成而通知失败的恢复；并发追加不重号；来源异常可定位且已提交事件不丢。

**完成条件**：读者看不到未提交事件；通知失败可用 DB 游标追上，不必重发任务。

### T19 · 实现模型—行动—返回—react 投影

- [x] 完成 T19

**依赖**：T18。**范围**：`process.py`、提取 Blade normalizer 公共解析；保留旧入口兼容。

**操作**：按固定来源 head 确定性组装消息、公开 reasoning、工具调用／返回、用户问答与附件；按 loop/tool_call_id 配对，react 引用下一真实模型事件。明确关联与推断／未知分开，终态可从事实重建；当前 schema/head 游标无持久增量缓存。

**单测 U/I**：`uv run pytest -q tests/skill_eval/test_process_projection.py tests/test_blade_sdk_compat.py`。并行反序返回、同工具多次重试、下一行动无自然语言 react、跨 loop、缺 ID、socket/history 对账、同文不同消息、reasoning summary 与普通消息分开；react 不重复计数，缺上下文不伪造。

**完成条件**：fixture 重放多次投影一致；原始事实不因修正投影被覆盖；旧 adapter 正常解析。

### T20 · 实现过程分页、证据读取和 SSE 续读

- [x] 完成 T20

**依赖**：T18、T19。**范围**：events/process/evidence API。

**操作**：持久 seq 续读、Last-Event-ID、heartbeat、schema/head 游标、搜索和正文按需加载；所有引用校验 run 与作用域。浏览器订阅不触发 chat。

**单测 I**：`uv run pytest -q tests/skill_eval/test_process_api.py tests/skill_eval/test_sse.py`。断线后精确补读、提交与订阅竞态无漏事件、心跳不推进 seq、投影重建 reset_required、旧游标 snapshot_required、跨 run 证据访问拒绝；客户端取消订阅不泄漏后台任务。

**完成条件**：可用 seed 运行查看过程，无需 SDK 在线；采集缺口有具体原因。

### T21 · 实现幂等创建运行与冻结输入

- [x] 完成 T21

**依赖**：T06、T13–T16。**范围**：`POST /runs`、service 创建事务。

**操作**：校验实验与 skill、模型、snapshot、input 引用、agent 与能力；一个事务写 queued run、job 和幂等映射；响应立即可查询。冻结版本显示标签、revision 和完整初始输入，不在执行时解析可变版本指针。

**单测 I**：`uv run pytest -q tests/skill_eval/test_create_runs.py`。同 key 同请求返回同 run；同 key 改请求 409；不同 key 相同输入新 run；空提示+空物料成功；无效 model/agent/实验拒绝；中途 DB 异常无孤立 job；后续保存 beta 不改变旧 run。

**完成条件**：请求线程不等待远端；现有场景表和评分队列没有新增记录。

### T22 · 实现持久 worker、租约与生成并发额度

- [x] 完成 T22

**依赖**：T21。**范围**：`worker.py`、生命周期注册、runtime 协议。

**操作**：领取／续租／释放 job，每 run 串行远端写操作，prepare/message/finish/collect 独立 operation；等待用户时保留会话但释放生成额度。单次生成超时与空闲保留分开。

**单测 I**：`uv run pytest -q tests/skill_eval/test_worker.py`。两个 worker 同 job 只领取一个；租约过期重新领取但先核查远端；等待输入期间另一 run 可生成；同会话消息有序；时钟推进触发生成超时但不误杀等待用户状态。

**完成条件**：仅依赖 FakeRuntime 可连续执行多条 operation；display_only／禁执行配置不启动消费者。

### T23 · 接入 SDK 会话、skill 与初始物料上传

- [x] 完成 T23

**依赖**：T03、T17、T22；真实映射由 T04 校验。**范围**：`runtime.py`、必要的 Blade 公共协议提取。

**操作**：创建新 session 后先持久化身份；上传运行快照和 inputs，记录各回执；确认模型与 skill 激活后发送启动消息并进入采集。无用户提示时使用经验证的可见启动契约；原 skill 字节不暗改。

**单测 I**：`uv run pytest -q tests/skill_eval/test_runtime_prepare.py`。严格调用顺序、部分上传失败不 chat、二进制、执行位限制、路径映射、模型配置不一致；每个新 run 新 session，恢复不新建；订阅就绪前不漏首段，双来源去重。

**完成条件**：无 env token、scorer、本机 callback 或自动 CLI fallback；actual uploaded／read／used 证据分开。

### T24 · 实现用户追问与追加附件投递

- [x] 完成 T24

**依赖**：T14、T22、T23。**范围**：messages API、消息 outbox、runtime send_message。

**操作**：先持久化用户消息，再上传本条附件并保存回执，最后调用同 session 的 chat；允许文字、附件或两者；追加日志不改初始 input snapshot。一次回复完成只切 waiting_continue。

**单测 I**：`uv run pytest -q tests/skill_eval/test_messages.py`。二次／三次追问 session 不变；同 message key 只投递一次；附件上传失败不提前发正文；并发消息串行；空自由消息无附件拒绝但初次空启动合法；chat:end 不将 run completed。

**完成条件**：每条消息可查询 pending/sent/unknown/failed，补文件不会静默覆盖旧 input。

### T25 · 实现智能体提问与用户真实应答

- [x] 完成 T25

**依赖**：T19、T24。**范围**：interaction 持久化、answer API、runtime answer_interaction。

**操作**：接收暂停问题与实际 tool_call_id，进入 waiting_input；用户答案＋附件通过真实结构化应答继续原 session；明确普通追问和回答 pending interaction 的分派。

**单测 I**：`uv run pytest -q tests/skill_eval/test_interactions.py`。无自动代答；问题原文可见；回答仅一次、两个连接竞答冲突；旧 interaction/已结束 run 拒绝；附件先就绪再 answer；刷新后问题仍在；headless 配置不静默跳过交互。

**完成条件**：用户输入原文、附件与远端回执可追溯；不沿用旧 benchmark 自动拒绝话术。

### T26 · 实现停止当前生成与超时处理

- [x] 完成 T26

**依赖**：T24、T25。**范围**：stop-generation API、远端停止任务。

**操作**：持久停止请求，针对真实 session/chat_run_id 调用 stop；确认前显示 stopping；已自然结束按事实处理，停止后 run 可继续。生成超时终止真实活动并区分可恢复错误与会话失效。

**单测 I**：`uv run pytest -q tests/skill_eval/test_stop_generation.py`。重复 stop、stop 与 chat:end 竞态、stop 失败重试、停止前未发出的队列消息取消、停止后可追加；只取消本地协程不算成功；已有消息和证据不丢。

**完成条件**：停止确认状态和远端活动一致，不提前把整次运行标 cancelled。

### T27 · 实现最终产物回收与预览适配

- [x] 完成 T27

**依赖**：T14、T23、T26。**范围**：`artifacts.py`、索引／下载 API、复用 artifact_preview。

**操作**：远端静止后 list/download 最终文件；过滤未改输入和 skill 源，原地修改输入保留观察证据并标超出首期范围；按 ID 寻址，验证路径／类型／数量／大小；临时下载完成后发布。运行中预览仅作可替换缓存，不生成轮次产物历史。

**单测 I**：`uv run pytest -q tests/skill_eval/test_artifacts.py`。同文件先后改写最终只索引最后内容；远端文件在下载中变化、超限、路径逃逸、二进制／Office／HTML、单文件失败；本地冻结 skill 不受远端修改影响。失败回收可独立重试，不调用 chat。

**补充验收**：纯文字结论且零文件可正常结束；输入原件保持不变，观察到原地修改／删除只标超出评测范围；列目录失败不能伪装为空目录。首期不实现副作用验证器。

**完成条件**：只保留终态产物索引；未下载项保留原因；复用预览不绕过现有渲染隔离。

### T28 · 实现主动结束运行及最终结果索引

- [x] 完成 T28

**依赖**：T25–T27。**范围**：finish API、状态机、final-result 生成。

**操作**：finish 原子关闭消息入口；有活动生成先真停止，等待可确认静止后回收最终消息、可观察操作／影响与产物；写最终索引和完整性。已结束 run 只读但备注可改。

**单测 I**：`uv run pytest -q tests/skill_eval/test_finish_run.py`。等待继续／等待回答／生成中结束；finish 重试；与发送消息双向竞态；晚到事件保留但不重开 run；回收失败标 partial；结束后新消息 409；无每轮产物快照；最终索引不随 beta 保存改变；纯文字结论且零文件可正常结束；观测到外部写入或原地修改输入时保留真实过程并标超出评测范围，不生成副作用验证成功结论。

**完成条件**：用户主动结束与 SDK chat:end 明确不同；failed/cancelled 也能形成部分结果索引，不自动生成虚构总结。

### T29 · 实现启动恢复与不确定投递核查

- [x] 完成 T29

**依赖**：T18、T22–T28。**范围**：`recovery.py`、应用生命周期、任务故障注入。

**操作**：按 prepare/message/answer/stop/finish 各 phase 和回执恢复；已绑定 session 不重发初始输入。接受状态未知时先查远端身份／历史，无法确认保持可解释失败；服务失效不得创建新 session 冒充续聊。

**单测 I**：`uv run pytest -q tests/skill_eval/test_recovery.py`。逐一在远端成功本地未写回、写回后通知前、上传后发送前、chat 接受后超时、answer 接受后超时、finish 回收中注入崩溃；重建 worker 后远端 create/chat/answer 次数不多于已确认操作；waiting_input/continue 恢复不占生成额度。

**完成条件**：只读部署不恢复执行；无可靠远端幂等证据时不自动重发；坏记录隔离且不阻止其他运行恢复。

### T30 · 实现 Skill／实验历史、模型分组与游标

- [x] 完成 T30

**依赖**：T15、T21、T28。**范围**：list runs、record-groups、详情读模型。

**操作**：skill 汇总全部版本／实验，支持实验／版本／状态／时间／模型筛选；全局按 agent+provider+canonical model 分组，requested 未确认身份独立分组。稳定游标，详情支持恢复地址。

**单测 I**：`uv run pytest -q tests/skill_eval/test_history.py`。同别名不同模型不误合并；未确认模型不与实际身份合并；相同 created_at、插入新记录后的分页无重复／漏既有项；跨实验筛选、无结果、越权和无效游标。

**完成条件**：列表读取不全量加载正文／产物；返回实验、用户版本、真实配置、状态和备注摘要。

### T31 · 实现每次运行的可选备注

- [x] 完成 T31

**依赖**：T21、T30。**范围**：note PUT、详情与历史摘要。

**操作**：缺行默认空备注；支持补充、修改、清空、revision 冲突；不进入模型上下文或 skill 包，new run 不继承来源备注。

**单测 I**：`uv run pytest -q tests/skill_eval/test_notes.py`。未填不影响运行／重跑／比较；失败／取消／结束后均可备注；清空再打开为空；并发保存 409；超长拒绝；不同 run 不串备注；ZIP、输入快照和 SDK 请求不含备注 canary。

**完成条件**：每 run 一份备注，不增加每轮备注或评分字段。

### T32 · 实现必须手动确认的重跑计划

- [x] 完成 T32

**依赖**：T13、T24、T25、T28。**范围**：rerun-plans/create/confirm、创建运行校验。

**操作**：reuse_all 收齐初始提示、所有用户追问／答案和上传附件；replace 清空后可重新提供或全空。显示清单，记录 cutoff/hash，确认后创建关联来源的新 run/session；不带旧模型输出／产物／备注或远端状态。

**单测 I**：`uv run pytest -q tests/skill_eval/test_rerun_plans.py`。不选择／不确认不能创建重跑；来源新增输入使计划失效；全部用户内容与附件顺序不丢、重复名映射不覆盖；replace 空输入合法；超限不静默删减；不机械发送多次旧消息造成副作用；未保存脚本快照确实用于新 run。

**完成条件**：计划固定且可审计，来源运行不变，API 不能绕过 UI 确认。

### T33 · 实现跨实验的最终结果比较与按需差异

- [x] 完成 T33

**依赖**：T13、T27、T28、T30、T31。**范围**：`comparison.py`、query/diff API。

**操作**：至少 2 次，支持 3+；只能选择已结束运行，固定 final-result 索引及回收 generation；比较整次最终回复、可观察影响、产物、完整用户输入、skill 内容、模型与备注；重型 diff 按需。

**单测 I**：`uv run pytest -q tests/skill_eval/test_comparisons.py`。跨实验／不同 skill、不同模型；运行中拒绝；失败／取消标部分；不可访问项保留缺失说明，不足两项明确失败；二进制不做文本 diff；大文件限制、基准切换、补收后旧比较不偷偷漂移；不接受按旧轮次产物比较；零文件文字结果可比较；展示范围限制，不比较或承诺外部写入／原地修改输入的实际效果。

**完成条件**：不产生自动评分／获胜者，不按不同 run 的相同 event seq 拼成语义对齐。

### T34 · 完成 API 注册与整体边界契约测试

- [x] 完成 T34

**依赖**：T09–T33。**范围**：独立 router、main 生命周期、异常转换和 API 类型。

**操作**：逐项对照 design 第 8 节接口，统一错误、状态码、ID 归属和分页；写完整 multipart／JSON／SSE 契约用例。补齐开关、只读部署和关闭任务清理路径。

**单测 I**：`uv run pytest -q tests/skill_eval/test_api_contract.py tests/skill_eval/test_display_only.py`。每种资源越权、未知 ID、非 Blade agent、非法 revision/游标；全部写接口及恢复被 display_only 阻断，历史 GET 可读；响应／日志不泄漏 key；后端缺 Blade extra 时旧功能可导入。

**完成条件**：覆盖声明的 API 而非只测 service；缺失接口未补齐则此项不能完成。

### T35 · 建立前端类型、测试入口与路由外壳

- [x] 完成 T35

**依赖**：T02、T16；实际接线需 T34。**范围**：`web/src/skillEval/api.ts`、`types.ts`、测试 render helper、main/AppShell/i18n。

**操作**：新增两个导航与全部页面路由，遵循 design 的错误／游标／事件模型；测试统一包装 I18nProvider 和 MemoryRouter；请求可控且 abort 可测。检查 Vitest 失败退出行为，不使用把错误转换成成功的脚本。

**单测 C**：`npm --prefix web test -- src/skillEval/api.test.ts src/skillEval/routes.test.tsx`。中文／英文导航、直达地址、错误响应、禁用能力、卸载取消请求；用一次受控临时失败验证 runner 退出非零后移除该用例，记录结果，不提交故意失败测试。

**完成条件**：独立新模块测试可在无后端下运行；`npm --prefix web run build` 通过。

### T36 · 实现导入确认、重名冲突与 Skill 工作区

- [x] 完成 T36

**依赖**：T09、T30、T35。**范围**：工作区、ImportPanel、包资源树。

**操作**：上传／catalog 并列入口，解析／确认／冲突／过期状态；名称版本可调整；右上 skill 详情，右下历史，不默认显示单次结果。

**单测 C**：`npm --prefix web test -- src/skillEval/ImportPanel.test.tsx src/skillEval/Workspace.test.tsx`。首次 0.0.1、同名无号／重复 beta 阻止、换号成功后归原 skill、冲突保留 File 和表单、无 catalog 仍可上传、空历史／分页／切 skill 更新范围。

**完成条件**：校验说明定位字段，不能静默自动换号或覆盖现有版本。

### T37 · 实现多文件编辑缓冲与整包差异

- [x] 完成 T37

**依赖**：T10、T13、T35。**范围**：SkillPackageEditor、文件树、标签与差异组件；需要编辑器依赖时更新 package-lock。

**操作**：SKILL.md／脚本多标签独立缓冲，二进制预览／替换；支持目录变化、mode 和来源差异；全包 operations 队列，不仅当前标签。加入站内离开和 beforeunload 保护。

**单测 C**：`npm --prefix web test -- src/skillEval/SkillPackageEditor.test.tsx`。两文件编辑后切换不丢、删除／重命名／新增摘要、恢复单文件不伤其他缓冲、来源证据切换、编码不能保存时不替换字符；draft 冲突保留全部内容；离开提示可取消。

**完成条件**：最终 API payload 包含整包变更；编辑不会执行脚本。

### T38 · 实现保存版本、发布、未保存试跑与导出操作

- [x] 完成 T38

**依赖**：T11–T13、T37。**范围**：编辑操作栏、版本状态与发布／首次保存面板。

**操作**：原 beta 同号保存；正式版来源首次保存要求新号；发布另建新号、保留原版本；导出当前缓冲与试跑不先调用版本保存。导出和运行冻结完整当前内容。

**单测 C**：`npm --prefix web test -- src/skillEval/VersionActions.test.tsx`。保存两个 dirty 标签、发布号冲突、来源 beta 不消失、正式版不可覆盖；spy 断言试跑／导出不请求 save；快照失败不启动 run／不丢缓冲；发布不调用 run。

**完成条件**：UI“版本已保存”仅在 save 成功显示，暂存或快照成功不误报。

### T39 · 实现实验与运行配置

- [x] 完成 T39

**依赖**：T14–T16、T21、T35、T36。**范围**：实验选择／新建／改名、模型与可选输入配置。

**操作**：无实验先创建或选择；固定 skill 归属、允许切该 skill 版本及模型；提示／附件均可空；展示默认正文采集与有效降档。运行按钮为一次用户动作生成稳定幂等 key，网络重试复用。运行配置处常驻展示首期支持文字／新文件输出、暂不支持外部写入与原地修改输入的范围提示，不宣称已强制隔离。

**单测 C**：`npm --prefix web test -- src/skillEval/RunConfiguration.test.tsx`。唯一 blade-agent、只有 skill 也能运行、上传未完成阻止、选择其他实验不能跨 skill、连续点击不创建双 run、手动再次运行使用新 key、模型不可用及采集受限清楚显示；运行前可见范围说明，不能误写成已拦截所有副作用。

**完成条件**：排队记录立即出现在所选实验历史，详情能直达。

### T40 · 实现正常对话、附件、问题应答与主动结束

- [x] 完成 T40

**依赖**：T24–T28、T35、T39。**范围**：RunDetail、ChatComposer、InteractionCard、终态结果。

**操作**：多轮对话和追加附件；pending 问题用 answer API，普通追问用 messages；区分生成中、等待回答、等待继续、停止确认和结束回收。结束后转只读、保留最终结果和一个运行备注入口。

**单测 C**：`npm --prefix web test -- src/skillEval/RunDetail.test.tsx src/skillEval/ChatComposer.test.tsx`。回复结束仍可追问；问题+附件提交、附件失败保留输入；stop 后可继续；finish 后禁止发送；确认中防重、服务端竞态冲突、刷新恢复问题与会话；不显示各轮产物回退入口；纯文字结果显示正常，无文件不报失败，详情可见首期范围限制。

**完成条件**：用户离开页面不请求 finish，不因组件 remount 再发送启动消息。

### T41 · 实现完整流程和实时阅读交互

- [x] 完成 T41

**依赖**：T19、T20、T35、T40。**范围**：ProcessView、轮次索引、来源／缺口面板与 SSE hook。

**操作**：思考／行动／返回／react 结构、用户问答／附件、原始时间视图、搜索和上下文入口；暂停跟随、未读数、generation reset 和断线补读。

**单测 C**：`npm --prefix web test -- src/skillEval/ProcessView.test.tsx src/skillEval/useRunEvents.test.tsx`。上翻不抢滚动、恢复跟随、相同 ID 重放不双计数、并行返回正确、缺字段原因可见；metadata 不显示正文入口假成功；多页补读后续流不漏；卸载不残留连接。

**完成条件**：过程逐轮查看不暗示存在逐轮产物快照；无私有推理占位假文案。

### T42 · 实现分组历史、详情返回与可选备注

- [x] 完成 T42

**依赖**：T30、T31、T35、T40。**范围**：RecordGroups、RunHistory、RunNote。

**操作**：智能体/provider/model 一级分组、二级运行；实验／版本筛选，URL 保存筛选／分页／选中项；备注 save/clear/conflict 状态与摘要刷新。

**单测 C**：`npm --prefix web test -- src/skillEval/Records.test.tsx src/skillEval/RunNote.test.tsx`。返回恢复展开组／筛选／滚动、未确认模型单列、无备注显示可选、补充／修改／清空、失败保留、切 run 不串输入；失败／结束运行都能备注。

**完成条件**：不为每轮消息创建备注，不把备注作为继续或比较的必填项。

### T43 · 实现重跑确认与多次最终结果比较

- [x] 完成 T43

**依赖**：T32、T33、T38、T40、T42。**范围**：RerunConfirm、ComparePage。

**操作**：重跑必须选择带上全部输入附件／重新提供并确认；展示全部清单，来源更新要求重确认。比较 2、3、4+ 次终态 run，跨实验可选，基准切换、横向浏览、窄屏基准／当前列切换；重型内容按需读。

**单测 C**：`npm --prefix web test -- src/skillEval/RerunConfirm.test.tsx src/skillEval/ComparePage.test.tsx`。未确认不发 run；带全内容／replace 全空；来源变化；未结束项拒绝；缺失项与不足两项、跨 skill 差异、大文件／二进制；没有轮次产物选择；旧结果索引不随补收自动漂移。

**完成条件**：重跑新 run/session 且保留 experiment/source 关系；试跑当前未保存文件仍不触发版本保存。

### T44 · 同步 Pencil、导出预览并做浏览器冒烟

- [x] 完成 T44

**依赖**：T36–T43。**范围**：当前 worktree 的 `skill-eval.pen`、`ui/`、[ui-change-list.md](ui-change-list.md)。

**操作**：按调整清单重绘新建实验、版本保存／发布、导入冲突、重跑确认、问题应答、等待继续、主动结束和最终比较状态；用 Pencil 工具编辑，不读写加密内容；导出更新画廊并逐张目视检查。启动本地 app，用 FakeRuntime 的浏览器场景核对交互和窄屏。

**验收 C/人工**：前端组件测试已由 T36–T43 覆盖；本项记录实际点击的路径、预期／实际和截图。检查页面不再出现强制“保存后才能试跑”或模型回复即整次完成的旧语义。design／画板／产品页面保持一致。

**完成条件**：所有调整清单项有对应画板／截图与检查记录；不将静态 PNG 当作端到端执行证据。

### T45 · 本地端到端、故障注入及旧能力回归

- [x] 完成 T45

**依赖**：T29、T34、T40–T43。**范围**：`tests/skill_eval/test_workflows.py`、`test_fault_matrix.py`、回归记录。

**操作**：通过真实本地 HTTP/API 和临时 DB 串联完整业务，远端仍用 FakeRuntime。将已实现的故障场景做跨模块组合，不仅重复各 service 单测。

**验收 I**：`uv run pytest -q tests/skill_eval/test_workflows.py tests/skill_eval/test_fault_matrix.py`。主链为导入→命名实验→未保存试跑→提问+附件→继续追问→结束→备注→微调→确认带全输入→重跑→三次比较→发布新号→导出重导。第二链使用全空输入和 replace 全空。故障链覆盖重启、断流、上传失败、接受未知、finish/消息竞态和最终产物回收失败，断言远端动作次数和历史不变。

**完成条件**：执行 `uv run pytest -q tests/skill_eval`，再执行已有 Blade 聚焦测试及 `uv run pytest -q`；前端执行独立模块测试和 build。仅在新增失败／变更时重跑相关检查，不用无目的轮询替代定位。

### T46 · CI、部署说明与目标服务最终验收

- [x] 完成 T46

**依赖**：T04、T44、T45。**范围**：CI、配置示例、部署／恢复说明、`acceptance.md`。

**操作**：将 `tests/skill_eval` 纳入既有 pytest 门禁，新增明确失败退出的 Skill Eval 前端测试门禁；原有全量前端问题按 T01 基线单列，不通过忽略本模块失败解决。保留 build／编译检查。真实凭据不进 CI fixture；目标预览服务做全链验收，记录实际版本、时间和可追踪身份。

**验收 U/I/C/L**：执行第 6 节最终命令；真实服务完整复现 T45 主链，额外验证真实模型公开字段、实际 skill 使用、远端停止、问答与附件、最终产物和副作用证据。对 REQ-01–REQ-23 每项填写 pass/fail/blocked 与证据；未完成不得因本地全绿写通过。

**完成条件**：目标服务联调、独立前端门禁、旧能力回归及文档均通过后才允许开启功能。外部部署／发布动作按会话已有授权范围执行，不由 tasks 文档自动触发。

## 5. 需求与任务对应

| 需求 | 实现与验收任务 |
| --- | --- |
| REQ-01 导航与工作区 | T35、T36、T39、T44 |
| REQ-02 上传可独立运行 | T08、T09、T21、T23、T36、T45 |
| REQ-03 资源与独立物料 | T10、T14、T24、T37、T39 |
| REQ-04 未运行导出与往返 | T08、T13、T38、T45 |
| REQ-05 幂等运行 | T21、T22、T23、T29、T39 |
| REQ-06 运行中过程与缺口 | T17–T20、T23、T41 |
| REQ-07 正确关联与 react | T18、T19、T25、T41 |
| REQ-08 真实停止与恢复 | T20、T26、T28、T29、T40、T45 |
| REQ-09 详情与返回 | T27、T30、T40、T42 |
| REQ-10 未保存重跑、输入确认 | T13、T21、T32、T38、T43、T45 |
| REQ-11 多次最终结果比较 | T33、T43、T45 |
| REQ-12 历史分组 | T30、T36、T42 |
| REQ-13 可选备注与准确版本导出 | T13、T31、T38、T42 |
| REQ-14 beta.41 真实联调 | T03、T04、T46 |
| REQ-15 仅 blade-agent | T16、T21、T34、T39 |
| REQ-16 全包编辑保存试跑 | T10–T13、T37、T38、T43 |
| REQ-17 来源证据及差异 | T19、T32、T37、T41、T43 |
| REQ-18 上传重名／重号冲突 | T05、T09、T36 |
| REQ-19 beta 保存与另号发布 | T11、T12、T38 |
| REQ-20 命名实验与归属 | T05、T06、T15、T21、T30、T33、T39 |
| REQ-21 可选输入、真实交互 | T03、T04、T14、T23–T25、T40、T46 |
| REQ-22 主动结束、最终产物 | T26–T29、T33、T40、T43 |
| REQ-23 正式版派生及快照固定 | T11–T13、T21、T38、T43 |

## 6. 检查命令与证据要求

每项任务使用其局部命令验收；阶段完成后才运行对应累计测试。命令中的新目录及测试文件以对应任务已创建为前提。

```sh
# 后端新模块全部本地测试（不得自动访问真实服务）
uv run pytest -q tests/skill_eval

# 既有 Blade 升级与恢复兼容
uv run pytest -q tests/test_blade_sdk_compat.py tests/test_blade_cli_adapter.py tests/test_blade_priority_recovery.py

# 最终后端回归与编译
uv run pytest -q
uv run python -m compileall -q backend octagon

# 新功能独立前端测试与构建
npm --prefix web test -- src/skillEval
npm --prefix web run build

# 锁文件与变更检查
uv lock --check
git diff --check
```

前端全量 `npm --prefix web test` 应在 T01 记录基线，并在最终验收对比；历史失败需明确列出，不能据 CI 旧注释直接忽略，也不能把只跑新模块测试说成全量前端通过。T46 至少为新模块建立可靠测试门禁。

最终 `acceptance.md` 应包含：任务完成表、23 项需求证据、失败／skip／blocked 清单、实际服务／SDK／CLI 版本、local 与 live 的明确分类、已知采集缺口、空闲会话与恢复约定。任务逐项按实际验收更新；本地与真实验收分开，不将已有基础升级或资产模块测试冒充完整功能开发完成。

## 7. 完成记录

2026-10-22：T01–T46 的实现与当前验收完成。逐任务测试入口仍可执行；统一结果、真实环境版本、任务组证据及实施差异见 [acceptance.md](acceptance.md)。未提交、合并或部署。旧前端 88 项失败与后端 4 项既有 skip 单列，不属于新增模块通过的证明；真实崩溃全窗口、所有模型及文件格式未穷举。
