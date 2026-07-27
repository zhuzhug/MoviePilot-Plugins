# 媒体库清理 (LibraryCleaner)

扫描本地媒体库路径，识别悬空软链、孤儿元数据、空目录、同片重复资源等残留，VTabs 分类展示。支持单条/批量删除，删除视频时自动级联清理同 inode 硬链、指向源的软链与同名元数据。

## 检测能力

| ID | 中文名 | 状态 | 说明 |
|---|---|---|---|
| `dangling` | 悬空软链 | ✅ | symlink 目标不存在或已失效 |
| `orphan_meta` | 孤儿元数据 | ✅ | `.nfo/.jpg/.png/.srt/.ass/.ssa/.sub/.idx/.vtt` 等同目录无同名视频 |
| `empty_dir` | 空目录 | ✅ | 目录下无任何视频（仅剩元数据或完全为空） |
| `dup_resource` | 同片重复资源 | ✅ | 同目录多个视频经发行标签（分辨率/编码/来源/HDR/音轨/语言/分组）归一化后 stem 相同，默认保留字典序首个 |
| `dup_softlink` | 重复软链 | ⏳ v0.3.0 | 多个软链指向同一源 |
| `dup_hardlink` | 重复硬链 | ⏳ v0.3.0 | 同 inode 的多个硬链（跨目录/媒体库） |
| `missing_video` | 失联视频 | ⏳ v0.3.0 | 有 .nfo/.jpg 但视频文件已不存在 |

## 主要功能

- **扫描来源**：从 `settings.LIBRARY_PATH` 读取（分号分隔），也可在设置页手动追加扫描路径（每行一个）。
- **路径过滤**：`include_pattern` / `exclude_pattern` 均为 Python 正则，先 exclude 后 include。
- **视频扩展识别**：使用 MP 内置 `settings.RMT_MEDIAEXT`。
- **删除动作**（`allow_delete` 关闭时按钮不显示，默认关）：
  - **悬空软链** → 直接删除 symlink。
  - **孤儿元数据** → 删除该文件。
  - **空目录** → 删除该目录。
  - **重复资源** → 删除视频文件时级联清理：同 inode 硬链（跨扫描根搜索）+ 指向源 realpath 的软链 + 同名元数据（`_METADATA_EXTS`），并可选清空产生的空父目录（最多向上 3 层，不会碰扫描根本身）。
- **越权保护**：所有删除路径必须落在扫描根之下（含 realpath 兼容），拒绝 `..` 攻击。
- **定时扫描**：CRON 默认 `0 5 * * *`，可选扫描完成时发送通知。

## 配置项

| 项 | 说明 |
|---|---|
| CRON | 定时扫描表达式，默认 `0 5 * * *` |
| scan_paths | 手动追加扫描路径（每行一个） |
| include_pattern / exclude_pattern | 路径正则过滤 |
| enable_dangling / enable_orphan_meta / enable_empty_dir / enable_dup_resource | 4 类检测开关 |
| enable_dup_softlink / enable_dup_hardlink / enable_missing_video | v0.3.0 占位（disabled） |
| max_display_per_type | 每类展示上限，默认 200 |
| empty_cascade | 删除文件后清理空父目录 |
| allow_delete | **启用删除按钮**，默认关闭 |
| notify_on_scan | 扫描完成时发送通知 |

## 使用注意

- **v0.2.0 无试运行模式**：一旦启用 `allow_delete`，删除按钮直接调用后端删除接口。建议先关闭 `allow_delete` 只做扫描核对，确认结果后再开启。
- **重复资源判定粒度**：仅按"同目录 + 归一化 stem 相同"匹配，跨目录的同名不同版本不会被合并（跨目录检测将在 v0.3.0 提供）。
- **扫描锁**：同一时刻只允许一次扫描，重复触发返回 409。
- **不递归符号链接**：`os.walk(followlinks=False)` 硬编码，避免死循环。

## 更新历史

| 版本 | 说明 |
|---|---|
| v0.2.0 | 新增同片重复资源检测与删除按钮，删除视频时级联清理同 inode 硬链、指向源的软链与同名元数据 |
| v0.1.0 | 首个开发版：识别悬空软链、孤儿元数据、空目录三类残留，仅扫描不删除 |
