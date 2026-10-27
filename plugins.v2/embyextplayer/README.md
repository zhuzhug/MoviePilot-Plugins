# Emby 调用播放器（EmbyExtPlayer）

给 Emby 详情页一键加上「调用本地播放器」按钮，支持 strm / 网盘（AList）直链。适配 `amilys/embyserver:latest` 镜像（原镜像作者 [@amilys](https://github.com/amilys)，感谢）。

## 前置条件

1. Emby 使用 **amilys/embyserver** 这类支持 `ext.js` 扩展机制的镜像（官方原版镜像没有 `data-main="ext"` 入口，脚本不会被加载）。
2. 给 MoviePilot **映射 Emby 安装目录**（容器 `/config` 对应的宿主机目录，目录内应含 `config/` 与 `data/`），并授予读写权限。
3. 可选：给 MoviePilot 挂载 `docker.sock`，可免重启直写容器。

## 功能

- 详情页原生播放按钮旁注入 Pot / VLC / IINA / NPlayer / MX / Infuse / 恒星 / MPV / 弹弹Play / 复制链接 共 10 个按钮，可勾选启用哪些。
- **strm / 网盘直链**：直接使用 `MediaSources[].Path` 中的真实地址唤起播放器（Emby 对 strm 的 `/stream` 端点普遍返回 500，无法走代理）。
- **中文路径修复**：直链已是百分号编码，脚本不再做二次编码（否则 `%E5` 会变成 `%25E5`，AList 返回错误 JSON，播放器报「无服务」）。
- **所有详情页生效**：不再因为 strm 条目无媒体流而不注入；Emby 重绘后自动补插。
- **条目类型过滤**：可限制只在电影/剧集/季等类型注入，避免合集、人物页出现无效按钮。
- 可选定时自愈：Emby 升级或容器重建后自动补写脚本。

## 配置项

| 参数 | 说明 |
| --- | --- |
| `enabled` | 启用插件 |
| `config_dir` | Emby 安装目录（MoviePilot 可见路径），留空自动探测 |
| `container_name` | Emby 容器名，默认 `emby` |
| `players` | 显示的播放器按钮，留空 = 全部 |
| `playable_types` | 注入按钮的条目类型，留空 = 不过滤 |
| `strm_host_rewrite` | 直链域名重写，例如 `http://192.168.2.60:9527`，留空用原始地址 |
| `watch_ms` | 按钮巡检间隔，默认 700ms |
| `direct_write` | 免重启直写容器（需 docker.sock） |
| `auto_restart` | 关闭直写时自动重启 Emby 使配置生效 |
| `heal_hours` | 自愈巡检间隔（小时），0 关闭 |

## 使用

1. 完成上面的映射前置条件。
2. 插件设置里打开「启用插件」，按需勾选播放器与条目类型，保存。
3. 打开插件**详情页**，直接点「立即应用」（也可发命令 `/emby_ext_apply`）。
4. 浏览器 `Ctrl+Shift+R` 刷新 Emby 网页，详情页即出现按钮。

## 详情页（部署控制台）

插件详情页会实时体检并展示 11 项检查，有问题的项会附带处理建议：

| 检查项 | 说明 |
| --- | --- |
| Emby 连接 | 是否能在媒体服务器配置里找到 Emby |
| 镜像扩展入口 | `index.html` 是否含 `data-main="ext"`（决定镜像是否支持本方案） |
| ext.js 可注入 | 是否存在 `extmod` 常量（不同魔改版格式可能不同） |
| 模块已登记 | `extmod` 里是否已含本模块名 |
| 播放器图标 | 镜像是否自带 `css/icons/icon-*.webp` |
| Emby 目录 | 安装目录是否找到且可写 |
| ext.sh 钩子 | 启动钩子是否已写入 |
| 持久化脚本 | 脚本是否已落盘及版本 |
| 版本一致性 | 磁盘脚本与插件内置版本是否一致 |
| 容器内生效 | Emby Web 是否已真正加载脚本 |
| docker.sock | 是否具备免重启直写能力 |

页面顶部显示"通过 x/11"，并显示**上一次操作**的时间与结果。

**四个操作按钮**（无需命令行）：

- **立即应用**：写入脚本 + 打钩子 +（可选）直写容器或重启
- **重新检测**：刷新体检结果（结果缓存 60 秒）
- **重启 Emby**：单独触发重启
- **还原改动**：移除钩子与脚本

对应的 API（前端 `bear` 认证）：`plugin/EmbyExtPlayer/apply`、`/check`、`/restart`、`/restore`

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `/emby_ext_apply` | 立即部署 / 重新部署 |
| `/emby_ext_status` | 查看部署状态（目录、ext.sh、容器内版本） |
| `/emby_ext_restore` | 移除插件写入的钩子与脚本 |

## 实现说明

插件做三件事：

1. 把渲染好的 `embyLaunchPotplayer.js` 写入 `<Emby目录>/embyLaunchPotplayer.js`（容器内即 `/config/embyLaunchPotplayer.js`）。
2. 在 `ext.sh` 中插入一段带标记的钩子（`# >>> MoviePilot EmbyExtPlayer BEGIN >>>`），启动时把它拷贝到 `/system/dashboard-ui/` 并确保 `ext.js` 的 `extmod` 含本模块名。只追加不覆盖，不改动用户的其他内容。
3. 可选：通过 `docker.sock` 直接写入运行中的容器（免重启），或调用 Emby `POST /System/Restart` 重启生效。

## 致谢

- 外挂播放器按钮的原始实现：[bpking1/embyExternalUrl](https://github.com/bpking1/embyExternalUrl)
- 提供 `ext.js` 扩展机制与图标的镜像：amilys/embyserver（[@amilys](https://github.com/amilys)）
