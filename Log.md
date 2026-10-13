# 开发日志（Log.md）

> 每次代码修改必须在本文登记：日期、版本/commit、修改内容、原因（尤其是真机反馈的问题）。
> 条目按时间正序追加；版本号与 `fpk/manifest` 保持一致。

## 2026-10-01 · v0.1.0 → v0.2.0（Docker 方向 → 飞牛原生 fpk）

- `1c7bcb4` 转型前基线：Docker 方向 v0.1.0 最后快照（VDF 容器 + 辅助脚本）。
- `62df663` 固定脚本 LF 行尾（`.gitattributes`）：CRLF 会破坏 NAS 上的 bash 生命周期脚本。
- `3311a06` 核心引擎重写：SHA-256 + ffmpeg 5帧 32×32 DCT pHash、(path,stat) 增量缓存、选择式 plan（替代 VDF CSV 输入）、`fpk-setup` 安装钩子；apply 安全核心未动。
- `2a6f3f7` 新增 `webui.py`：AF_UNIX socket HTTP 服务（统一网关），TCP 回退供 Windows 开发；X-Trim-Isadmin 鉴权。
- `58b138a` fpk 骨架重写：manifest platform + os_min_version=1.1.3100、统一网关（gatewayPrefix=/app/video-dedupe + app.sock）、cmd/ 九脚本（守护进程契约）、测试 32 项、AGENTS.md 架构宪法；删除 Dockerfile/deploy.py/compose 等废弃产物。

## 2026-10-01 · v0.2.0 → v0.2.4（真机部署准备）

- `7b9dbef` 修复聚类缺陷：原先 exact 组成员被排除出视觉聚类，导致"原片+副本"抱团后转码版永远孤立。改为统一 union-find（SHA 硬链 + pHash 软链），混合簇标 visual。真视频验证：原片/副本/转码版聚为一组，不同内容对照独立。
- `fdd1710` **目录配置改为从系统"访问权限"授权派生**（用户反馈：向导手填目录 JSON 不可用且与飞牛官方授权机制重复）：读写授权=cleanup、只读授权=reference、id=路径 md5；向导删目录步骤；空授权显示引导页。
- `a7de2dd` **依赖全离线**（真机安装失败：pip 拉清华源失败）：构建期缓存 johnvansickle 静态 ffmpeg/ffprobe 打进包，numpy 用 Linux wheel 随包；pip 源降级为回退。
- `bc9cdaf` **彻底移除 pip/venv**（发现 static-ffmpeg wheel 是 7.9KB 空壳，运行时才下载二进制）：numpy wheel 解包进 app/site-packages（videodedupe 顶部 sys.path 注入）；install/config/upgrade/main 去掉全部 pip 逻辑；platform=all→x86。

## 2026-10-01 · v0.2.4 → v0.2.12（真机安装与白屏排查）

- `1fcb00c` 修复 0.2.2"原因未知"安装失败：重写 install_callback 时丢失 `mkdir $TRIM_PKGVAR`，numpy 自检的 stderr 重定向随 set -e 无文案退出；恢复 mkdir，所有失败路径先写中文文案并落 install.log。
- `f8f09d7` **WSL fpk 运行时模拟**（uv python3.12 + 包内二进制 + TRIM_* 变量模拟平台）端到端复现真机问题，修三个真 bug：① fpk-setup 被 argparse 要求 --config（安装失败主因）；② webui 空 roots 启动 fail-fast 崩溃，引导页无机会显示；③ AF_UNIX 无 client_address，address_string IndexError。
- `6d2b6f9` HTTP/1.1 + HEAD 支持：统一网关是 1.1 反代，1.0 短连接有 keep-alive 兼容风险；HEAD 此前 501。
- `1b976bf` **白屏根因**：viewport meta `content="...` 引号未闭合，浏览器把整个文档吞进属性。补引号 + 测试加引号配对断言。
- `19bd035` UI 全面翻新（按用户反馈"毛坯房"）：GameLibrary（Steam 风）设计令牌、侧栏导航、卡片/徽章/状态点；修复扫描期读请求抢排它锁导致的 `Errno 11`（state 增 exclusive=False 读路径）；配置页人话化。
- `2954c92` 扫描进度改 AJAX 静默轮询（原 4 秒整页 reload 白屏闪烁）；扫描期间 stat 变化的文件降级为剔除（原整批作废）。
- `7f0e198` 深/浅色主题切换（localStorage 记忆）；审阅页改为相册式卡片流（大预览图、点卡片勾选、组级"保留最小版本，勾选其余"）；"审计报告"从主流程文案移除（降级为"删除记录存档"细节）；扫描完成态直达审阅。
- `05a2455` 修复扫描中止于 `Errno 13 Permission denied`（用户授权整个 /vol7，扫描撞上应用账号无权读取的系统目录）：不可读目录/文件降级为跳过并记警告，越界/符号链接仍拒绝；scan_worker 异常 traceback 落 webui.log。
- `de2bb5d` 审阅独立为侧栏导航项（自动选最近扫描）；左下角改显示版本号；"配置"更名"设置"；错误页与设置页内嵌运行日志尾部（免去 SSH）；高级 JSON 收进二级折叠。
- `e462040` 可观测性：异常先落 webui.log 再发错误页（原 BrokenPipe 刷屏盖住真实异常）；客户端断管静默；ffmpeg 版本探测缓存。
- `e462040`→`0.2.13` 开发日志机制：新建 `Log.md`（本文），AGENTS.md 开发纪律加入"每次修改必须登记"；内容无代码变化，随包重发。
## 2026-10-01 · v0.2.13 → v0.2.14

- 修复扫描中途 `PermissionError: [Errno 13] Permission denied: 'vol7'`（用户 traceback 定位）：飞牛 ACL 对祖先卷（/vol7）只授予 traverse(x) 不授予 list(r)，`anchored` 从 `/` 起用 O_RDONLY|O_DIRECTORY 打开各级目录需要读权限而失败。改为从授权根起、中间目录 O_PATH|O_NOFOLLOW（仅路径引用、无需读权限）；NOFOLLOW、inode/哈希复核等安全语义不变。新增 traverse-only 祖先回归测试（36 项双平台绿）；WSL 模拟真删链路复验通过。版本 0.2.14。

## 2026-10-01 · v0.2.14 → v0.2.15

- 扫描进度增加「估计剩余时间」徽章（按已用时间/完成比例推算，前 2% 不显示）。
- 删除「扫描在后台进行，您可以关闭本页面…」文案；扫描范围标题去掉「（N 个文件夹）」计数。
- 新增子文件夹选择扫描：概览页每个授权根可展开目录树（懒加载逐级浏览、勾选），「开始/重新扫描」时只扫勾选的子目录（不勾选=全部）。核心 inventory 支持 scan_subdirs；新增 /browse 目录列举 API（仅目录名，过滤隐藏/符号链接/排除规则）。
- 端到端验证：browse API、scoped 扫描（total=1）、ETA 徽章、全删链路复归。版本 0.2.15。

## 2026-10-02 · v0.2.15 → v0.2.16

- 修复真机"生成清理计划"时 `Errno 11`：plan 对大库逐文件做 SHA+ffprobe 复核需数十分钟，却全程持有排它锁，用户重复点击即撞锁。三层修复：① plan 全程改为无锁读（并发 plan 无害，结果签名互不干扰）；② apply 仅在真正执行删除时持排它锁，预演(dry_run)无锁（同为长耗时只读校验）；③ webui 将计划生成改为后台线程 + `/plan-status` 轮询 + `/plan-review` 结果页，plan 复核进度实时写入 progress（前端进度条），超时/误点不再阻塞。
- WSL 全链路验证：POST /plan 立即返回生成中页 → 后台完成 → plan-review 200 → apply 真删成功。版本 0.2.16。

