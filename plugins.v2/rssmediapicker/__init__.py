import datetime
import re
import traceback
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.download import DownloadChain
from app.core.config import settings
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.metainfo import MetaInfo
from app.helper.mediaserver import MediaServerHelper
from app.helper.rss import RssHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ExistMediaInfo, NotificationType
from app.schemas.types import MediaType, SystemConfigKey

lock = Lock()


class RssMediaPicker(_PluginBase):
    """
    RSS资源择优下载。

    拉取RSS后先识别媒体信息，再按 TMDB/季/集 归组：
    - 媒体库已存在时跳过；
    - 媒体库不存在时，每组按配置策略只选一个种子推送给下载器。
    """

    plugin_name = "RSS资源择优下载"
    plugin_desc = "从RSS中识别影视资源，检查Emby媒体库是否已存在，并按大小与关键词策略择优下载。"
    plugin_icon = "https://github.com/wenDPwen.png"
    plugin_version = "1.0.0"
    plugin_author = "wen"
    author_url = "https://github.com/wenDPwen"
    plugin_config_prefix = "rssmediapicker_"
    plugin_order = 30
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None

    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _clear: bool = False
    _clearflag: bool = False
    _cron: str = "*/30 * * * *"
    _rss_urls: str = ""
    _include: str = ""
    _exclude: str = ""
    _size_range: str = ""
    _proxy: bool = False
    _use_strategy: bool = False
    _filter_rule: bool = False
    _size_strategy: str = "first"
    _emby_servers: List[str] = []
    _downloader: str = ""
    _save_path: str = ""

    def init_plugin(self, config: dict = None):
        self.stop_service()

        config = config or {}
        if config:
            self.__validate_and_fix_config(config=config)
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._clear = bool(config.get("clear"))
            self._cron = config.get("cron") or "*/30 * * * *"
            self._rss_urls = config.get("rss_urls") or ""
            self._include = config.get("include") or ""
            self._exclude = config.get("exclude") or ""
            self._size_range = config.get("size_range") or ""
            self._proxy = bool(config.get("proxy"))
            self._use_strategy = bool(config.get("use_strategy"))
            self._filter_rule = bool(config.get("filter_rule"))
            self._size_strategy = config.get("size_strategy") or "first"
            self._emby_servers = self.__as_list(config.get("emby_servers"))
            self._downloader = config.get("downloader") or ""
            self._save_path = config.get("save_path") or ""

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("RSS资源择优下载服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.check,
                trigger="date",
                run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
            )
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        if self._onlyonce or self._clear:
            self._onlyonce = False
            self._clearflag = self._clear
            self._clear = False
            self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除RSS资源择优下载历史记录",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        if self._cron:
            return [{
                "id": "RssMediaPicker",
                "name": "RSS资源择优下载服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.check,
                "kwargs": {},
            }]
        return [{
            "id": "RssMediaPicker",
            "name": "RSS资源择优下载服务",
            "trigger": "interval",
            "func": self.check,
            "kwargs": {"minutes": 30},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self.__col_switch("enabled", "启用插件", 4),
                            self.__col_switch("notify", "发送通知", 4),
                            self.__col_switch("onlyonce", "立即运行一次", 4),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VCronField",
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期",
                                        "placeholder": "5位cron表达式，留空每30分钟",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "size_strategy",
                                        "label": "选择策略",
                                        "items": [
                                            {"title": "RSS顺序第一个", "value": "first"},
                                            {"title": "优先大体积", "value": "largest"},
                                            {"title": "优先小体积", "value": "smallest"},
                                        ],
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "rss_urls",
                                        "label": "RSS地址",
                                        "rows": 4,
                                        "placeholder": "每行一个RSS地址",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col_text("include", "包含", "支持正则表达式", 6),
                            self.__col_text("exclude", "排除", "支持正则表达式", 6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col_text("size_range", "种子大小(GB)", "如：1-50，留空不限制", 6),
                            self.__col_text("save_path", "保存目录", "留空使用下载器默认目录", 6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "emby_servers",
                                        "label": "Emby服务器",
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "items": self.__get_emby_server_items(),
                                        "placeholder": "留空使用MP默认媒体库存在性判断",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "downloader",
                                        "label": "下载器",
                                        "clearable": True,
                                        "items": self.__get_downloader_items(),
                                        "placeholder": "留空使用默认下载器",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col_switch("proxy", "使用代理服务器", 3),
                            self.__col_switch("use_strategy", "使用择优规则", 3),
                            self.__col_switch("filter_rule", "使用订阅优先级规则", 3),
                            self.__col_switch("clear", "清理历史记录", 3),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "clear": False,
            "cron": "*/30 * * * *",
            "rss_urls": "",
            "include": "",
            "exclude": "",
            "size_range": "",
            "proxy": False,
            "use_strategy": False,
            "filter_rule": False,
            "size_strategy": "first",
            "emby_servers": [],
            "downloader": "",
            "save_path": "",
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        if not history:
            return [{
                "component": "div",
                "text": "暂无数据",
                "props": {"class": "text-center"},
            }]

        history = sorted(history, key=lambda item: item.get("time") or "", reverse=True)
        cards = []
        for item in history[:100]:
            title = item.get("title") or item.get("torrent_title") or "未知资源"
            action = item.get("action") or "-"
            size = self.__format_size(item.get("size") or 0)
            msg = item.get("message") or ""
            cards.append({
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {
                        "component": "VDialogCloseBtn",
                        "props": {"innerClass": "absolute top-0 right-0"},
                        "events": {
                            "click": {
                                "api": "plugin/RssMediaPicker/delete_history",
                                "method": "get",
                                "params": {
                                    "key": item.get("key"),
                                    "apikey": settings.API_TOKEN,
                                },
                            }
                        },
                    },
                    {
                        "component": "VCardTitle",
                        "props": {"class": "pa-2 pe-8 break-words whitespace-break-spaces"},
                        "text": title,
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-2 pt-0"},
                        "text": (
                            f"动作：{action} / 候选：{item.get('candidate_count', 1)} / "
                            f"大小：{size} / 时间：{item.get('time') or '-'}"
                        ),
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-2 pt-0 text-caption"},
                        "text": msg,
                    },
                ],
            })

        return [{
            "component": "div",
            "props": {"class": "grid gap-3 grid-info-card"},
            "content": cards,
        }]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error(f"退出RSS资源择优下载插件失败：{str(err)}")

    def delete_history(self, key: str = "", apikey: str = ""):
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        if not key:
            self.save_data("history", [])
            return schemas.Response(success=True, message="已清空历史记录")
        history = self.get_data("history") or []
        history = [item for item in history if item.get("key") != key]
        self.save_data("history", history)
        return schemas.Response(success=True, message="删除成功")

    def check(self):
        if not self._rss_urls:
            logger.info("RSS资源择优下载未设置RSS地址")
            return

        with lock:
            logger.info("开始执行RSS资源择优下载 ...")
            history: List[dict] = [] if self._clearflag else (self.get_data("history") or [])
            processed_keys = {
                item.get("key") for item in history
                if item.get("action") in ["downloaded", "exists"] and item.get("key")
            }

            candidates = self.__load_candidates(processed_keys=processed_keys)
            if not candidates:
                logger.info("RSS资源择优下载未获取到可处理候选资源")
                self._clearflag = False
                self.save_data("history", history)
                return

            grouped: Dict[str, List[dict]] = {}
            for candidate in candidates:
                grouped.setdefault(candidate["key"], []).append(candidate)

            stats = {
                "groups": len(grouped),
                "downloaded": 0,
                "exists": 0,
                "failed": 0,
                "skipped": 0,
            }

            downloadchain = DownloadChain()
            for key, group_items in grouped.items():
                if key in processed_keys:
                    stats["skipped"] += 1
                    continue

                selected = self.__select_candidate(group_items)
                meta: MetaInfo = selected["meta"]
                mediainfo: MediaInfo = selected["mediainfo"]
                torrentinfo: TorrentInfo = selected["torrentinfo"]
                title = selected["title"]

                exists, exists_message = self.__media_exists(meta=meta, mediainfo=mediainfo)
                if exists:
                    logger.info(f"{mediainfo.title_year} {meta.season_episode or ''} 已存在，跳过")
                    stats["exists"] += 1
                    history.append(self.__history_item(
                        action="exists",
                        key=key,
                        selected=selected,
                        group_items=group_items,
                        message=exists_message,
                    ))
                    continue

                download_id = downloadchain.download_single(
                    context=Context(
                        meta_info=meta,
                        media_info=mediainfo,
                        torrent_info=torrentinfo,
                    ),
                    downloader=self._downloader or None,
                    save_path=self._save_path or None,
                    username="RSS资源择优下载",
                    source=self.plugin_name,
                )
                if download_id:
                    stats["downloaded"] += 1
                    history.append(self.__history_item(
                        action="downloaded",
                        key=key,
                        selected=selected,
                        group_items=group_items,
                        message=f"已添加下载任务：{download_id}",
                    ))
                    logger.info(f"{title} 已推送下载器：{download_id}")
                else:
                    stats["failed"] += 1
                    history.append(self.__history_item(
                        action="failed",
                        key=key,
                        selected=selected,
                        group_items=group_items,
                        message="添加下载任务失败",
                    ))
                    logger.error(f"{title} 添加下载任务失败")

            self.save_data("history", history[-500:])
            self._clearflag = False
            self.__notify_summary(stats)
            logger.info(f"RSS资源择优下载完成：{stats}")

    def __load_candidates(self, processed_keys: set) -> List[dict]:
        candidates = []
        rss_urls = [url.strip() for url in self._rss_urls.splitlines() if url.strip()]
        for url in rss_urls:
            logger.info(f"开始刷新RSS：{self.__safe_url(url)}")
            results = RssHelper().parse(url, proxy=self._proxy)
            if results is None:
                logger.error(f"RSS链接已过期：{self.__safe_url(url)}")
                continue
            if results is False or not results:
                logger.error(f"未获取到RSS数据：{self.__safe_url(url)}")
                continue

            logger.info(f"RSS {self.__safe_url(url)} 获取到 {len(results)} 条资源")
            for result in results:
                try:
                    candidate = self.__build_candidate(result=result, source_url=url)
                    if not candidate:
                        continue
                    if candidate["key"] in processed_keys:
                        continue
                    candidates.append(candidate)
                except Exception as err:
                    logger.error(f"解析RSS资源失败：{str(err)} - {traceback.format_exc()}")
        return candidates

    def __build_candidate(self, result: dict, source_url: str) -> Optional[dict]:
        title = result.get("title") or ""
        description = result.get("description") or ""
        enclosure = result.get("enclosure") or result.get("link") or ""
        page_url = result.get("link") or enclosure
        size = float(result.get("size") or 0)
        pubdate = result.get("pubdate")

        if not title or not enclosure:
            return None

        match_text = f"{title} {description}"
        if not self.__match_rule(self._include, match_text, expected=True):
            logger.info(f"{title} 不符合包含规则")
            return None
        if not self.__match_rule(self._exclude, match_text, expected=False):
            logger.info(f"{title} 命中排除规则")
            return None
        if not self.__match_size(size):
            logger.info(f"{title} 种子大小不符合条件")
            return None

        meta = MetaInfo(title=title, subtitle=description)
        if not meta.name:
            logger.warning(f"{title} 未识别到有效标题")
            return None

        mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
        if not mediainfo:
            logger.warning(f"未识别到媒体信息：{title}")
            return None

        torrentinfo = TorrentInfo(
            title=title,
            description=description,
            enclosure=enclosure,
            page_url=page_url,
            size=size,
            pubdate=pubdate.strftime("%Y-%m-%d %H:%M:%S") if pubdate else None,
            site_proxy=self._proxy,
        )

        if self._filter_rule:
            filter_groups = self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
            filter_result = self.chain.filter_torrents(
                rule_groups=filter_groups,
                torrent_list=[torrentinfo],
                mediainfo=mediainfo,
            )
            if not filter_result:
                logger.info(f"{title} 不匹配订阅优先级规则")
                return None

        return {
            "key": self.__group_key(meta=meta, mediainfo=mediainfo),
            "title": title,
            "description": description,
            "size": size,
            "source": self.__safe_url(source_url),
            "meta": meta,
            "mediainfo": mediainfo,
            "torrentinfo": torrentinfo,
        }

    def __select_candidate(self, candidates: List[dict]) -> dict:
        if not candidates:
            raise ValueError("候选资源为空")
        if not self._use_strategy or self._size_strategy == "first":
            return candidates[0]
        if self._size_strategy == "largest":
            return max(candidates, key=lambda item: item.get("size") or 0)
        if self._size_strategy == "smallest":
            return min(candidates, key=lambda item: item.get("size") or 0)
        return candidates[0]

    def __media_exists(self, meta: MetaInfo, mediainfo: MediaInfo) -> Tuple[bool, str]:
        servers = self._emby_servers or [None]
        for server in servers:
            exist_info = self.chain.media_exists(mediainfo=mediainfo, server=server)
            if self.__exist_info_matches(meta=meta, mediainfo=mediainfo, exist_info=exist_info):
                server_name = server or "默认媒体服务器"
                return True, f"{server_name} 已存在 TMDB:{mediainfo.tmdb_id or '-'}"
        return False, ""

    def __exist_info_matches(
            self,
            meta: MetaInfo,
            mediainfo: MediaInfo,
            exist_info: Optional[ExistMediaInfo],
    ) -> bool:
        if not exist_info:
            return False
        if mediainfo.type == MediaType.MOVIE:
            return True

        season = self.__to_int(getattr(meta, "begin_season", None) or getattr(meta, "season", None) or mediainfo.season)
        episodes = [self.__to_int(ep) for ep in (getattr(meta, "episode_list", None) or []) if self.__to_int(ep)]
        seasons = self.__normalize_seasons(exist_info.seasons or {})

        if episodes and season:
            exists_eps = set(seasons.get(season) or [])
            return set(episodes).issubset(exists_eps)

        if season:
            exists_eps = set(seasons.get(season) or [])
            total_eps = set((mediainfo.seasons or {}).get(season) or [])
            if total_eps:
                return total_eps.issubset(exists_eps)
            return bool(exists_eps)

        return True

    def __group_key(self, meta: MetaInfo, mediainfo: MediaInfo) -> str:
        media_id = mediainfo.tmdb_id or mediainfo.douban_id or f"{mediainfo.title}_{mediainfo.year}"
        parts = [mediainfo.type.value if mediainfo.type else "未知", str(media_id)]

        if mediainfo.type == MediaType.TV:
            season = self.__to_int(
                getattr(meta, "begin_season", None) or getattr(meta, "season", None) or mediainfo.season
            )
            episodes = [self.__to_int(ep) for ep in (getattr(meta, "episode_list", None) or []) if self.__to_int(ep)]
            if season:
                parts.append(f"S{season}")
            if episodes:
                parts.append("E" + ",".join(str(ep) for ep in sorted(set(episodes))))

        return "|".join(parts)

    def __history_item(self, action: str, key: str, selected: dict, group_items: List[dict], message: str) -> dict:
        mediainfo: MediaInfo = selected["mediainfo"]
        meta: MetaInfo = selected["meta"]
        return {
            "action": action,
            "key": key,
            "title": f"{mediainfo.title_year} {meta.season_episode or ''}".strip(),
            "torrent_title": selected.get("title"),
            "type": mediainfo.type.value if mediainfo.type else "",
            "year": mediainfo.year,
            "tmdbid": mediainfo.tmdb_id,
            "size": selected.get("size") or 0,
            "candidate_count": len(group_items),
            "source": selected.get("source"),
            "message": message,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def __notify_summary(self, stats: dict):
        if not self._notify:
            return
        text = (
            f"本次处理媒体组：{stats.get('groups', 0)}\n"
            f"已下载：{stats.get('downloaded', 0)}\n"
            f"媒体库已存在：{stats.get('exists', 0)}\n"
            f"下载失败：{stats.get('failed', 0)}\n"
            f"历史跳过：{stats.get('skipped', 0)}"
        )
        self.post_message(mtype=NotificationType.Plugin, title=self.plugin_name, text=text)

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "clear": self._clear,
            "cron": self._cron,
            "rss_urls": self._rss_urls,
            "include": self._include,
            "exclude": self._exclude,
            "size_range": self._size_range,
            "proxy": self._proxy,
            "use_strategy": self._use_strategy,
            "filter_rule": self._filter_rule,
            "size_strategy": self._size_strategy,
            "emby_servers": self._emby_servers,
            "downloader": self._downloader,
            "save_path": self._save_path,
        })

    def __validate_and_fix_config(self, config: dict):
        size_range = config.get("size_range")
        if size_range and not self.__is_number_or_range(str(size_range)):
            self.systemmessage.put(f"种子大小设置错误：{size_range}", title=self.plugin_name)
            config["size_range"] = ""
        for field in ["include", "exclude"]:
            pattern = config.get(field)
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as err:
                    self.systemmessage.put(f"{field} 正则表达式错误：{err}", title=self.plugin_name)
                    config[field] = ""

    def __match_rule(self, pattern: str, text: str, expected: bool) -> bool:
        if not pattern:
            return True
        matched = bool(re.search(pattern, text or "", re.IGNORECASE))
        return matched if expected else not matched

    def __match_size(self, size: float) -> bool:
        if not self._size_range:
            return True
        if not size:
            return False
        sizes = [float(item) * 1024 ** 3 for item in self._size_range.split("-")]
        if len(sizes) == 1:
            return size >= sizes[0]
        return sizes[0] <= size <= sizes[1]

    @staticmethod
    def __is_number_or_range(value: str) -> bool:
        return bool(re.match(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$", value))

    @staticmethod
    def __normalize_seasons(seasons: dict) -> Dict[int, List[int]]:
        normalized = {}
        for season, episodes in seasons.items():
            season_no = RssMediaPicker.__to_int(season)
            if not season_no:
                continue
            normalized[season_no] = [
                ep for ep in [RssMediaPicker.__to_int(item) for item in (episodes or [])] if ep
            ]
        return normalized

    @staticmethod
    def __to_int(value) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def __as_list(value) -> List[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value).split(",") if item.strip()]

    @staticmethod
    def __safe_url(url: str) -> str:
        parsed = urlparse(url or "")
        if not parsed.netloc:
            return url or ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    @staticmethod
    def __format_size(size: float) -> str:
        try:
            size = float(size or 0)
        except (TypeError, ValueError):
            size = 0
        if size >= 1024 ** 3:
            return f"{size / 1024 ** 3:.2f} GB"
        if size >= 1024 ** 2:
            return f"{size / 1024 ** 2:.2f} MB"
        return f"{size:.0f} B"

    @staticmethod
    def __col_switch(model: str, label: str, md: int) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{
                "component": "VSwitch",
                "props": {"model": model, "label": label},
            }],
        }

    @staticmethod
    def __col_text(model: str, label: str, placeholder: str, md: int) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{
                "component": "VTextField",
                "props": {"model": model, "label": label, "placeholder": placeholder},
            }],
        }

    @staticmethod
    def __get_emby_server_items() -> List[dict]:
        try:
            configs = MediaServerHelper().get_configs().values()
            return [
                {
                    "title": f"{config.name} ({config.type})",
                    "value": config.name,
                }
                for config in configs
                if config.enabled and str(config.type).lower() == "emby"
            ]
        except Exception as err:
            logger.error(f"获取Emby服务器配置失败：{str(err)}")
            return []

    def __get_downloader_items(self) -> List[dict]:
        items = [{"title": "默认下载器", "value": ""}]
        try:
            downloaders = self.systemconfig.get(SystemConfigKey.Downloaders) or []
            items.extend([
                {
                    "title": f"{downloader.get('name')} ({downloader.get('type')})",
                    "value": downloader.get("name"),
                }
                for downloader in downloaders
                if downloader.get("enabled") and downloader.get("name")
            ])
        except Exception as err:
            logger.error(f"获取下载器配置失败：{str(err)}")
        return items
