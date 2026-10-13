# 第三方组件与许可

本仓库原创代码（videodedupe.py、webui.py、build_fpk.py、fpk/ 生命周期脚本与配置、tests/）使用 MIT 许可证，见 `LICENSE`。

## 运行期依赖（安装时经 venv pip 拉取，非本仓库分发物）

- **numpy**：BSD-3-Clause。用于 32×32 灰度帧的 DCT-II 变换（pHash）。
- **static-ffmpeg**（pip 包）：分发 GPL v3 静态编译的 ffmpeg/ffprobe 二进制（源自 BtbN/FFmpeg-Builds 一类上游）。本项目仅以子进程方式调用，不链接、不修改、不随包分发其代码；GPL 源码获取：https://ffmpeg.org/download.html 及该 pip 包项目主页。按 GPL 要求，替换系统中的该组件不影响本仓库 MIT 代码。

## 随包分发的资源

- **Outfit 可变字体**（`fpk/app/fonts/outfit-latin-wght.woff2`、`outfit-latin-ext-wght.woff2`，SIL Open Font License 1.1，© 2021 The Outfit Project Authors）：Web UI 界面字体（拉丁字符集），经 [Strata](https://github.com/Niko1221/Strata) 仓库取自托管子集文件；完整许可文本见 `fpk/app/fonts/OFL.txt`。SIL OFL 允许随包分发与商用，不得单独出售字体本身。

## 算法参考（未复制代码）

- [Video Duplicate Finder v4.1.1](https://github.com/0x90d/videoduplicatefinder)（AGPL-3.0）：v0.1.0 曾以官方容器为扫描引擎；v0.2.0 起仅参考其帧位置（10/25/50/75/90%）与相似度阈值语义（96% ≈ pHash 平均汉明距离 ≤10），未复制任何源码。
- [MediaDedupe](https://github.com/parcival42/MediaDedupe)（MIT）、[vid-fp](https://github.com/Danielnara24/vid-fp)（Apache-2.0）：调研参考，未复制源码。

## 平台组件

- 飞牛 fnOS `python312` 官方运行时与应用框架（fnpack 1.2.3 打包）；Python Software Foundation License。
- 本地构建工具 fnpack 下载自 `static2.fnnas.com`（仅存 `.tools/`，不随包分发）。