## 2026-10-02 · v0.2.16 → v0.2.17

- `/plan-status` 接口对浏览器直接访问改为渲染状态页（进度条 + 完成自动跳转计划确认页），仅带 `Accept: application/json` 的前端轮询返回 JSON。此前用户在计划生成期间直接打开该接口会看到裸 JSON，误以为出错（实际 running=true/planning 1/99 是正常生成中状态）。版本 0.2.17。

## 2026-10-02 · v0.2.17 → v0.2.18

- 计划生成状态提升为全局一等状态：概览页在生成期间显示专属进度卡（进度条/已核对数/当前文件，轮询自动跳转）；审阅页与设置页顶部挂横幅提示（含进度与状态页链接）；生成期间拒绝开始扫描（503 引导至状态页）。此前生成状态只存在于提交后的临时页面，用户刷新/离开后无处可见（用户反馈："UI 这么显示谁知道它在运行？"）。
- 修复审阅页 banner 变量未定义（上轮补横幅时遗漏赋值）。
- WSL 验证：生成期间概览/审阅/设置/plan-status 四处状态可见，扫描互斥 503。版本 0.2.18。

## 2026-10-02 · v0.2.18 → v0.2.19

- 计划生成结果持久化（progress.json phase=plan_ready/plan_error）：应用重启后状态页显示"计划已生成，等待确认"并可直达 plan-review 执行删除（真机复现：用户重启应用后状态页只显示"已结束 99/99"成为死胡同——内存结果丢失而核对其实已完成）。
- plan-review 的 plan_id 可从磁盘恢复，恢复路径不重放预演明细（执行时仍做完整校验）。
- 中断分支明确化：phase=planning 且进程内无运行任务时，状态页与 plan-review 显示"上次生成被中断（应用重启），请回审阅页重新生成（扫描结果与指纹缓存不受影响）"。
- 确定性测试三分支（中断/plan_ready 恢复/正常）全通过；WSL 重启恢复全链路（重启后确认删除真执行）通过。版本 0.2.19。

## 2026-10-02 · v0.2.19 → v0.2.20

- **计划生成提速两个数量级**（用户质疑：核对读哈希不就好了？）：plan 复核原实现对每文件全量重读算 SHA + 重新跑 ffprobe，与扫描期完全重复。改为：SHA 走 (path,stat) 增量缓存（stat 未变即命中，内容被替换会因 stat 变化被检出拒绝）；元数据用扫描缓存 + 仅重查外挂字幕（廉价）。执行删除前 apply 仍做全量 SHA 终检（安全兜底不变）。实测 16MB 文件对 plan 从全量重读降为 0.02s。
- 审阅页默认按建议预勾选（每组保留最小的，其余为删除候选，红底不可删项不勾）；操作条加全局「全部按建议勾选」「全部取消勾选」批量按钮。
- 版本 0.2.20。

## 2026-10-02 · v0.2.20 → v0.2.21

- 修复 plan-status 状态页不自动刷新：0.2.17 重构时轮询脚本丢失，页面渲染一次即静止（用户看到进度停在 2/99 不动），且进度条未设初始宽度。补回轮询（更新数字/进度条；完成后自动跳确认页，失败/中断自动重渲染对应分支）。
- 关于用户提议"删除核对步骤"：0.2.20 已把核对改为缓存查询（秒级），核对保留其安全价值（提前发现文件变化）；执行删除阶段的全量终检为必要安全成本，不可省。版本 0.2.21。

## 2026-10-02 · v0.2.21 → v0.2.22

- **修复 0.2.20 引入的安全回归**（测试网拦截）：apply 预演的缓存键误用计划时的 stat 而非文件当前 stat——同大小替换并恢复 mtime 的文件可命中旧缓存绕过校验（Windows 因 ctime=创建时间无法检出该场景，Linux ctime 可检出）。修复：fingerprint_cached 改用当前实际 stat 作缓存键，并与计划快照不一致时直接拒绝。测试新增 dry-run 缓存断言（预演不全量重读、执行全量终检必须拒绝哈希不符），replaced_same_size 测试按平台语义拆分。
- 用户报告"生成清理计划还是很慢"的根因即此：0.2.20 只提速了 plan，plan_worker 里的 apply 预演仍在全量重读。两处都走缓存后整体秒级。
- 36→37 项双平台绿；WSL 真删链路复归通过。版本 0.2.22。

## 2026-10-02 · v0.2.22 → v0.2.23

- **删除执行后台化**（用户问"确认删除后怎么知道删没删成功"——原实现同步 POST，期间无进度、刷新/超时即失联）：apply 移除循环上报 applying 进度（done/total/当前文件/已释放字节）；webui 的确认删除改为后台线程 + "正在执行删除"页（进度条+已释放 MB 实时更新）+ /apply-status 状态页（JSON/人话页协商）+ /apply-result 结果页（删除数、释放量、前后剩余空间、删除日志路径）。失败时明确提示"已完成部分不会重复删除，可对同一计划重新执行（自动跳过已删项）"。执行期间扫描/计划/重复删除互斥。
- WSL 全链路：POST /apply 200 → 执行中页 → apply_done 持久化 → 结果页"清理完成" → 文件真实删除。版本 0.2.23。

## 2026-10-02 · v0.2.23 → v0.2.24

- 删除阶段校验减半（用户确认取舍：预检缓存化、保留终读）：apply 执行路径的整批预检从全量重读改为 (path,stat) 缓存查询（stat 未变即命中，替换被检出拒绝）；保留「改名锁定后读一遍内容再 unlink」的终读——不可逆删除前唯一一次内容级验证。每删除文件的全量读从两次降为一次。
- 37 项双平台绿（含 dry-run 缓存断言、执行终检拒绝哈希不符、traverse-only 祖先回归）；WSL 真删链路复归通过。版本 0.2.24。

## 2026-10-02 · v0.2.24 → v0.2.25

- 修复删除进行中概览页无任何显示（用户愤怒反馈：点出去回来必须重新触发扫描撞上互斥才能看到进度）：概览接入三种任务态统一调度——删除中显示"正在执行删除"进度卡（done/total/已释放 MB/详情链接，轮询自动更新），与扫描/计划卡同级。
- 删除进度改全量口径：total=计划全部删除项（含此前幂等跳过的已删项），done 从 journal 已删数起算，freed 从 journal 已删字节起算——续跑计划不再显示误导性的 1/1。
- WSL 验证：删除中概览卡、全量口径进度、apply-result 结果页。版本 0.2.25。

## 2026-10-02 · v0.2.25 → v0.2.26

- **Web UI 精修重构**（用户要求前端美化；方向经确认：保留已认可的深色 Steam 身份，只提升执行层）：
  - 设计令牌扩充：--ease/--dur/--radius-sm/暗色阴影；暗 --muted 提亮至 #9aa8b3、浅 --elevated 改 #f3f7fb（修复浅色卡上悬停行/提示不可见）、补缺失的 .d-warn；字体栈 system-ui 系（全离线无 webfont）；数字表格 tabular-nums。
  - 内联 SVG 线条图标全量替换 emoji（🏠🔍⚙️🌗等 13 枚，viewBox 24/stroke 1.8/currentColor 全单引号守住引号配对测试）；主题按钮 sun/moon 纯 CSS 随 html.light 切换。
  - 交互完备性：全局 :focus-visible 焦点环；审阅 tile 改 role=checkbox + aria-checked/aria-disabled/tabindex + Enter/Space 键盘操作；.sel 加 ✓ 圆形角标；运行态进度条流光（gated prefers-reduced-motion）；吸底操作条毛玻璃兜底；首套响应式 @media 880px（侧栏变顶栏/单列/tile 网格收窄）。
  - **6 处近似重复的轮询 JS 合并为全局 vdPoll(url,tick,done)**（直击 0.2.17/0.2.21 两次重构丢轮询事故类别），并新增回归断言：test_status_pages_keep_polling（plan-status/apply-status 运行态必须含 vdPoll）+ test_ui_baseline_guards（focus-visible/reduced-motion/@media 在页）。轮询地址、间隔、reload 收尾、JSON/人话协商行为不变。
  - 顺手修：server_version 从硬编码 0.2.12 改随 v.VERSION；videodedupe VERSION 常量 0.2.1→0.2.26 对齐 manifest（侧栏"版本 v0.2.1"暴露的长期漂移）；build_fpk.py 产物名版本改从 fpk/manifest 派生（终结三处漂移）。
