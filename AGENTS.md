# AGENTS.md — 视频查重助手（video-dedupe）项目宪法

面向 AI 代理与本仓库的所有开发者。**先读此文件，再动代码。**

## 项目是什么

飞牛 fnOS 原生 fpk 应用：扫描 NAS 上授权目录中的视频，识别**字节完全重复**（SHA-256）与**视觉相似**（ffmpeg 抽帧 pHash）的副本，经人工审阅生成签名清理计划；真实清理走**系统回收站执行器（待真机验证）**，验证通过前引擎层封死真实删除（`videodedupe.require_cleanup_available` 唯一门禁，见 docs/Review_26.10.12.md §四A）。v0.1.0 曾基于 VDF Docker 容器，v0.2.0 起为纯原生实现（无容器）。

- 目标 NAS：内网 fnOS 1.2.0800 Mainland-PE（x86），经 trim-cli（fnos 技能）访问；Git Bash 下调用必须 `MSYS_NO_PATHCONV=1`。具体地址与个人目录习惯见 `LOCAL.md`（gitignore，不入库）

## 技术栈（不要引入栈外依赖）

| 层 | 选型 | 约束 |
| --- | --- | --- |
| 核心引擎 | Python 3.12 **标准库**（sqlite3/json/http 等） | 除 numpy（DCT）外零第三方 import |
| 视觉指纹 | ffmpeg/ffprobe（包内 `app/bin/` 静态二进制，GPL）+ numpy（包内 `app/site-packages/`，解包 wheel）DCT-II 64bit pHash | 单路串行、`-threads 1`、nice 降优先级；**安装期无 pip/venv/网络** |
| Web UI | stdlib http.server，AF_UNIX `app.sock`（统一网关），无前端框架 | Windows 开发用 `--tcp` 回退 |
| 打包 | 官方 fnpack 1.2.3（.tools/fnpack.exe） | manifest `platform=x86`（bin/ 与 site-packages 为 linux-x64 二进制）；构建期缓存于 .tools/ |
| 测试 | unittest（无 pytest） | Windows + WSL Ubuntu 双平台全绿才可提交 |

## 架构地图：按功能改对应模块，别的不碰

| 要改什么 | 只动哪里 | 说明 |
| --- | --- | --- |
| 扫描/指纹/分组/缓存 | `videodedupe.py` 的 `inventory`、`fingerprint_cached`、`visual_groups`、`phash` | 增量缓存键 = (path, stat)；损坏视频降级不中止 |
| 审阅/计划逻辑 | `videodedupe.py` 的 `plan`、`selection`、`export_audit`、`write_review` | 输入是选择列表；计划文档结构不可随意变 |
| **删除安全** | `videodedupe.py` 的 `apply`、`anchored`、`fingerprint`、`snapshot` | ⚠️ 安全关键。改动需 28 项测试全绿 + 双平台验证，宁可不改 |
| 页面/交互/网关路由 | `webui.py` | 所有链接必须带 `self.px` 前缀；鉴权在 `is_admin` |
| 任务状态/互斥 | `tasks.py`（扫描/计划/删除入口经 `tasks.run`/`claim` 接入；锁在 `tasks.oplock`） | 单表 tasks 复用 inventory.sqlite3；跨进程操作锁 Web/CLI 共用；重启核对 `reconcile` 只核实不重放 |
| 安装/配置钩子 | `videodedupe.py` 的 `fpk_setup`、`derive_roots` | 目录配置从系统"访问权限"授权派生（读写=cleanup，只读=reference，id=路径哈希）；启动时刷新 |
| 应用生命周期 | `fpk/cmd/*` | main=守护进程契约（0/1/3 退出码）；install/config/upgrade/uninstall 幂等 |
| 应用骨架/依赖声明 | `fpk/manifest`、`fpk/config/*`、`fpk/wizard/*`、`fpk/app/ui/config`、`fpk/app/requirements.txt` | 网关字段：gatewayPrefix=/app/video-dedupe、gatewaySocket=app.sock（实际位于 $TRIM_APPDEST） |
| 打包流程 | `build_fpk.py` | 打包清单、权限规范化、校验和 |
| 测试 | `tests/test_videodedupe.py` | 安全路径用例不删不加 skip |
| 文档 | `README.md`、`VALIDATION.md`、`THIRD_PARTY.md`、本文件 | 验证记录如实写 |

## 常用命令

```sh
# 测试（Windows 开发机）
py -3.12 -X utf8 -m unittest discover -s tests -v
# Linux 全量（含删除安全路径；WSL Ubuntu-26.04）
# fpk/manifest 必须同步：VERSION 与 test_version_display 都以它为唯一来源，缺文件时版本回退 0.0.0
wsl.exe -d Ubuntu-26.04 -- bash -lc 'cp -r ~/… videodedupe.py tasks.py webui.py static tests ~/fnos-check/ && mkdir -p ~/fnos-check/fpk && cp fpk/manifest ~/fnos-check/fpk/ && cd ~/fnos-check && python3 -X utf8 -m unittest discover -s tests -v'
# 本地 Web 冒烟
py -3.12 -X utf8 webui.py --config config.local.json --tcp 18099   # 需 VD_ALLOW_NO_ADMIN=1
# 构建 fpk（输出 dist/video-dedupe-<版本>.fpk + SHA256SUMS + 源码归档；版本以 fpk/manifest 为唯一来源）
py -3.12 -X utf8 build_fpk.py
```

