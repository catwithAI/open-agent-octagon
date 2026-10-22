# Skill Eval 配置与恢复

代码已接入主应用；仓库默认 `skill_eval.enabled: false`。本次没有修改用户 `octagon.yaml` 的执行开关，也没有部署服务。真实验收使用临时数据目录，未写旧场景、评分队列或生产数据。

## 安装与启动

```sh
uv sync --locked --extra dev --extra blade
# 需要本地 CLI 时使用仓库安装脚本；SDK 执行通道不回退 CLI。
./scripts/install-blade-cli.sh
npm --prefix web ci
npm --prefix web run build
```

主服务沿用仓库既有启动入口与数据目录。现有 `blade.base_url`/`blade.api_key` 从本地配置或既有环境配置载入；不要把 PAT 写进 spec、测试夹具、浏览器或命令参数。当前验证环境为用户指定的预览服务；测试模型为 `provider-fd5995eebe::deepseek-v4-1-flash-261017`。

需要开放新执行时，在部署配置中设置：

```yaml
skill_eval:
  enabled: true
  catalog_roots: []
  generation_timeout_seconds: 600
  max_active_runs: 2
  lease_seconds: 60
  recovery_poll_seconds: 5
  idle_session_retention_seconds: null
```

`catalog_roots` 可填写服务器已有 Skill 目录，根目录配置由操作者管理。来源只读，复制时验证指纹；不写回来源。未配置时仍可上传 SKILL.md/ZIP。完整包、输入、正文、事件和产物限额见 `octagon.yaml.example`，远端可能更严格。大于文本编辑上限的文件保留原字节，可下载和替换。

`enabled=false` 禁止创建/继续执行，仍可管理资产和查看历史；`octagon.display_only=true` 进一步阻止写请求、恢复器和后台执行。worker 启动失败或意外退出时执行 API 返回明确不可用，能力报告同时降为 unavailable。

## 运行与故障处理

- chat:end 仅结束一次生成。waiting_input/continue 不占生成额度，也不因用户离开页面自动结束。
- 停止先显示确认中，针对实际 session/chat_run_id；失败保留未确认状态，可手动重试。排队初次运行可取消，未发送后续消息可取消。
- 主动结束关闭输入，再确认远端静止、收集最终结果。回收失败显示 partial；“仅重试回收”不会再次调用模型。
- 重启根据持久 phase/回执恢复。已绑定会话不再创建；安全上传阶段可恢复；已接受 chat 先只读对账/订阅。无法确认接受情况时保留失败与证据，不能直接清空 job 后盲发。
- 会话失效时保留旧记录，用户在微调页手动确认输入后开启新运行。没有自动回答问题、自动重跑或自动删除会话。
- 最终结果补收产生新 collection generation，旧比较仍指向原先选择的结果清单。需要新结果时重新创建比较。

## 备份与清理

备份同一个 SQLite 数据库（含 WAL 一致性处理）及 `<data_path>/skill-eval`，避免仅备份数据库而丢字节。版本/消息/事件/结果索引在 `skill_eval_*` 表，blob/manifest 为不可变对象；预览是派生缓存。

首期不自动清除历史或空闲会话。显式调用 Store.collect_unreferenced 时设置延迟 cutoff，持有同一发布/GC 锁，检查版本、草稿、导入、输入、产物、collection 和比较引用；损坏引用会拒绝清理，不手工删除仍被引用的 hash 文件。

## 自包含验收入口

```sh
# 只读，不创建会话
uv run python scripts/verify-skill-eval-blade.py --preflight

# 显式真实模型调用，使用合成 Skill/物料；只清理自己创建的会话
uv run python scripts/verify-skill-eval-workflow.py --execute \
  --model provider-fd5995eebe::deepseek-v4-1-flash-261017
uv run python scripts/verify-skill-eval-workflow.py --execute --controls-only \
  --model provider-fd5995eebe::deepseek-v4-1-flash-261017 \
  --output /tmp/skill-eval-live-controls.json

# 离线浏览器演示，不读取 octagon.yaml/PAT
uv run python scripts/skill-eval-demo.py --port 18100
# 另一个终端，按需设置开发代理
OCTAGON_BACKEND_URL=http://127.0.0.1:18100 npm --prefix web run dev -- --port 15173
```

真实脚本不会由 CI/单测自动执行。报告有清理结果，若清理 unknown，仅核查报告列出的自建会话，不扫描或删除用户其他会话。远端保留期限、所有模型别名、所有文件格式及真实进程崩溃窗口未作穷举承诺。