- 安全不变量零触碰：删除双确认结构、保护 tile 语义、安全文案逐字未动；39 项双平台全绿（Windows 6 跳过/WSL 1 跳过，均平台性跳过）；fnpack 构建通过（0.2.26）；本地 --tcp 冒烟：扫描→审阅（1 组 4 文件含保护/预勾选）→计划生成→计划确认全链路 + 暗浅双主题 + 375px 移动档截图自查通过。版本 0.2.26。

## 2026-10-08 · v0.2.26 → v0.2.27

- **Web UI 全面对齐 Strata 设计语言**（用户指定参照 github.com/Niko1221/Strata；四项方向已确认：Strata 原味浅色默认+深色切换 / 顶栏+居中单列 / 换皮+概览仪表盘化 / 打包 Outfit 字体；Steam 深蓝身份退役）：
  - 设计令牌照抄 Strata 仓库 serve/web/tokens.css 实测值：浅色默认 `:root`（#f9fafb 背景/白卡/翡翠绿 #10b981）+ `html.dark`（#0e1113/#161a1d/#34d399）；半径 8/14/24/pill、cubic-bezier(.2,.7,.2,1) 160ms；code 块双主题恒深色（Strata 特征）。默认浅色迁移：仅 `localStorage.vdTheme==='dark'` 才加 `.dark`（存过 'light' 的老用户自然落浅色）。
  - 布局侧栏→顶栏+居中单列：sticky 毛玻璃顶栏、tab 胶囊分段控件（概览/审阅/设置）；@media 880px 顶栏换行、tab 横向滚动、指标卡 2 列。legacy CSS 类名全保留（handler 标记零改动，换皮集中在 PAGE）。
  - 概览仪表盘化：阶段 pill 链（扫描→生成计划→确认删除→清理完成，done/act/err/next 四态映射各 phase）+ 四指标大数字卡（重复组/删除候选/可释放空间/视频总数）。指标在 webui 层用 `v.state + v.load_scan + v.load_groups` 现算，候选口径与审阅页预勾选完全一致（每组非最小、非 reference 根、nlink≤1）；体积 KB/MB/GB 三档格式化。
  - 全局状态胶囊：所有页面顶栏常驻轮询 /progress，phase→文案/点色映射，点击回概览。自适应间隔（运行 2.5s/空闲 10s/隐藏 15s/失败 5s）+ visibilitychange 立即唤醒 + 单 timer clearTimeout 防叠加——修复首版"隐藏 tab 下永不 fetch"缺陷。既有 6 处页面级 vdPoll 不动。
  - Outfit 可变字体自托管：fpk/app/fonts/ 两个 woff2（latin/latin-ext，取自 Strata 仓库）+ OFL.txt；新增 /font 路由（font/woff2 + immutable 7 天缓存，缺文件 404 降级不崩）；@font-face unicode-range 分块、`font-display:swap`，字标/大数字/标题生效，中文回退系统栈。THIRD_PARTY.md 登记 SIL OFL 1.1。
  - 引擎仅 VERSION 常量改 0.2.27；删除双确认、保护 tile 语义、安全文案逐字未动。
- 新增测试 test_strata_theme_and_shell + test_font_route；41 项双平台全绿（跳过项不变）；fnpack 构建通过（0.2.27，74.7MB 含字体）；本地 --tcp 冒烟全链路（扫描 5 文件→指标卡 1 组/2 候选/63KB/5→审阅预勾选→计划生成→确认页）+ 浅/深双主题 + 1280/375 两档共 7 次截图自查通过。版本 0.2.27。

## 2026-10-08 · 公开仓库整理（GitHub 发布）

- 参照用户自己的 GameLibrary 仓库整理公开架构：README.md 重写（一句话定位/下载表/看看界面/三步开始/平时怎么用/数据与常见问题/当前版本/开发与项目结构/参与和许可）+ README.en.md 英文镜像 + `assets/readme/` 四张界面截图（本地 --tcp 冒烟环境实测截取并逐张视觉核验：浅色概览/审阅/计划确认 + 深色概览）+ `docs/releases/v0.2.27.md` 发布文本（格式参照 GameLibrary v1.7.5：# 版本 → ## 简体中文 → 状态声明 → 主要变化与修复/升级与数据保留/下载表/验证与限制 → --- → ## English 全镜像）。
- 公开前脱敏：AGENTS.md 的 NAS 地址与个人目录描述移入 gitignore 的 `LOCAL.md`（本地笔记不入库）；config.example.json 示例路径与 VALIDATION.md 验收计划改中性表述；全库 grep 复核无 IP/敏感词残留。
- 冒烟截图补造数据：根路径改 `D:/媒体库/{电影,参考}` 体面化后全新首扫→审阅→计划链路复跑（期间发现 IAB 无头环境点击表单 onsubmit 的 confirm 会阻塞定位器，用 form.submit() 规范外提交绕开；属测试环境问题，非产品缺陷）。
- 公开仓库 `sumingwang233/fnos-video-dedupe` 采用 **fresh history**：本地 main 保留完整私有历史不外推，远端以脱敏快照孤儿分支 `public` 推送为 main（`git push public public:main`，禁推本地 main，规则记录于 LOCAL.md）；发布 v0.2.27 附 fpk/源码归档/SHA256SUMS。

## 2026-10-13 · v0.2.27 工作区改动（Review 26.10.12 分阶段重构，未发布）

> 依据 `docs/Review_26.10.12.md` 审查报告逐阶段改造；manifest 仍为 0.2.27，以下全部为工作区改动、未提交发布。清理执行入口在本轮保持禁用（回收站真机验证前）。

