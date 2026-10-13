# 0.2.0 验证记录

开发环境：Windows 11（Python 3.12 + numpy 2.5.3）、WSL Ubuntu 26.04（Python 3.14.4，无 pip/numpy）。fnOS 真机 1.2.0800（x86）尚未安装本版。所有测试仅操作临时文件，不涉及真实视频清理。

## 已验证（2026-10-01）

- 单元测试 32 项（含 4 项 webui 鉴权/路由）：Windows 27 过 5 跳（Linux 专属删除路径）；WSL Linux 31 过 1 跳（无 numpy 的 pHash 合成帧测试，Windows 已覆盖）。覆盖：完全重复、同大小不同内容、参考目录保护、重叠目录、中文文件名、扫描后替换、缺失保留文件、越界选择、硬链接保护、配置及挂载变化、计划篡改、HTML 转义、审计导出、损坏视频降级、扫描取消、fpk-setup 授权校验、pHash 相似性、无 admin 头 403、网关前缀路由。
- Web 端到端冒烟（TCP 模式 + 网关前缀模式）：概览 → 扫描 → 进度轮询 → 审阅（分组渲染）→ 计划预演 → 确认页，全链路通过；所有链接保持在前缀内。
- fnpack 1.2.3 构建真实 `video-dedupe-0.2.0.fpk`（33KB 纯脚本；依赖安装期经 venv 拉取）：manifest/platform=all、生命周期脚本可执行位、app.tgz MD5 重算，外层 SHA256SUMS 见 dist。
- Python 源码 compileall 双平台通过。

## 尚未验证

- 真机安装链路：应用中心安装 fpk（0.2.2 起依赖全离线内置；前两次安装失败为 pip 源不可达与 static-ffmpeg 空壳 wheel 所致，已分别以静态二进制/解包 wheel 替代）、data-share 创建、**系统设置→访问权限授权目录与读写/只读派生**（单测已覆盖派生逻辑与 id 稳定性，真机 ACL 行为待验）。
- 统一网关：桌面 iframe 打开、X-Trim-Isadmin 头注入、app.sock 生命周期（start/stop/status 由 cmd/main 管理）。
- 真实视频的 pHash 聚类质量与扫描耗时（合成帧单测已验证哈希算法，真实片库误报率待测）。
- ffmpeg 在 NAS 上经 static-ffmpeg 的实际解码兼容性（旧片容器/编码）。
- 大库（~9400 文件）首次扫描时长与增量续跑、取消的实际体验。

## 真机验收顺序（阶段 6）

1. 合成样本级：本地 ffmpeg 生成测试集（字节相同 ×2 + 低分辨率转码 + 不同内容对照）上传 NAS 测试目录 → 安装 fpk → 仅授权该测试目录 → 完整流程扫描→审阅→预演→确认删除→核对 journal 与保留文件。
2. 保护级：配置变化后旧计划拒绝、未授权目录明确报错、增量重扫命中缓存。
3. 毕业考：授权真实媒体目录全量扫描 → 挑 1–2 个小的完全重复组执行一次真实删除，核对空间回收。

不要依据静态打包成功直接对唯一副本执行永久删除。真实部署与删除需用户逐步确认。

## 2026-10-13 · Review 26.10.12 重构工作区验证（基于 v0.2.27，未发布）

### 已验证（Windows 开发机）

