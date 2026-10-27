// ==UserScript==
// @name         embyLaunchPotplayer
// @name:zh      embyLaunchPotplayer (strm 强制版)
// @namespace    http://tampermonkey.net/
// @version      1.4.0-strm
// @description  emby launch external player - force on every item page, strm/direct-url aware
// @description:zh-cn emby调用外部播放器（强制所有详情页生效，支持 strm 网盘直链）
// @license      MIT
// @author       @bpking (modified for strm)
// @github       https://github.com/bpking1/embyExternalUrl
// @include      */web/index.html
// ==/UserScript==

(function () {
    'use strict';

    /* ==================== 用户配置 start ==================== */
    const Potplayer = 1;              // 0 = 整体关闭
    const WATCH_MS = 700;             // 按钮巡检间隔(ms)，Emby 重绘后自动补插
    // strm/网盘直链域名重写：留空 = 完全按 .strm 里的原始地址
    // 若局域网内访问 DDNS 域名回环不通(NAT hairpin)，可填 NAS 内网地址，例如 'http://192.168.2.60:9527'
    const STRM_HOST_REWRITE = '';
    // 只在可播放的条目类型上注入按钮（避免合集/人物/音乐专辑等页面出现无效按钮）
    // 改成空数组 [] = 不过滤（回到旧行为）；条目类型解析失败时按"放行"处理
    const PLAYABLE_TYPES = ['Movie', 'Episode', 'Series', 'Season', 'Video', 'MusicVideo', 'Trailer'];
    // 只显示指定的播放器按钮（留空数组 [] = 全部显示）
    // 可选值：Pot / VLC / IINA / NPlayer / MX / Infuse / Stellar / MPV / Dandanplay / Copy
    const ENABLED_PLAYERS = [];
    /* ==================== 用户配置 end ==================== */

    if (Potplayer == 0) { return null; }

    const BOX_ID = 'ExternalPlayersBtns';
    // 注意：不要用 "div[is='emby-scroller']:not(.hide)" 先取第一个 scroller 再往下找，
    // 旧视图（列表页/上一个 item）的 scroller 依然匹配 :not(.hide)，会取到错误节点。

    // 取当前可见 item 视图的 .mainDetailButtons。
    // 注意：Emby 的 viewManager 最多保留 3 个视图在 DOM 中（旧视图只加 .page-hidden 不销毁，
    // 且 .hide 是加在 page 元素上而不是 emby-scroller 上），所以：
    //   1) 不能用 document.querySelector("div[is='emby-scroller']") 取「第一个」scroller
    //      —— 那往往是上一页（列表页）的 scroller，里面没有 mainDetailButtons；
    //   2) 必须排除 .page-hidden 祖先，并取文档中最后一个（最新 append 的视图）。
    function mainDetailButtons() {
        const list = Array.prototype.slice
            .call(document.querySelectorAll("div[is='emby-scroller'] .mainDetailButtons"))
            .filter(function (el) { return !el.closest('.page-hidden'); });
        return list.length ? list[list.length - 1] : null;
    }

    function currentPage() {
        const mdb = mainDetailButtons();
        if (mdb && mdb.closest('.page')) { return mdb.closest('.page'); }
        const pages = Array.prototype.slice.call(document.querySelectorAll('.page:not(.page-hidden)'));
        return pages.length ? pages[pages.length - 1] : document;
    }

    function scoped(sel) {
        return currentPage().querySelector(sel);
    }

    function currentItemId() {
        const m = /[?&]id=(\d+)/.exec(window.location.hash || '');
        return m ? m[1] : null;
    }

    // 强制版：只要有原生播放按钮组就注入。
    // 原版要求 .selectVideoContainer / .selectAudioContainer 未隐藏，
    // 而 strm/无 MediaStreams 的条目这两个容器会被 Emby 加上 hide，导致永远不注入。
    function showFlag() {
        return !!mainDetailButtons();
    }

    // ---- 条目类型过滤：每个 item 只查一次，巡检不会重复请求 ----
    const itemTypeCache = {};
    const itemTypePending = {};
    let itemTypeCacheSize = 0;

    function isPlayableType(type) {
        if (!PLAYABLE_TYPES.length) { return true; }   // 空数组 = 不过滤
        if (!type) { return true; }                    // 解析不到类型时不阻断，保持旧行为
        return PLAYABLE_TYPES.indexOf(type) > -1;
    }

    function getItemType(itemId) {
        if (Object.prototype.hasOwnProperty.call(itemTypeCache, itemId)) {
            return Promise.resolve(itemTypeCache[itemId]);
        }
        if (itemTypePending[itemId]) { return itemTypePending[itemId]; }
        let userId;
        try {
            userId = (ApiClient._serverInfo && ApiClient._serverInfo.UserId) || ApiClient.getCurrentUserId();
        } catch (e) {
            return Promise.resolve(null);              // ApiClient 还没就绪 → 放行
        }
        const p = Promise.resolve(ApiClient.getItem(userId, itemId)).then(function (item) {
            const type = (item && item.Type) || null;
            if (itemTypeCacheSize > 300) {             // 简单上限，避免长期挂页无限增长
                Object.keys(itemTypeCache).forEach(function (k) { delete itemTypeCache[k]; });
                itemTypeCacheSize = 0;
            }
            itemTypeCache[itemId] = type;
            itemTypeCacheSize++;
            delete itemTypePending[itemId];
            return type;
        }, function () {
            delete itemTypePending[itemId];
            return null;                               // 请求失败 → 放行，不影响现有功能
        });
        itemTypePending[itemId] = p;
        return p;
    }

    function buttonHtml() {
        return `<div id="${BOX_ID}" class="detailButtons flex align-items-flex-start flex-wrap-wrap">
            <button id="embyPot" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="Potplayer"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-PotPlayer">　</i> <span class="button-text">Pot</span> </div> </button>
            <button id="embyVlc" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="VLC"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-VLC">　</i> <span class="button-text">VLC</span> </div> </button>
            <button id="embyIINA" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="IINA"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-IINA">　</i> <span class="button-text">IINA</span> </div> </button>
            <button id="embyNPlayer" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="NPlayer"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-NPlayer">　</i> <span class="button-text">NPlayer</span> </div> </button>
            <button id="embyMX" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="MXPlayer"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-MXPlayer">　</i> <span class="button-text">MX</span> </div> </button>
            <button id="embyInfuse" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="InfusePlayer"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-infuse">　</i> <span class="button-text">Infuse</span> </div> </button>
            <button id="embyStellarPlayer" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="恒星播放器"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-StellarPlayer">　</i> <span class="button-text">恒星</span> </div> </button>
            <button id="embyMPV" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="MPV"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-MPV">　</i> <span class="button-text">MPV</span> </div> </button>
            <button id="embyDandanplay" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="弹弹Play"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-DandanPlay">　</i> <span class="button-text">弹弹Play</span> </div> </button>
            <button id="embyCopyUrl" type="button" class="detailButton emby-button emby-button-backdropfilter raised-backdropfilter detailButton-primary" title="复制串流地址"> <div class="detailButton-content"> <i class="md-icon detailButton-icon button-icon button-icon-left icon-Copy">　</i> <span class="button-text">复制链接</span> </div> </button>
        </div>`;
    }

    const ICONS = {
        'icon-PotPlayer': ['icon-PotPlayer.webp', '1.4em'],
        'icon-VLC': ['icon-VLC.webp', '1.3em'],
        'icon-IINA': ['icon-IINA.webp', '1.4em'],
        'icon-NPlayer': ['icon-NPlayer.webp', '1.3em'],
        'icon-MXPlayer': ['icon-MXPlayer.webp', '1.4em'],
        'icon-infuse': ['icon-infuse.webp', '1.4em'],
        'icon-StellarPlayer': ['icon-StellarPlayer.webp', '1.4em'],
        'icon-MPV': ['icon-MPV.webp', '1.4em'],
        'icon-DandanPlay': ['icon-DandanPlay.webp', '1.4em'],
        'icon-Copy': ['icon-Copy.webp', '1.4em']
    };

    // 播放器键 -> 按钮 DOM id（用于按 ENABLED_PLAYERS 过滤按钮）
    const PLAYER_ID_MAP = {
        Pot: 'embyPot',
        VLC: 'embyVlc',
        IINA: 'embyIINA',
        NPlayer: 'embyNPlayer',
        MX: 'embyMX',
        Infuse: 'embyInfuse',
        Stellar: 'embyStellarPlayer',
        MPV: 'embyMPV',
        Dandanplay: 'embyDandanplay',
        Copy: 'embyCopyUrl'
    };

    // 按 ENABLED_PLAYERS 移除不需要的按钮；空数组 = 全部保留
    function applyPlayerFilter(box) {
        if (!ENABLED_PLAYERS || !ENABLED_PLAYERS.length) { return; }
        Object.keys(PLAYER_ID_MAP).forEach(function (key) {
            if (ENABLED_PLAYERS.indexOf(key) === -1) {
                const el = box.querySelector('#' + PLAYER_ID_MAP[key]);
                if (el) { el.remove(); }
            }
        });
    }

    function init() {
        const mdb = mainDetailButtons();
        if (!mdb) { return false; }
        const old = document.getElementById(BOX_ID);
        if (old) { old.remove(); }
        mdb.insertAdjacentHTML('afterend', buttonHtml());

        const box = document.getElementById(BOX_ID);
        if (!box) { return false; }

        applyPlayerFilter(box);

        const bind = function (id, fn) {
            const el = box.querySelector('#' + id);
            if (el) { el.onclick = fn; }
        };
        bind('embyPot', embyPot);
        bind('embyVlc', embyVlc);
        bind('embyIINA', embyIINA);
        bind('embyNPlayer', embyNPlayer);
        bind('embyMX', embyMX);
        bind('embyInfuse', embyInfuse);
        bind('embyStellarPlayer', embyStellarPlayer);
        bind('embyMPV', embyMPV);
        bind('embyDandanplay', embyDandanplay);
        bind('embyCopyUrl', embyCopyUrl);

        Object.keys(ICONS).forEach(function (cls) {
            const el = box.querySelector('.' + cls);
            if (el) {
                const cfg = ICONS[cls];
                el.style.cssText += 'background: url(css/icons/' + cfg[0] + ') no-repeat;background-size: 100% 100%;font-size: ' + cfg[1];
            }
        });
        console.log('[extPlayer] 按钮已注入 itemId=' + currentItemId());
        return true;
    }

    async function getItemInfo() {
        const userId = (ApiClient._serverInfo && ApiClient._serverInfo.UserId) || ApiClient.getCurrentUserId();
        const itemId = currentItemId();
        if (!itemId) { return null; }
        let response = await ApiClient.getItem(userId, itemId);
        // 剧集 -> 下一集
        if (response.Type === 'Series') {
            const nextUp = await ApiClient.getNextUpEpisodes({ SeriesId: itemId, UserId: userId });
            if (nextUp && nextUp.Items && nextUp.Items.length) {
                return await ApiClient.getItem(userId, nextUp.Items[0].Id);
            }
            return null;
        }
        // 季 -> 第一集
        if (response.Type === 'Season') {
            const seasonItems = await ApiClient.getItems(userId, { parentId: itemId });
            if (seasonItems && seasonItems.Items && seasonItems.Items.length) {
                return await ApiClient.getItem(userId, seasonItems.Items[0].Id);
            }
            return null;
        }
        return response;
    }

    function getSeek(position) {
        position = Number(position) || 0;
        let ticks = position * 10000;
        const parts = [];
        let hours = ticks / 36e9;
        hours = Math.floor(hours);
        if (hours) { parts.push(hours); }
        let minutes = (ticks -= 36e9 * hours) / 6e8;
        ticks -= 6e8 * Math.floor(minutes);
        minutes = Math.floor(minutes);
        if (minutes < 10 && hours) { minutes = '0' + minutes; }
        parts.push(minutes);
        let seconds = ticks / 1e7;
        seconds = Math.floor(seconds);
        if (seconds < 10) { seconds = '0' + seconds; }
        parts.push(seconds);
        return parts.join(':');
    }

    function streamsOf(mediaSource) {
        return (mediaSource && mediaSource.MediaStreams) || [];
    }

    function getSubPath(mediaSource) {
        const selectSubtitles = scoped('select.selectSubtitles');
        const streams = streamsOf(mediaSource);
        let subTitlePath = '';
        if (selectSubtitles && Number(selectSubtitles.value) > 0) {
            const idx = streams.findIndex(function (m) { return m.Index == selectSubtitles.value && m.IsExternal; });
            if (idx > -1) {
                subTitlePath = '/' + mediaSource.Id + '/Subtitles/' + streams[idx].Index + '/Stream.' + streams[idx].Codec;
            }
        } else {
            let idx = streams.findIndex(function (m) { return m.Language === 'chi' && m.IsExternal; });
            if (idx < 0) { idx = streams.findIndex(function (m) { return m.IsExternal; }); }
            if (idx > -1) {
                subTitlePath = '/' + mediaSource.Id + '/Subtitles/' + streams[idx].Index + '/Stream.' + streams[idx].Codec;
            }
        }
        return subTitlePath;
    }

    // strm / 网盘直链判定
    function isRemoteSource(ms) {
        if (!ms) { return false; }
        const container = String(ms.Container || '').toLowerCase();
        const protocol = String(ms.Protocol || '').toLowerCase();
        return container === 'strm' || ms.IsRemote === true || protocol === 'http' || streamsOf(ms).length === 0;
    }

    function rewriteHost(u, base) {
        try {
            const a = new URL(u);
            const b = new URL(base);
            return b.origin + a.pathname + a.search;
        } catch (e) { return u; }
    }

    // 关键改造：strm 直接用 .strm 内写入的真实地址（MediaSources[].Path）
    // Emby 对 strm 的 /videos/{id}/stream 代理端点实测返回 500，走不通
    function buildStreamUrl(itemInfo, mediaSource, mediaSourceId) {
        const path = mediaSource.Path || '';
        if (isRemoteSource(mediaSource) && /^https?:\/\//i.test(path)) {
            return STRM_HOST_REWRITE ? rewriteHost(path, STRM_HOST_REWRITE) : path;
        }
        const container = mediaSource.Container || 'mkv';
        return ApiClient._serverAddress + '/emby/videos/' + itemInfo.Id +
            '/stream.' + container + '?api_key=' + ApiClient.accessToken() +
            '&Static=true&MediaSourceId=' + mediaSourceId;
    }

    function titleFromPath(p) {
        try {
            const u = new URL(p);
            const name = decodeURIComponent((u.pathname.split('/').filter(Boolean).pop()) || '');
            return name || 'video';
        } catch (e) {
            return String(p || '').split('/').pop() || 'video';
        }
    }

    // 直链（MediaSources[].Path）本身已经是合法的百分号编码，
    // 绝不能再对它做 encodeURI/encodeURIComponent —— 否则 %E5 会变成 %25E5，
    // AList/网盘会返回 200 + 一段错误 JSON 而不是视频流（播放器表现为"无服务"）。
    // 这里只兜底处理会破坏协议串的空格与双引号。
    function safeUrl(u) {
        return String(u || '').replace(/ /g, '%20').replace(/"/g, '%22');
    }

    // 标题：去掉视频扩展名 + 把双引号换成单引号（避免截断 potplayer 的 /title 参数）
    function safeTitle(t) {
        return String(t || '').replace(/\.[A-Za-z0-9]{2,5}$/, '').replace(/"/g, "'");
    }

    async function getEmbyMediaInfo() {
        const itemInfo = await getItemInfo();
        if (!itemInfo) { throw new Error('无法识别当前条目'); }
        const sources = itemInfo.MediaSources || [];
        if (!sources.length) { throw new Error('该条目没有可用媒体源'); }

        let mediaSourceId = sources[0].Id;
        const selectSource = scoped('select.selectSource');
        if (selectSource && selectSource.value) { mediaSourceId = selectSource.value; }
        const mediaSource = sources.find(function (m) { return m.Id === mediaSourceId; }) || sources[0];

        const subPath = getSubPath(mediaSource);
        const subUrl = subPath
            ? ApiClient._serverAddress + '/emby/videos/' + itemInfo.Id + subPath + '?api_key=' + ApiClient.accessToken()
            : '';
        const streamUrl = buildStreamUrl(itemInfo, mediaSource, mediaSourceId);
        const position = Math.max(0, Math.floor(((itemInfo.UserData || {}).PlaybackPositionTicks || 0) / 10000));

        console.log('[extPlayer] item=' + itemInfo.Id + ' type=' + itemInfo.Type +
            ' container=' + mediaSource.Container + ' remote=' + isRemoteSource(mediaSource));
        console.log('[extPlayer] streamUrl=' + streamUrl);
        if (subUrl) { console.log('[extPlayer] subUrl=' + subUrl); }

        return {
            streamUrl: streamUrl,
            subUrl: subUrl,
            title: titleFromPath(mediaSource.Path || ''),
            position: position
        };
    }

    async function embyPot() {
        const i = await getEmbyMediaInfo();
        let url = 'potplayer://' + safeUrl(i.streamUrl) + ' /current';
        if (i.subUrl) { url += ' /sub=' + safeUrl(i.subUrl); }
        url += ' /title="' + safeTitle(i.title) + '" /seek=' + getSeek(i.position);
        console.log(url);
        window.open(url, '_blank');
    }

    async function embyVlc() {
        const i = await getEmbyMediaInfo();
        const os = getOS();
        let url;
        if (os === 'windows') {
            url = 'vlc://' + safeUrl(i.streamUrl);
        } else if (os === 'ios') {
            // 查询参数式：这里必须用 encodeURIComponent（客户端会解一次）
            url = 'vlc-x-callback://x-callback-url/stream?url=' + encodeURIComponent(i.streamUrl) +
                (i.subUrl ? '&sub=' + encodeURIComponent(i.subUrl) : '');
        } else {
            url = 'intent:' + safeUrl(i.streamUrl) + '#Intent;package=org.videolan.vlc;type=video/*;' +
                (i.subUrl ? 'S.subtitles_location=' + safeUrl(i.subUrl) + ';' : '') +
                'S.title=' + safeTitle(i.title) + ';i.position=' + i.position + ';end';
        }
        console.log(url);
        window.open(url, '_blank');
    }

    async function embyIINA() {
        const i = await getEmbyMediaInfo();
        const url = 'iina://weblink?url=' + encodeURIComponent(i.streamUrl) + '&new_window=1';
        console.log(url);
        window.open(url, '_blank');
    }

    async function embyMX() {
        const i = await getEmbyMediaInfo();
        const url = 'intent:' + safeUrl(i.streamUrl) + '#Intent;package=com.mxtech.videoplayer.ad;S.title=' +
            safeTitle(i.title) + ';i.position=' + i.position + ';end';
        console.log(url);
        window.open(url, '_blank');
    }

    async function embyNPlayer() {
        const i = await getEmbyMediaInfo();
        const url = getOS() === 'macOS'
            ? 'nplayer-mac://weblink?url=' + encodeURIComponent(i.streamUrl) + '&new_window=1'   // 查询参数式，保持编码
            : 'nplayer-' + safeUrl(i.streamUrl);
        console.log(url);
        window.open(url, '_blank');
    }

    async function embyInfuse() {
        const i = await getEmbyMediaInfo();
        const url = 'infuse://x-callback-url/play?url=' + encodeURIComponent(i.streamUrl);
        console.log(url);
        window.open(url, '_blank');
    }

    async function embyStellarPlayer() {
        const i = await getEmbyMediaInfo();
        const url = 'stellar://play/' + safeUrl(i.streamUrl);
        console.log(url);
        window.open(url, '_blank');
    }

    async function embyMPV() {
        const i = await getEmbyMediaInfo();
        let url;
        if (getOS() === 'ios' || getOS() === 'android') {
            url = 'mpv://' + safeUrl(i.streamUrl);
        } else {
            const b64 = function (s) { return btoa(s).replace(/\//g, '_').replace(/\+/g, '-').replace(/=/g, ''); };
            url = 'mpv://play/' + b64(i.streamUrl);
            if (i.subUrl) { url += '/?subfile=' + b64(i.subUrl); }
        }
        console.log(url);
        window.open(url, '_blank');
    }

    async function embyDandanplay() {
        const i = await getEmbyMediaInfo();
        let url;
        if (getOS() === 'android') {
            url = 'intent:' + safeUrl(i.streamUrl) + '#Intent;package=com.xyoye.dandanplay;type=video/*;end';
        } else {
            // ddplay: 是不透明处理器，参数需整体编码（客户端解一次）
            url = 'ddplay:' + encodeURIComponent(i.streamUrl + '|filePath=' + safeTitle(i.title));
        }
        console.log(url);
        window.open(url, '_blank');
    }

    async function embyCopyUrl() {
        const i = await getEmbyMediaInfo();
        const textarea = document.createElement('textarea');
        document.body.appendChild(textarea);
        textarea.style.position = 'absolute';
        textarea.style.opacity = '0';
        textarea.value = i.streamUrl;
        textarea.select();
        let ok = false;
        try { ok = document.execCommand('copy', true); } catch (e) { ok = false; }
        document.body.removeChild(textarea);
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(i.streamUrl).catch(function () {});
            ok = true;
        }
        const btn = document.querySelector('#' + BOX_ID + ' #embyCopyUrl .button-text');
        if (btn && ok) {
            const old = btn.innerText;
            btn.innerText = '已复制';
            setTimeout(function () { btn.innerText = old; }, 1500);
        }
        console.log('[extPlayer] copy -> ' + i.streamUrl);
    }

    function getOS() {
        const u = navigator.userAgent;
        if (!!u.match(/compatible/i) || u.match(/Windows/i)) { return 'windows'; }
        if (!!u.match(/Macintosh/i) || u.match(/MacIntel/i)) { return 'macOS'; }
        if (!!u.match(/iphone/i) || u.match(/Ipad/i)) { return 'ios'; }
        if (u.match(/android/i)) { return 'android'; }
        if (u.match(/Ubuntu/i)) { return 'Ubuntu'; }
        return 'other';
    }

    function removeButtons() {
        const stale = document.getElementById(BOX_ID);
        if (stale) { stale.remove(); }
    }

    // ---- 巡检：详情页出现/重绘时补插按钮（Emby 会重绘 scroller，注入节点会被清掉）----
    function ensureButtons() {
        const itemId = currentItemId();
        if (!itemId) {
            removeButtons();
            return;
        }
        const mdb = mainDetailButtons();
        if (!mdb) { return; }
        const box = document.getElementById(BOX_ID);
        // 只有「同一父容器 + 紧跟在当前 mdb 之后」才算已就位；
        // 旧视图里的残留节点（不同 parent）一律重建，避免误判后永不注入
        if (box && box.parentNode === mdb.parentNode && box.previousElementSibling === mdb) { return; }
        if (!showFlag()) { return; }

        // 类型过滤：缓存命中就同步判定；未命中则查一次后判定（这期间不注入）
        if (Object.prototype.hasOwnProperty.call(itemTypeCache, itemId)) {
            if (!isPlayableType(itemTypeCache[itemId])) { removeButtons(); return; }
            init();
            return;
        }
        getItemType(itemId).then(function (type) {
            if (currentItemId() !== itemId) { return; }   // 期间已经切到别的条目
            if (!isPlayableType(type)) { removeButtons(); return; }
            init();
        });
    }

    document.addEventListener('viewbeforeshow', function (e) {
        const ctx = (e.detail && (e.detail.contextPath || e.detail.path)) || '';
        if (String(ctx).indexOf('/item') === 0) { setTimeout(ensureButtons, 60); }
    });
    document.addEventListener('itemshow', function () { setTimeout(ensureButtons, 60); });
    window.addEventListener('hashchange', function () { setTimeout(ensureButtons, 120); });
    setInterval(ensureButtons, WATCH_MS);
    ensureButtons();

    console.log('[extPlayer] strm 强制版已加载 v1.4.0');
})();