- **F1 扫描范围绑定根目录**（P1「裸相对路径应用到每个根」/§四C）：`videodedupe.py` 新增 `scan_scope()`——拒绝未知根/绝对路径/`..`/反斜杠/符号链接（含中间链），同一根下父子选择去重；`inventory` 按根身份取起点，实际生效范围写入 scans 表新列 `scope`（旧库幂等 ALTER，NULL=全部授权目录，`load_scope` 读取）。webui 目录树复选框提交值改 root:rel、选择摘要展示完整位置与权限角色、`post_scan` 在占扫描锁前完成服务端校验（非法范围 400 不启动）。
- **F2 表单提交取消修复**（P1「async 处理函数返回 Promise 不能作同步取消布尔」）：消除 webui 全部 4 处依赖返回值的内联 onsubmit——两个扫描表单改 data-scan-scope 标记 + `addEventListener('submit')`（`startScopedScan` 去 async 返回真布尔，取消即 `preventDefault()`）；取消扫描与执行删除确认表单改 data-confirm 声明式属性，由页面外壳共享脚本委托式 submit 守卫统一取消。
- **F3 读取鉴权与请求边界**（P1「GET 路由未统一检查管理员」/§四E）：do_GET/do_HEAD 统一鉴权门——白名单 `PUBLIC_GET_PATHS` 仅公开字体与样式表，业务页面/JSON//browse//thumb 匿名一律 403（403 页不再向陌生人展示日志尾部）；do_POST 的 is_admin 检查重构为 `_post_guard`（管理员→请求体上限→CSRF），`page()` 正则统一注入 CSRF 令牌；超限 413、非法 Content-Length/非 UTF-8 表单 400。TCP 开发模式不因客户端伪造身份头获得管理员；缩略图读取绑定扫描归属与授权。
- **F4 结果页数据契约修复**（P1「结果页把空间统计当数字计算」）：重写 `get_apply_result`——正确遍历引擎 `free_space` 返回的 {root_id: 剩余字节} 字典按授权目录逐行展示，根 id 经 cfg 映射路径（缺失如实标「已移除的目录」），合计行按 `st_dev` 文件系统去重（同盘多根只计一次）；修复旧代码 dict/2**30 的 TypeError；空结果如实分流为「删除正在执行中(503)/上次删除无结果」。
- **F5 启动配置失败熔断**（P1「启动时忽略 fpk-setup 失败」/§四F）：`fpk/cmd/main` 的 start_app 中 fpk-setup 失败即写明中文原因并 `exit 1`，不再 `|| true` 沿用过期授权边界启动；空授权（roots=[] 正常引导态）不算失败照常启动。新增 FpkMainStartGateTests：mock 解释器注入脚本副本，单测启动门禁本身（成功路径受 Windows 无 AF_UNIX 限制未集成测试，`# ponytail:` 已注明）。
- **T1 任务状态模块**（P1「忙碌判断不一致/progress.json 重启假运行」/§四B）：新增 `tasks.py`——复用 inventory.sqlite3 单张 tasks 表 + 自 `state()` 原样提取的跨进程 operation.lock；`tasks.claim` 在独占操作锁内原子完成「查活跃任务+落 running 行」（配置与参数验证完成后才占用）；超过 120s 心跳宽限且锁空闲的陈旧行自动判 interrupted 回收；inventory/plan/apply 经 `tasks.run` 接入，重启 `reconcile` 只核实不重放。`build_fpk.py` 清单与 AGENTS.md 架构地图收录新模块。
- **T2 请求行为统一**（P1「计划提交后页面 location.reload」/§四B 请求行为表）：scan/cancel/plan/apply 四个 POST 成功统一 303 跳转到带任务 ID 的 GET 页（新增 `redirect()` 应答，空体无脚本）；plan-status/plan-review/apply-status/apply-result 支持 `?task=` 固定读取指定任务行（不存在→404、成功但结果缺失→如实说明、缺省回落最近一条兼容旧链接）；移除 GET /apply 触发核验的行为。
- **S1 匹配规则统一与子组保留**（P0「连通组 A≈B≈C 不证明 A≈C」+ P1「max/min 容差不一致」/§四C）：抽出扫描/计划共用纯函数 `pair_match`（时长窗口 max(5.0, 较短×0.02)、pHash 均值距离 ≤10，保持原值不调参），消除计划侧 min 容差导致的「扫描展示、提交被拒」；分组入库新增 exacts 列保存字节一致子组（旧库 NULL 按 sha 现场重算 + ALTER 迁移），连通组降级为候选集合；plan 为每个待清理文件选定证据最强的直接匹配保留对象并写入计划文档。
- **S2 能力分级降级与缓存版本**（P1「元数据/指纹/缩略图共用一次异常处理」/§四C）：`fingerprint_cached` 拆为元数据/视觉指纹/缩略图三个独立能力，各自记录成功与 *_error 失败状态，缩略图或抽帧失败不再覆盖已读到的元数据；失败状态不写 hashes 持久缓存（重扫按未命中重试失败能力），成功且版本一致的命中继续复用；缓存键加入 `HASH_CACHE_VERSION`；单文件消失/变化降级为记录警告并从本轮剔除（lstat 失败不再整轮崩溃），授权边界/配置身份/挂载异常仍立即停止。
- **S3 扫描进度与停止响应**（P2「遍历阶段无进度难取消」/§三.2）：`_inventory` 呈现「查找视频/识别内容/整理结果」三阶段进度，discover 总数未知只报 found 不伪造百分比；停止检查点补齐目录遍历（folder 级+文件级 check_cancel）与分组循环，与指纹循环共用 `_stopped()` 收尾（phase=cancelled + stopped_at + 数量，任务行记 PARTIAL），文案明确「再次扫描会复用已有识别结果」；删除末尾全量批量 re-stat，改 `fingerprint_cached` 逐文件在处理当口复核。
- **S4 导出独立操作**（P1「报告失败拖垮已完成扫描」/§四F）：`_inventory` 入库后直接返回业务 summary，不再自动 `export_audit`；`export_audit` 混合组缺 vhash 成员不崩溃、details 逐成员如实标注「视觉指纹：缺失」；plan 审核报告写 export_dir 失败降级为 result.report_error，签名计划已落盘仍记任务成功；Web 新增 POST /report 显式导出（不占任务行）、CLI export 子命令沿用。
- **E4 导出内容修复**（P2「容器时代路径与错误单位」）：新增 `display_path()`/`bitrate_kbs()`，export_audit 与 plan 共 3 处 `/media/<root_id>/` 容器路径改为授权目录真实路径；BitrateKbs 从 bit/s 换算 Kb/s（÷1000，修正千倍偏差）；Duration/Bitrate 缺失不再伪装成 0/None 字面量（CSV 写空、HTML 显示 -），Resolution 统一 `?x?` 不再拼 NonexNone。
- **R1 审阅选择语义**（P0「审阅默认选中非最小文件」/§三.3）：移除「最小文件=建议保留」全部三处落点（默认预勾选、首页候选口径、报告话术），初次进入不自动选中任何清理项，视觉相似组默认全部保留；新增 `suggestions()`——SHA 子组优先保留受保护副本（只读目录/硬链接），否则按文件夹路径+文件名稳定顺序保留第一份并展示理由，绝不用文件大小推断；外挂字幕不一致/元数据异常/字幕检查失败的子组整体跳过并列出原因；混合相似组只处理各自 SHA 子组，转码版本归 manual。
- **R2 分页与审阅草稿**（P2「整集加载/无草稿/首页重复计算」/§四D）：审阅页服务端分页（每页 20 组）+ 类型/待处理-已处理/文件夹/名称筛选 + 超大分组 12 成员预览（?full 展开），文件按本页需要经 load_files 子集加载、缩略图 lazy；新增 drafts 草稿表（绑定 scan_id、乐观修订号）：跨页合并、旧修订保存返回 409 提示刷新不静默覆盖、保存失败页面选择保留且提交禁用；post_plan 只吃草稿，scan_guard 拒绝继承旧扫描选择；首页指标改读 scans.summary 持久化 + scan_metrics 单一来源。
- **C1 多帧比较引擎**（§四C「多帧预览按需生成」/§三.3）：新增 `preview_frames(cfg, item, duration)`——FRAME_POSITIONS=(0.1,0.25,0.5,0.75,0.9) 五个相对位置按需抽帧（复用 thumbnail→run_tool 的 ffmpeg 单路串行 + -threads 1 + nice 调用链），每帧返回 {pos, t=duration×pos, img}；私有缓存 state_dir/previews/，键=digest(PREVIEW_VERSION, path, stat)，文件替换自然失效；单帧失败降级为该帧 error 项，缓存只收成功帧。
- **C2 比较界面与确认页清单**（P1「确认页缺保留/清理清单」/§三.3-§三.4）：新增 GET /compare——并排展示分辨率/时长/编码/帧率/大小/音轨语言/声道/内嵌/外挂字幕九字段并高亮差异行，五帧 tab 同步切换且标注按各成员时长换算的实际时间，帧图走 GET /preview（按需生成、`_PREVIEW_LOCK` 进程内串行防并发 ffmpeg、(scan,key) 归属校验、stat 不符拒绝）；页面明示不提供质量评分或相似百分比，「相似度差异」改中性「画面指纹距离 d/64」；确认页直接展示各组保留/清理文件与所在文件夹、差异注意点、返回修改入口且保留全部选择；清理执行入口保持待平台验证禁用（CLEANUP_DISABLED，文案如实说明「直接永久删除、不进回收站」，不宣称可恢复）。
- **X1 静态资源抽取与打包清单**（§四E/§四F）：PAGE 字符串中的 `<style>`（163 行）与全站共享 `<script>`（107 行）逐字节机械抽取到 `static/app.css`、`static/app.js`（webui.py 2381→2116 行），页面外链引用并内联 `var VD_PX` 供脚本拼接接口 URL，CSS 字体改相对引用 `../font`；新增 /static/ 路由（文件名白名单精确匹配、无路径拼接），匿名面仅放字体与样式表；版本显示改从 fpk/manifest 读取，消除运行时显示版本的手工漂移；build_fpk.py 清单纳入 static/ 与 tasks.py，AGENTS.md WSL 同步命令纳入 static。
- **V0 本地冒烟**：双 TCP 实例（A=18099 带 VD_ALLOW_NO_ADMIN=1、B=18098 默认鉴权）经 stdlib http.client 执行 38 项 HTTP 检查：核心页面全 200；默认模式下匿名访问业务页/缩略图/POST 全 403、伪造 X-Trim-Isadmin 头无效；CSRF 缺失/请求体超限/越界参数拒绝；/font 与 /static 公开可达；全部链接保持网关前缀。结论登记 VALIDATION.md。
- **D1 文档收尾**（本条目）：Log.md 登记本轮全阶段；README.md/README.en.md 同步用户可见行为变化（扫描范围绑定授权文件夹、读取鉴权收紧、任务状态持久化、审阅分页与草稿、多帧并排比较、默认不预勾选、清理入口待平台验证暂不可用）；VALIDATION.md 登记双平台全量结果、本地冒烟结论与「系统回收站真机验证待做（阶段 0）」。
- 验证：Windows 全量 `py -3.12 -X utf8 -m unittest discover -s tests -v` 141 项全绿（7 项为 Windows 平台性跳过的 Linux 删除路径）；WSL Ubuntu-26.04（Python 3.14.4，无 numpy/ffmpeg）同套 141 项 139 过 1 跳 2 失败——两处失败均为本轮新增测试对干净 Linux 环境的前置依赖而非引擎缺陷（`test_start_aborts_when_setup_fails` 的 mock 解释器未设执行位，Git Bash 视一切文件可执行故 Windows 通过、真 Linux `test -x` 拒绝；`test_ffmpeg_args_serial_low_priority_shape` 依赖宿主 PATH 有 ffmpeg，`resolve_tools` 先于被桩替的 run_tool），详见 VALIDATION.md。两处失败已在终局门禁当场由回归修复工程师修复（`test_start_aborts_when_setup_fails` 的 mock 解释器补执行位并探测可用 bash；`test_ffmpeg_args_serial_low_priority_shape` 的宿主 ffmpeg 依赖消除，见 tests/test_videodedupe.py 对应用例），复跑 WSL 全绿——最终状态双平台各 **144 项全绿**（Windows 跳过 7 / WSL 跳过 2，均为平台性跳过），「双平台全绿」提交门禁已满足。
- **提交拆分与版本 3.0.0**：全部重构按架构地图边界拆 6 提交入库（068d567 报告入库 / 57fec86 tasks 模块 / 2d27865 生命周期熔断 / c5c30ec 引擎 / 879ed91 Web 层 / 56dabb3 文档），各提交经 worktree 实测全绿（引擎提交态=新引擎+旧 webui 102 项兼容验证）；随后 fpk/manifest `version` 0.2.27→**3.0.0**（X1 后 manifest 为唯一版本源），144 项全绿后重构建 `dist/video-dedupe-3.0.0.fpk`（sha256 `1e6650b3…`，暂存区含 tasks.py 与 static/），早前 0.2.27 命名的本会话构建产物已清理。README 下载表与 docs/releases 发布文本留待发布 3.0.0 时同步。