## 安全不变量（任何改动不得破坏）

> 当前封口状态（Review 26.10.12 §四A）：真实清理在引擎层封死——`require_cleanup_available` 无条件拒绝 `execute=True`，放行条件只有一个：经真机验证的系统回收站执行器（新执行器，不是旧 unlink 路径）。下列不变量描述清理机制本身，回收站执行器接入后同样必须全部成立。

1. **默认预演**：`apply` 不带 `--execute --confirm <plan_id>` 绝不删除；confirm 必须与计划 sha256 完全一致。
2. **每组至少保留一份**：全选被拒；reference 角色目录与硬链接（nlink>1）永不可删。
3. **计划即合同**：plan 文档由内容哈希签名；config identity、挂载点 (dev,ino)、文件 stat、SHA-256 任一不符即拒绝执行。
4. **原子暂存**：Linux 删除先 rename 到同目录 `.vd-delete-<rand>.<ext>` 并复核 inode/哈希，再 unlink；中断后人工恢复，绝不自动清理暂存物。
5. **journal 先行**：每步删除前写 journal；已删除项幂等跳过；原路径新文件不受旧计划影响。
6. **越界即拒**：符号链接路径、`..`、非授权根、非视频扩展名、扫描后变更的文件一律拒绝。
7. **只删 Linux**：Windows 开发机永不执行真实删除。
8. 审计导出只进 export_dir（共享目录）；缩略图与数据库只在 state_dir（应用私有）。

## fnOS 事实速查（踩过的坑）

- `platform` 取值 `x86/arm/all`（不是 amd64/arm64）；网关应用不写 `service_port`；统一网关要求 `os_min_version=1.1.3100`+。
- 运行时路径：`/var/apps/python312/target/bin/python3`；venv 放 `$TRIM_PKGVAR/venv`（升级保留）。
- 关键环境变量：`TRIM_APPDEST/TRIM_PKGETC/TRIM_PKGVAR`、`TRIM_DATA_SHARE_PATHS`（共享目录）、`TRIM_DATA_ACCESSIBLE_PATHS`（**授权目录——唯一的目录配置来源**，冒号分隔，勿手动向导传目录）。
- 目录配置哲学：用户在 系统设置→应用→访问权限 用 GUI 添加文件夹（读写/只读），应用启动时 `fpk-setup` 派生 config.json。**永远不要让用户手写目录 JSON**；`wizard_*` 只承载 pip 源等运维参数。
- `cmd/main` 契约：start 幂等、stop TERM→10s→KILL、status 运行=0/未运行=3/失败=1；错误写 `$TRIM_TEMP_LOGFILE`。
- 网关转发请求带 `X-Trim-Isadmin`/`X-Trim-Userid` 头；socket 实际路径 `${TRIM_APPDEST}/app.sock`。
- 媒体访问靠应用设置中的授权目录（ACL 授予 package 用户），应用无默认文件权限。

## code-review-graph 工作流（硬规则）

本仓库已配置 CRG（MCP 工具 `mcp__code-review-graph__*`，本机 uv tool 安装 v2.3.9；图数据库在 `.code-review-graph/`，已 gitignore）。**不满足以下规则的会话不算完成：**

1. **改动前**：`get_minimal_context_tool(repo_root="D:/Official/fnos_Dev", task="<本次任务>")` 定位影响面；需要找符号用 `semantic_search_nodes_tool` / `query_graph_tool`，不要全库通读。
2. **改动后**：`build_or_update_graph_tool(repo_root="D:/Official/fnos_Dev")` 增量更新（秒级），并 `list_graph_stats_tool` 确认无错误。
3. **提交前评审**：`detect_changes_tool` + `get_review_context_tool` 查看变更风险与影响半径。
4. 所有调用显式传 `repo_root`（正向斜杠）——ZCode 的 MCP 配置 cwd 指向别的项目。
5. `.code-review-graphignore` 排除 `.tools/ dist/ .zcode/ *.fpk` 及图标；新增生成物目录时同步维护。

## 开发纪律

- **ponytail 原则**：标准库优先、删除优于新增、不造单实现抽象；已知天花板用 `# ponytail:` 注释标注（如 visual_groups 的 O(k²) 桶内配对）。
- **每次修改必须登记 `Log.md`**：日期、版本/commit、改了什么、为什么（真机反馈的问题尤其要记录症状与根因）。提交前登记，条目按时间正序追加。
- 每次开发只改「架构地图」中对应模块；跨模块需求拆成多次提交。
- 提交前：双平台测试全绿 + compileall + （涉及应用骨架时）fnpack 构建成功。
- 涉及 NAS 真机的验证步骤与结论写 `VALIDATION.md`；失败即停，不掩盖。
- 许可：本仓库 MIT；`static-ffmpeg` 分发 GPL 二进制，THIRD_PARTY.md 必须保留声明与源码指引；VDF(AGPL) 仅作算法参考，不得复制其代码。