- **全量单测**：`py -3.12 -X utf8 -m unittest discover -s tests -v` — **141 项全绿（7 项跳过，均为 Windows 平台性跳过的 Linux 删除安全路径）**，61.3s。覆盖本轮新增：扫描范围绑定与符号链接拒绝、统一 GET/HEAD 鉴权与 TCP 伪造身份头拒绝、CSRF/请求体上限/参数错误、任务表 claim/回收/interrupted、pair_match 同口径与 exacts 子组、能力分级降级与缓存版本、三阶段进度与取消检查点、导出独立与内容修复、审阅建议（不预选/受保护优先/字幕差异跳过）、分页与草稿冲突 409、多帧 preview_frames 参数与缓存、确认页清单、启动熔断门禁、/static 白名单与 manifest 版本源。
- **本地冒烟（双 TCP 实例）**：A=`VD_ALLOW_NO_ADMIN=1` 开发旁路（18099）、B=默认鉴权（18098），stdlib http.client 共 **38 项 HTTP 检查全过**——核心页面 /、/config、/progress、/review、/plan-review、/apply-status、/apply-result 全 200；默认模式下匿名访问业务页/缩略图/POST 全 403；伪造 `X-Trim-Isadmin` 头在 TCP 模式无效；CSRF 缺失、请求体超限、越界 scope/路径均被拒；/font 与 /static/app.css、/static/app.js 公开可达；全部链接保持网关前缀。检查后进程停止、端口释放确认。

### WSL Ubuntu-26.04 全量单测（首跑 2 失败，已修复复跑全绿）

- 环境：Python 3.14.4，无 numpy、无 ffmpeg；源码经 `/mnt/d` 复制到 `~/fnos-doc-check`（含 videodedupe.py、tasks.py、webui.py、static/、tests/、fpk/）。`python3 -X utf8 -m unittest discover -s tests -v` — **141 项：139 过 / 1 跳 / 2 失败**。
- 两处失败均为**本轮新增测试对干净 Linux 环境的前置依赖，非引擎逻辑缺陷**（已定位根因）：
  1. `test_start_aborts_when_setup_fails`：测试用 `write_text` 生成 mock 解释器但未设执行位——Git Bash 视一切文件可执行故 Windows 通过，真 Linux 下脚本的 `[ ! -x "$PY" ]` 先行退出（输出「缺少飞牛 Python 3.12 运行时。」而非预期的「配置派生失败」）。按 AGENTS.md 标准 WSL 同步命令（不含 fpk/）该项会自动 skip；本次因补齐 fpk/（manifest/字体路由用例需要）而真实执行暴露。
  2. `test_ffmpeg_args_serial_low_priority_shape`：测试桩替 `run_tool`，但 `thumbnail` 内部先经 `resolve_tools`（videodedupe.py:316-333）寻找 ffmpeg——WSL 无 ffmpeg 时抛 Unsafe 被 `preview_frames` 降级捕获（videodedupe.py:478），args_log=0。Windows 通过依赖开发机 PATH 中的 ffmpeg（D:\1\ffmpeg-7.1.1）。
- 处置（已闭环）：终局门禁的回归修复工程师当场修复两处——`test_start_aborts_when_setup_fails` 的 mock 解释器补 `chmod 0o755` 执行位并探测可处理 Windows 路径的 bash（`_bash_for_windows_paths`）；`test_ffmpeg_args_serial_low_priority_shape` 消除对宿主 PATH ffmpeg 的依赖（见 tests/test_videodedupe.py 对应用例）。终局复跑与主会话独立复核均为 **144 项全绿（跳过 2）**，双平台全绿提交门禁已满足。

### 尚未验证（真机，阶段 0 前置）

- **系统回收站真机验证（阶段 0）：待做**。按 Review 26.10.12 §四A 六项：fnOS 1.2.0800 第三方应用可用接口与授权方式；个人/共享目录的回收站行为；回收站关闭/不支持/权限不足时必须拒绝而非永久删除；文件进入哪个账号的回收站、能否按原名原位恢复；请求与回收站结果的可靠关联（超时/断连/重试）；接口能否绑定已核验文件身份。**通过条件：既满足系统可恢复性，也保留现有文件替换防护；通过前清理执行入口保持禁用（§五「在结果可恢复性得到验证之前，不开放清理功能」），无永久删除回退。**
- AF_UNIX 网关模式下的 X-Trim 头信任与真实网关转发行为（TCP 冒烟无法覆盖）。
- 真实媒体库（约一万视频）完整流程：discover 阶段观感、停止响应延迟、分页/草稿/五帧比较的实际耗时与体验。
- ffmpeg 真实失败场景（损坏容器/旧编码）下的能力分级降级表现；NAS 上 fpk 安装与静态资源随包分发。