## 2026-10-13 · v3.0.0 图标设计提案 01（未接入）

- 用户反馈现有图标粗糙、缺少独特性。当前桌面图标由 `build_fpk.py` 即时生成蓝底播放三角，与新版翠绿界面及页头叠框符号不统一。
- 依据 SHA-256 / 多帧 pHash 查重和人工对比流程，设计「同帧」：翠绿后框与暖白前框错位叠放，共享一个播放形负空间，表达多个视频文件对应同一内容。内置 image_gen 生成并调整；PNG 与设计规格保存在 `assets/icon/`，该目录加入 CRG 排除规则。
- 本次为视觉提案，未替换构建图标或 Web 品牌符号，未改版本及业务代码，未制作最终尺寸资产；未运行测试、构建、NAS 验证或发布。

## 2026-10-13 · v3.0.0 发布

- README 双语下载表/版本声明/版本历史与 AGENTS.md 构建注释对齐 3.0.0；新增 `docs/releases/v3.0.0.md` 双语发布文本（状态声明：清理入口待回收站真机验证前禁用，升级后属预期行为）。
- README 随包分发故重打包（fpk sha256 `86eb9e2b…`，构建含时间戳不可复现，发布附件以 SHA256SUMS.txt 为准）；按 fresh-history 重做 public 孤儿分支快照推送 GitHub（`git push public public:main`，禁推本地 main）并发布 release v3.0.0（附件 fpk / 源码归档 / SHA256SUMS.txt）。
- 界面截图仍为 v0.2.27 版本，已在发布文本如实声明，随后续版本更新。

## 2026-10-13 · v3.0.0 应用图标接入

- 用户提供手绘新图标（SVG 母版 + 1024 渲染稿）：蓝底 #1D4ED8 圆角方载体、白色前后双帧错位叠放、负空间播放三角「只留一份」，替代此前提案与旧的过程式蓝底三角。母版入库 `assets/icon/`（提案素材一并保留）。
- `build_fpk.py` 删除纯 Python 过程式 `icon()` 与 struct/zlib 依赖，改为静态资产：`fpk/ICON.PNG`（64）与 `fpk/ICON_256.PNG`（256）由母版经 ffmpeg lanczos 渲染入库，`app/ui/images/icon_*.png` 随之静态化；源码归档新增 `assets/icon/*.svg` 设计源。
- `webui.py` 页头 logo 换新设计迷你版（24 网格：半透明后帧 + evenodd 镂空播放三角的前帧，主题无关），并新增 SVG data-URI favicon；单引号与 title/style 顺序断言保持。
- 验证：Windows 144 项全绿、compileall 通过、fnpack 构建成功（fpk sha256 `24f23ce4…`，构建嵌时间戳不可复现，发布以 SHA256SUMS.txt 为准）；release v3.0.0 附件已同步刷新。

## 2026-10-13 · v3.0.1 真机验收反馈修复（三项）

