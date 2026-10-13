# 同帧 · 图标设计提案 01

日期：2026-10-13。面向视频查重助手 v3.0.0 的新图标提案，未接入当前构建或替换现有图标。

成稿：`same-frame-concept-01.png`。使用内置 image_gen 生成初稿并调整，未使用付费 API/CLI 工作流。

## 设计依据

- `videodedupe.py:388` 的 `pair_match` 同时识别 SHA-256 完全相同与 pHash 视觉近似；`visual_fingerprint` 抽取多个时间点的画面指纹。图标用两个错位画框表达多个视频副本，用一个共享的播放形负空间表达同一内容。
- `webui.py:88` 的现有品牌符号是两个叠放方框；`build_fpk.py:68` 的桌面图标另行生成蓝底播放三角。提案结合重复与视频两层含义。
- `static/app.css:14` 的界面主色为翠绿。提案采用深林绿底、翠绿后框和暖白前框，呼应现有界面。生成 PNG 有轻微色调变化，所列色值是设计目标。
- 大色块、粗轮廓、一个负形，无文字和细节装饰。重点表达识别与对比；当前版本清理执行入口保持关闭，不用回收站或清理完成标记表达尚未开放的能力。

## 资产状态

这是视觉提案 PNG。尚未制作确定几何的矢量母版及 64 / 256 像素交付文件，未做飞牛桌面实机小尺寸验证。

现有构建会在 `build_fpk.py:151` 调用 `icon(size)` 并生成包内 `ICON.PNG`、`ICON_256.PNG` 和 `app/ui/images/icon_{size}.png`，因此仅放置本提案文件不会改变已安装应用或后续构建的图标。Web 页头品牌图形也仍采用原有内联 SVG。

本次仅产出图标设计，未运行测试、构建、NAS 验证或发布。

## 生成提示词

工具：内置 `image_gen.imagegen`。下列为两轮提示词的设计规格记录。

初稿：

```text
Use case: logo-brand.
Asset type: one original minimalist application icon for video-dedupe, a native fnOS NAS app detecting exact SHA-256 duplicates and perceptually similar videos, followed by human comparison and review.
Concept: Same Frame / 同帧. Two offset rounded video frames share one right-facing rounded triangular negative-space opening. Two flat foreground forms maximum: warm-white front frame and a substantial emerald rear echo toward the upper left. Integrate the two-to-one relationship into one cohesive mark. Use large clear gaps and approximately 62% tile-width occupancy, optically centered.
Palette: deep forest-green #0B3028 rounded-square tile, emerald #34D399 rear echo, warm-white #F4FAF7 front frame; actual transparency outside the tile corners.
Style: exceptionally simple, geometric, flat 2D, clean vector-like contours, legible at small desktop-icon sizes. Single icon only.
Avoid: text, lettering, badges, magnifying glass, trash can, broom, fingerprint texture, film perforations, sparkles, infinite loop, cloud, disks, shadows, gradients, glow, bevels, glass, 3D, fine lines, mockups, multiple variants, watermark or imitation of a named brand.
```

调整：

```text
Use case: precise-object-edit. Preserve the two offset rounded video frames, proportions, overlap and single rounded triangular play opening. Eliminate texture, gradients, shading, glow, transparency holes and uneven color. Use only solid forest-green #0B3028, emerald #34D399 and warm-white #F4FAF7. Change the background to an opaque full-bleed square, with no rounded outer tile edge or transparent patches. The opening and gap match the background. Smooth crisp edges, measured padding, no added shapes or typography. Return one icon only, no mockup.
```
