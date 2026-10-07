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