- **反馈3（旧记录 guard）**：新增授权文件夹后旧扫描审阅报「扫描不存在或目录配置已经变化」必须重扫。根因：`scan_guard` 对全量 `config_identity` 比对，任何授权增删都击穿。改为逐根比对——扫描时的每个授权根现在仍存在且挂载身份（dev/ino）一致即可查看；移除/替换旧根才拒绝。`scans.config` 仍写全量身份留档；计划与执行一致性由 plan 文档签名 + apply 终检独立保证，安全不变量不变（83ddf5a）。
- **反馈2（母文件夹勾选）**：目录卡片此前只有子目录勾选入口，扫某个授权根全量只能逐个勾子目录。引擎 `selection`/`scan_scope` 支持空 rel（`root:` = 整个授权根，与子选择混用时根覆盖子）；首页每张授权卡片新增「扫描整个文件夹（含所有子目录）」勾选框，勾整根后同根子勾选不再重复计数（6a7b51d）。
- **反馈1（实时指标与实时审阅）**：扫描中概览四卡原先全显示「完成扫描后显示」、审阅页 503。现指纹循环维护 live_sha，节流 0.5s（末文件强制重算）产出实时下界指标（重复组/建议可清理/可释放字节，`≥` 前缀标注）随 progress.json 写盘；≥2s 把初步完全重复组节流落库（files 增量补插 + groups 全量重建附初步建议，无 scans 行），新增 `live_groups`/`live_files` 只读通道；概览四卡实时渲染并由既有轮询刷新、扫描卡加「实时查看已发现的重复」入口；/review 扫描中改为实时只读预览（无勾选/比较/草稿脚本，瓦片按初步建议标注）。完成态先清临时行再写完整结果、取消同样清理，两态不混存；顺手修扫描进度条首屏漏 f 前缀的样式缺陷（3464b43）。
- 验证：Windows 149 项全绿（skip 7 平台性）、WSL Ubuntu-26.04 149 项全绿（skip 2）；compileall 通过；CRG 增量更新无错误。真机复验（实时指标随扫描增长、整根勾选、加授权后看旧记录）待用户在 NAS 上安装 v3.0.1 后确认，结论登记 VALIDATION.md。
- 版本：fpk/manifest → 3.0.1；README/README.en 当前版本、下载表与版本说明同步。

## 2026-10-13 · 真实清理引擎层封口（Review 26.10.12 §四A 第一步，未发布）

- **为什么**：此前清理禁用只挡在 Web 层（CLEANUP_VERIFIED），引擎 `apply --execute` 仍是可直达的永久删除路径；与「清理必须走系统回收站、可恢复」的产品边界不符。本阶段把封口下沉到引擎层，Web 按钮降为体验层提示。
- **引擎**：`videodedupe.py` 新增模块级 `require_cleanup_available()`——无条件 `raise Unsafe("系统回收站能力尚未验证，真实清理不可用")`，是真实清理的唯一门禁；docstring 写明放行条件：仅当经真机验证的系统回收站执行器接入，且放行的必须是新执行器、不是旧 unlink 路径。`apply()` 在 `execute=True` 时先于 Linux/confirm 校验与 `tasks.run` 占用调用门禁（禁用状态不产生 apply 任务行，预演不受影响）；`_apply()` 真实执行体开头同样调用——绕过 `apply` 的内部调用也拒绝。CLI `apply --execute` 与 webui `apply_worker` 均经 `apply` 进入，无旁路。
- **Web**：`webui.py` 的 CLEANUP_VERIFIED 块保留，注释注明它是体验层、真实门禁在引擎层（开关误翻也删不掉）；文案口径复核仍准确（被禁用路径确为「直接永久删除、不进系统回收站」）。
- **测试**：新增 3 项双平台用例——execute 被门禁拒绝且 `_apply` 不被调用、tasks 无 apply 任务行、直接调 `_apply` 同样被拒；预演不收口由既有 dry-run 用例覆盖（test_exact_and_unicode_and_dry_run / test_dry_run_uses_cache）。5 项 Linux 删除机制用例（anchored/幂等重试/原子认领/崩溃 journal/批次中断）改为用例内 patch 放行门禁——默认封口、机制测试显式开门，不删用例不加 skip；test_execute_requires_confirmation 的拒绝现来自门禁（在其之前），断言不变仍绿。
- **文档**：AGENTS.md 项目描述从「执行显式确认的永久删除」改为「清理走系统回收站执行器（待真机验证），验证通过前引擎层封死真实删除」；安全不变量清单前加封口状态引言（八条不变量本身未改）。
- 验证：Windows 全量 `py -3.12 -X utf8 -m unittest discover -s tests` 152 项全绿（skip 7 平台性）；compileall 通过；WSL 由工作流门禁统一跑。

## 2026-10-13 · 审阅差异确认改快照语义（Review 26.10.12 §2，未提交）

- **为什么**：原实现里差异确认形同虚设——confirmDiff 只置卡片 `data-confirmed='1'`，setTile 改勾选既不清确认也不动提示条，vdCollect 保存草稿时还用当前 clean 现生成 confirmed（`confirmed=clean.slice()`），选择怎么改确认都自动跟进，服务端 clean ⊆ confirmed 校验永远放行。
- **app.js**：新增全局 `confirmedByGroup[gid]`（确认时刻的已选 key 有序快照）；setTile 先取 checkbox，无实际变化（`cb.checked===on`）直接 return（不触发保存、不作废确认）；实际变化时清确认快照 + `card.dataset.confirmed='0'` + 差异提示条从「已确认」切回「需重新确认」并重新给出确认入口（服务端为已确认组双渲染 data-state=ok/pending 两个 .diffnote，客户端只切可见性、不在 JS 里重建 HTML）；confirmDiff 写入快照并把双提示条切回已确认态。
- **webui.py 审阅页**：已确认组 diffnote 双渲染（ok 可见 + pending 隐藏）；本页各组从草稿取回确认快照（含 clean ⊄ confirmed 的失效快照，原样带回、由提交前校验拦截）载入时初始化 `confirmedByGroup`；vdCollect 的 confirmed 改为只读快照（`confirmedByGroup[gid]||[]`），删除用当前 clean 现生成的旧逻辑。render 的 data-confirmed 仍按 clean ⊆ confirmed 判定（现状核对后保持）；比较页 vdConfirmCompare 本就存确认时刻的 key 列表（快照语义），未动。
- **测试**：新增 test_review_diffnote_states_and_confirm_snapshot——确认态/需重新确认态 HTTP/HTML 渲染断言（data-confirmed 判定、双提示条与隐藏确认入口、快照恢复 JSON、超集快照原样带回）+ 审阅页内联脚本源码断言（vdCollect 读快照、无 clean.slice 现生成）+ app.js 源码断言（无变化早退、清确认三件套、确认写快照）。仓库测试无 JS 运行时，setTile/confirmDiff 的浏览器执行态未覆盖，留待真机冒烟。
- 验证：Windows 全量 `py -3.12 -X utf8 -m unittest discover -s tests` 153 项全绿（skip 7 平台性，152 基线 + 本轮 1 项新增）；compileall 通过；`node --check static/app.js` 语法通过；CRG 增量更新无错误。WSL 由工作流门禁统一跑。

## 2026-10-13 · 审阅草稿串行保存与计划修订号绑定（Review 26.10.12 §3，未提交）

