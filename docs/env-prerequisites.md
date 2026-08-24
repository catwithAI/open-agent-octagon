# Env 场景前置依赖清单

agent-octagon 有 13 个评测场景（`envs/<name>/`）。每个场景要跑通/拿到分数需要满足的前置条件如下。
本机基准：**macOS (darwin) + Python 3.11 + uv**。

> 快速判断：**能干净跑的 8 个**（只需 python3）：example-coding-chain、example-hitl、travel-planner、
> apple-incremental-game、gdpval-prepaid-amortization-db、gdpval-prepaid-amortization-official、
> edgebench-juliet、agent-parallel-scheduling。其余有硬依赖，见下。

## 分类归总

### ✅ 零特殊前置（本机可直接干净跑，只需 python3 / 标准库）
| env | 说明 |
|---|---|
| **example-coding-chain** | skill 薄壳随 env 自带（install_mode: copy），纯 mock 10 步计算链 |
| **example-hitl** | sqlite mock，scorer 读 `env.db` |
| **travel-planner** | sqlite mock，纯模型 + MCP，最适合做通信观测干净样例 |
| **agent-parallel-scheduling** | sqlite mock + 固定延迟 async 工具；测工具并行调度（并发度 / 依赖保真度）；scorer 纯 trace + sqlite 确定性评分 |
| **apple-incremental-game** | Python tester（跨平台 ASCII，非 ELF） |
| **gdpval-prepaid-amortization-db** | sqlite + json，输入数据自带，**无 LLM judge** |
| **gdpval-prepaid-amortization-official** | zipfile + xml 自解 OOXML（不需 openpyxl/pandas/LibreOffice），数据自带 |
| **edgebench-juliet** | bash + python3 评分（不编译 C++）；隐藏数据集 34MB facts.jsonl 已随仓库 |

### 🔗 需外部 skill 源（`SELECTED_SKILLS_DIR` 必须导出，否则 core.py 加载即失败）
| env | 说明 |
|---|---|
| **example-tool-use-a** | core.py 顶层读 `SELECTED_SKILLS_DIR`，未设直接 raise；import skill 的 tools.py |
| **example-tool-use-b** | 同上，SKILL_DIR = 外部专有 skill 目录下的子 skill |
| **example-tool-use-c** | 同上，SKILL_DIR = 外部专有 skill 目录下的子 skill |

- 这三个 **只认环境变量、不读 octagon.yaml 默认路径**。默认目录 `<SELECTED_SKILLS_DIR>` 需指向外部专有 skill 源，需手动：
  ```bash
  export SELECTED_SKILLS_DIR=/path/to/selected-skills
  ```
- 运行时还需 **cartopy / shapely / matplotlib** 出 figure.png（否则 `artifact_completeness` 维度掉分）。scorer 本身靠已提交 baseline JSON，无外部工具。

### 🖥️ 需 LibreOffice + PyMuPDF + 可调用工具的 Blade Agent Judge（三重依赖，最重）
| env | 依赖 | 缺了会怎样 |
|---|---|---|
| **ppt-visual-repair** | **LibreOffice/soffice**（pptx→pdf 渲染）+ **PyMuPDF/fitz**（pdf→png）+ **带图片查看工具的 Blade Agent Judge session**（占评分 80%）+ pptx 参考文件（自带） | 缺 soffice → `office_render`(10%)=0 且 previews 为 None 导致 judge 被跳过；缺 Blade server、API key、可用模型或图片查看能力 → `llm_visual_judge`(80%)=0 |

本机配置方式（已验证可跑）：
```bash
# LibreOffice：brew 镜像慢，用清华镜像下 dmg 装到 /Applications
#   soffice = /Applications/LibreOffice.app/Contents/MacOS/soffice
# PyMuPDF：
uv pip install PyMuPDF
# Judge 统一通过 Blade Agent session 运行；模型在 Blade 模型目录中选择。
```
octagon.yaml：
```yaml
blade:
  base_url: "http://127.0.0.1:8020"
  api_key: "sk-blade-..."

llm_judge:
  model: "<Blade 模型目录中的模型 ID>"
  timeout: 900
  keep_session: false
```

`llm_judge.model` 只传给 `BladeAgentClient.create_session(model=...)` 选择 Judge
session 的底层模型。材料通过 Blade upload API 上传，Judge 再在自己的 workspace
中调用图片查看、Python、openpyxl 或其他文件工具。不要把需要检查文件的 Judge
改成裸 `/chat/completions` provider；裸模型 API 没有 Blade workspace 和工具调用链。

### 🔨 需编译器 + Linux 运行环境（macOS 本机跑不了）
| env | 依赖 | 缺了会怎样 |
|---|---|---|
| **ad-placement** | **g++**（编译 C++17 提交）+ 打包 tester 二进制 | tester 是 **x86-64 Linux ELF**，macOS 原生不可运行 → 需容器 / Linux；否则 tester 调用失败、batch_score=0。50 用例自带 |

### 📦 需媒体工具 + 联网 TTS
| env | 依赖 | 缺了会怎样 |
|---|---|---|
| **recording-recap** | **ffmpeg / ffprobe** + Python **edge_tts**（微软联网 TTS，voice=zh-CN-YunyangNeural） | 缺则渲染流水线/workflow_completion 走不通、成片时长核对失败 |

## 通用前置（所有 env 之上）
- **blade-agent attempt**：需本地 blade server（`octagon.yaml` blade.base_url，默认 `localhost:8020`）+ blade api_key。
- **LLM-as-Judge**：需要工具检查 Excel、PDF、PPTX 或来源文件的 Judge 必须创建独立 Blade Agent session；`llm_judge.model` 仅用于选择该 session 的模型，不能用裸 API provider 替代。
- **claude-code / codex attempt 走第三方 provider**：需 `OPENROUTER_API_KEY` 环境变量 + `octagon.yaml` 的 `model_providers`（or-cc / or-codex），base_url 各不同（CC 用 `/api`、Codex 用 `/api/v1`）。
- **看真实正文**：`octagon.wire_blob_api_enabled: true`（默认 False）。
- 改 `octagon.yaml` 后必须**重启后端**（app.state.settings 启动时加载）。

## 选型建议
- **做通信观测 / framework 对比干净样例** → **travel-planner**（零前置、有模型调用 + MCP、跑得快）。
- **要 Office 产物对比** → **ppt-visual-repair**（需上面三重依赖全配齐）。
- **外部专有 skill 类** → 先 `export SELECTED_SKILLS_DIR` + 装 cartopy。
- **代码题** → apple-incremental-game（本机可跑）；ad-placement 需 Linux 容器。

## synthetic-swe-multi-subgoal-evolution（Octagon 原创 SWE-Long 场景）

| env | 依赖 | 缺了会怎样 |
|---|---|---|
| **synthetic-swe-multi-subgoal-evolution** | Python 3.11+；**Node 20+**；评分时前端行为测试使用 `web/package-lock.json` 固定版本、由 `web/node_modules` 预装的只读依赖（react 18.3.1 / vitest 4.1.10 / vite 5.4.21 / @vitejs/plugin-react 4.7.0 / RTL 16.3.2 / jsdom 29.1.1） | Python/Node、`web/node_modules`、vite 或 plugin-react 缺失/版本漂移时评分抛 `ScorerUnavailableError` → `scoring_status=scorer_unavailable`、`score_total=NULL`，不产生候选业务低分；Vitest 启动失败同样记基础设施不可用。容器部署需在 evaluator 环境预装 Node 与前端依赖 |