- **为什么**：原审阅页草稿脚本是「布尔 dirty + 一次性 vdSave」——防抖与翻页/提交可并发多个保存请求，旧请求的成功回调直接把 dirty 置 false，飞行中的新编辑可能被吞（提交的是旧快照、状态却已显示保存）；提交也不校验草稿修订，慢一拍的标签页能用旧认知生成计划。
- **static/review.js**（新增，按 X1 先例抽离审阅页内联脚本，页面只注入 JSON 配置标签 vd-review-cfg：scan/rev/csrf/px/confirmed 快照）：editSeq/savedSeq 双编辑序号——任何选择变化 editSeq++；saveLatest() 串行循环（全局唯一 vdLoop，自动保存/翻页前保存/生成计划前保存共用同一循环，绝不并行）在 savedSeq<editSeq 时提交当次快照，成功只推进 savedSeq=submittedSeq 并继续补存；dirty 恒等于 savedSeq!==editSeq（vdDirty()），飞行中旧请求的成功回调无从置「干净」。postDraft() 请求包装：非 2xx/409/响应非 JSON 或缺 rev 一律 reject 不伪成功；409（他人已保存更新）置冲突态后停止一切自动保存并提示刷新，绝不自动拿新修订重试覆盖。生成计划：冻结页面选择控件等 saveLatest() 成功后把隐藏域 draft_rev 刷为最新已存修订再提交，保存失败解冻留在本页；dirty 时 beforeunload 离开警告（不在此发请求，不承诺异步必达）。
- **webui.py**：post_plan 新增 draft_rev 门禁——与本次 load_draft 读到的修订不符（另一标签页已保存更新/本页修改未保存成功/缺参的陈旧页面）一律 409（要求刷新确认最新草稿），如实落失败任务行；计划生成继续只用本次读取的草稿快照透传 selections（核对 plan_worker→v.plan 不重读草稿，加注释固化该契约）。_STATIC_TYPES 白名单纳入 review.js（与 app.js 同为管理员权限）。
- **测试**：新增 test_plan_draft_rev_gate（修订匹配 303 / 陈旧修订与缺参 409 / 409 后最新草稿未被覆盖且不进后台线程）与 test_review_serial_save_script_contract（review.js 源码断言：双序号三态、循环全局唯一、savedSeq=submitted、无 vdDirty=false 直接置净、409 标注与自动保存停止、beforeunload、draft_rev、冻结/解冻）；既有 /plan 用例补 draft_rev 参数，审阅页断言从内联 VD_REV 改为 JSON 配置标签口径。另以 node vm 桩 DOM/fetch 实跑 18 项执行态检查（飞行中编辑被补存、409 停止、无效响应不伪成功、提交冻结/失败解冻、draft_rev 刷新），临时脚本不入库。
- 验证：Windows 全量 `py -3.12 -X utf8 -m unittest discover -s tests` 155 项全绿（skip 7 平台性；153 基线 + 本轮 2 项新增）；compileall 通过；`node --check static/review.js` 通过；CRG 增量更新无错误。WSL 由工作流门禁统一跑；浏览器真机冒烟（串行保存/冻结提交实机表现）留待后续。

## 2026-10-14 · 计划状态唯一事实来源与占用边界收尾（Review 26.10.12 §4+§5A，未提交）

- **为什么**：/plan-status 的 phase/done/total 此前读全局 progress.json——任务A成功后任务B扫描中时查A会串到B的数字；failed 任务在 progress 残留旧 plan_ready 时被排在成功分支之后而遭「计划已生成」掩蔽；post_plan 在 tasks.claim 之后、worker 线程接管之前的同步校验若抛出内层捕集之外的异常（sqlite3.Error、Thread.start 失败）会留下悬挂 running 行；预演失败时任务行已 SUCCESS 但确认页仍宣称「清单已生成并核验完毕」。
- **webui.py**：新增模块级纯函数 `plan_status_view(task, result)`，/plan-status 的 HTML 与 JSON 两分支统一消费——phase/done/total 取任务行镜像（write_progress→tasks.reflect），plan_id 仅 SUCCESS 且结果可读时给出；HTML 分支序固定为 running→interrupted→failed→成功可读→结果不可读，历史 progress 兼容（仅当库里查不到任何 plan 任务行）收敛到状态页唯一一处并注释。get_plan_review 对 SUCCESS 但结果不可读如实 404（原会回退 persisted plan_ready 掩蔽）。post_plan 把 claim 后全部同步校验与线程启动包进同一 try：任何异常先经 `_fail_claimed_task`（FAILED 收尾；收尾再失败时原始与收尾双异常落日志、任务行交启动 reconcile 兜底）再渲染 500 错误页，303 只在线程成功启动后发出。plan_worker 计划生成与预演分离：SUCCESS 只代表生成成功，预演成败单独存 `_PLAN_PREVIEW`（`{"ok","detail"/"error"}`），预演失败不回写 plan_error；确认页预演状态单独表达——未预演/不在本进程/失败分别给出「预演核对明细不在本进程 / 预演未完成（附原因） / 完成预演核对」文案，绝不把未完成的预演显示为已核验。
- **测试**：新增 5 项——plan_status_view 纯函数契约（含 None 任务/非 dict 结果防御）；任务A成功+任务B扫描中查A不串B（done/total/phase 取A的任务行镜像）；任务B失败+全局 progress 残留A的 plan_ready 时 B 如实显示失败、interrupted 与 success-结果不可读同样不被掩蔽；重启后（清空进程内预演缓存）从任务行恢复确认页且预演三态文案分明；claim 后校验注入 sqlite3.Error 与 Thread.start 失败（只换 webui 命名空间的 threading，不碰测试服务器线程）→ 任务行落 FAILED、无 running 残留、不进后台线程。
- 验证：Windows 全量 `py -3.12 -X utf8 -m unittest discover -s tests` 160 项全绿（skip 7 平台性；155 基线 + 本轮 5 项新增）；compileall 通过；CRG 增量更新无错误、变更风险低（引擎与删除安全路径零触碰）。WSL 由工作流门禁统一跑。

## 2026-10-14 · journal 不可读定级 VERIFY 与四入口统一（Review 26.10.12 §5B，未提交）

- **为什么**：`journal_summary` 此前把读取/解析失败降级为零计数——读不到 journal ≠ 确认没执行，`run()` 异常路径据此把「不可证未执行」的清理任务误判为 FAILED；同类「不确定执行」在四个入口各说各话（异常→failed、键盘中断→interrupted、stale 回收→interrupted、重启 reconcile→verify），违反同一语义唯一口径。
- **tasks.py**：`journal_summary` 返回结构加 `readable` 标志——文件缺失/损坏（非法 JSON 或结构非预期，catch 扩到 TypeError/AttributeError）时 `{readable: False, deleted: None, errors: None, pending: None, pending_key: ""}`，不再伪零计数。新增 `apply_outcome(cfg, plan_id)` 统一定级：pending 为真或为 None（不可证未执行）→ VERIFY；deleted 非零 → PARTIAL；否则 FAILED；汇总函数自身抛错（故障注入）也按不可读处理，绝不降级 FAILED。四入口全部改经 `apply_outcome`：`run()` 的执行器异常与 KeyboardInterrupt（apply 型不再记 interrupted）、`claim()` 的 stale 回收（apply 行按 journal 定级，scan/plan 仍 interrupted）、`reconcile()`（原无条件 verify 改为可证零执行时如实 FAILED）。VERIFY 为终态，任何自动流程不重放/重试。
- **webui.py**：apply-status 与 apply-result 的 VERIFY 分支适配不可读汇总——`readable=False` 时如实显示「删除日志缺失或不可读，无法确认已执行的范围」，不渲染「已完成 None 项」伪计数；文案从「应用重启时清理没有执行完成」改为「清理未正常结束，执行结果不确定，需按删除日志人工核实」（VERIFY 现可来自异常/中断/stale，不止重启）。videodedupe.py 仅改 `apply` docstring 口径。
- **测试**：`test_journal_summary_counts_and_defaults` 重写（缺失/损坏 JSON/结构损坏三态）；新增 `test_apply_outcome_grading_by_journal`（五档定级含 pending 优先于 partial）、`test_apply_uncertain_finalization_shared_across_entries`（异常/KI/stale 回收三入口 × 缺失→VERIFY、仅删除→PARTIAL、空 journal→FAILED，scan 行回收仍 interrupted）、`test_apply_grading_survives_journal_summary_fault`（mock journal_summary 抛错 → 终态 VERIFY 非 FAILED）。
- 验证：Windows 全量 `py -3.12 -X utf8 -m unittest discover -s tests` 163 项全绿（skip 7 平台性；160 基线 + 本轮 3 项新增）；compileall 通过；CRG 增量更新无错误、变更风险 0.36 低（删除执行器本体与安全不变量零触碰）。WSL 由工作流门禁统一跑。

## 2026-10-14 · 比较页差异拆分、预览生成移出 GET、实时指标去承诺化（Review §6+§7 前半，未提交）

- **为什么**：比较页把「有展示差异」与「需额外确认」混在同一个 notes 判定——成员元数据全失败时各列同为「读取失败」，行内一致反而显示「本组各版本参数一致」；GET /preview 在请求线程顺手跑 ffmpeg，与扫描的互斥无保障、忙碌无明确反馈；扫描中四卡用「≥ 前缀 + 可释放空间」把已识别部分的下界包装成释放承诺。
- **videodedupe.py**：预览缓存拆出 `preview_cache_stem`（文件身份 path+stat 与 PREVIEW_VERSION 共同决定的缓存键）与 `cached_preview_frames`（只读缓存查询，缺失/损坏返回空列表，绝不触发 ffmpeg）；`preview_frames` 改为消费同一身份生成落盘（生成行为不变）；live_metrics 处注释口径同步为「初步值」（progress 字段名与数值口径不变）。
- **webui.py 比较页拆分**：has_display_diff（任一展示行有差异）决定能否显示「参数一致」，needs_confirmation（notes 非空）单独驱动确认卡——两者独立：仅分辨率/编码等展示差异如实高亮但不强制确认；成员元数据读取失败时单独呈现「本组无法完整比较」，绝不因各列都失败判一致。
- **webui.py 大组确认限制**：hidden_selected（clean 键集 − 比较页可见键集；可见上限 COMPARE_MAX_MEMBERS=12 语义不变）非空时停用整组确认按钮并解释原因（本页核对不到的差异不能被确认）；分页/逐页确认合并明确不做。
- **webui.py 预览生成拆分**：GET /preview 只读缓存（无缓存 404「尚未生成」）；新增 POST /preview/generate——管理员+CSRF+请求体上限由 _post_guard 统一，参数经与 GET 共用的 _locate_preview_record 校验（记录定位/授权根/元数据/stat 复核同一边界），tasks.oplock 非阻塞占用（与扫描同一把跨进程操作锁）：忙碌 409 明确返回不排队，锁内复查缓存（拿到锁前他人可能已生成）防重复生成，生成仍单路 -threads 1（preview_frames 原路径）；不新增任务类型、不碰 tasks.TYPES。删除进程内 _PREVIEW_LOCK（跨进程操作锁是其超集）。比较页缺缓存成员渲染占位格（实际时间仍由元数据标注）+ 显式「生成预览画面」按钮（vdGenPreview，成功后整页刷新）。
- **webui.py 实时指标去承诺化**：扫描中四卡移除「≥」前缀与「可释放空间」——改「完全重复候选（初步）」「候选文件合计大小」，副标注「扫描结束后可能调整」；轮询 JS 同步去前缀；审阅实时预览横幅与 lead 对齐候选口径。完成态/空闲态卡片的「可释放空间」按非实时呈现保留（完成态数字来自扫描完成时持久化的建议合计，副文案已如实描述为「预计释放」）。
- **测试**：新增 CompareSplitScenarioTests 四项（仅分辨率不同/仅编码不同→展示差异但不要求确认、参数一致不出现；元数据全失败→无法完整比较、绝不判一致也不误报差异；13 成员组 hidden_selected 停用确认+原因、可见内选择恢复按钮）；新增 test_preview_generate_post_contract（CSRF 缺失 403、非法参数 404、生成后 GET 可读且比较页渲染帧图、已有缓存直接命中不重复生成、操作锁占用 409 busy）与 test_scanning_metric_cards_wording（新文案在、旧「≥」「可释放空间」不出现于扫描中页面）；既有 preview/compare 用例改到只读缓存口径（GET 断言不触发生成）。
- 验证：Windows 全量 `py -3.12 -X utf8 -m unittest discover -s tests` 169 项全绿（skip 7 平台性；163 基线 + 本轮 6 项新增）；compileall 通过；CRG 增量更新无错误、get_review_context 风险低（删除安全路径与引擎分组算法零触碰）。WSL 由工作流门禁统一跑；生成按钮/忙碌提示的浏览器实机冒烟留待后续。

## 2026-10-13 · Review 26.10.12 六提交加固收口（双平台门禁逐提交全绿，独立复核通过）

- 本轮按用户提供的 7 节 review（docs/Review_26.10.12.md）完成 6 提交加固，基线 f60540f..HEAD 按时间序：7f46522 引擎层封死真实清理（`require_cleanup_available` 唯一门禁）；f102da2 差异确认随选择变化失效——确认改快照语义；754cbf7 草稿串行保存与计划修订绑定（draft_rev 门禁）；d44c398 任务行为计划状态唯一事实源、占用后异常必收尾；7707501 journal 不确定结果保守定级为待核实（VERIFY）；29add72 比较页拆分差异与确认、预览生成移出 GET、指标措辞去承诺。各提交实现细节见上方逐阶段条目（引擎封口→确认快照→草稿串行→任务状态→journal 定级→比较页/预览/指标）。
- 双平台门禁逐提交全绿：Windows 全量各提交态 152 → 153 → 155 → 160 → 163 → 169 项递增全绿（skip 7，均为既有 posix 平台护栏）；WSL Ubuntu-26.04 同套源码由工作流门禁统一执行，双平台绿后提交方落库。
- 独立复核（工作流复核员，只读 diff+源码+测试）结论摘录：verdict **pass**，无 high 发现，六项验收逐条核验满足；按验收命令原样复跑 Windows 全量 Ran 169 tests in 65.186s、OK (skipped=7)，7 个 skip 逐一枚举确认为基线平台护栏；八条安全不变量核对未破坏（删除机制本体——原子暂存/journal 先行/终检——零改动，只加门禁）。两项 low 如实记录：① 审阅页浏览器行为（setTile 确认失效、saveLatest 串行循环、提交冻结）在仓库测试内仅源码字符串断言、无 JS 运行时执行态覆盖，开发时 node vm 桩 DOM/fetch 实跑的 18 项执行态检查为临时脚本不入库、无法复核；② apply 状态页 done/total 仍镜像全局 progress.json——不在验收第 4 条（计划状态）范围，running 分支因 oplock 单写者不串台、终态分支不消费这些数字，仅为观察项。
- 收尾登记（本条目）：VALIDATION.md 同步登记双平台逐提交结果、独立复核结论与「浏览器端 JS 行为无 JS 运行时、仅源码断言+HTTP 级测试，待真机或人工核验」的如实声明；真机系统回收站阶段 0 仍待做，清理执行入口保持引擎层封死。

## 2026-10-14 · v3.0.2 发布（真机验收修复 + 六提交加固合并发布）

- 用户确认发布。用户指定版本号为 3.0.2（此前 3.0.1 仅存在于本地开发历史、从未公开，其内容并入本版）；f60540f 起的 3 项真机验收修复与 7f46522..66190ad 的 6 项加固 + 文档登记合并发布。
- 发布材料：docs/releases/v3.0.2.md（双语，GameLibrary 格式；状态声明改写——真实清理自本版起为引擎层唯一门禁，v3.0.0「CLI 保留执行路径」的说法不再成立）；README/README.en 的 v3.0.2 版本说明同步加固内容与候选口径指标措辞。
- 构建：fnpack 1.2.3 产出 dist/video-dedupe-3.0.2.fpk（sha256 见 SHA256SUMS.txt，fpk 嵌时间戳不可复现，发布以校验和文件为准——v3.0.0 先例的混合构建口径）。发布流程：孤儿分支快照强推 public:main + tag v3.0.2 + gh release v3.0.2 附 fpk/源码归档/SHA256SUMS.txt。
- 验证：发布前 Windows 全量 169 项全绿（skip 7）；工作流终局已双平台全绿；真机安装与浏览器交互点验待用户回填 VALIDATION.md。
