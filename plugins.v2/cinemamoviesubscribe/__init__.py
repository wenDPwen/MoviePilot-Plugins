import datetime
import re
import traceback
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.plugins import _PluginBase
from app.schemas import ExistMediaInfo, NotificationType
from app.schemas.types import MediaType

lock = Lock()


class CinemaMovieSubscribe(_PluginBase):
    """
    院线电影订阅。

    从 TMDB 发现中国大陆、香港、澳门、台湾院线上映电影，按上映日期范围过滤，
    若 Emby/媒体库中不存在且未订阅，则自动创建电影订阅。
    """

    plugin_name = "院线电影订阅"
    plugin_desc = "自动发现国内、香港、澳门、台湾院线上映电影，媒体库不存在时自动添加电影订阅。"
    plugin_icon = "https://raw.githubusercontent.com/wenDPwen/MoviePilot-Plugins/main/icons/movie.jpg"
    plugin_version = "1.1.7"
    plugin_author = "wen"
    author_url = "https://github.com/wenDPwen"
    author_icon = "https://raw.githubusercontent.com/wenDPwen/MoviePilot-Plugins/main/icons/author.jpg"
    plugin_author_icon = author_icon
    plugin_config_prefix = "cinemamoviesubscribe_"
    plugin_order = 31
    auth_level = 2
    _processed_key = "processed_keys"
    _processed_actions = ["subscribed", "exists", "sub_exists"]

    _scheduler: Optional[BackgroundScheduler] = None

    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _clear: bool = False
    _clearflag: bool = False
    _cron: str = "0 9 * * *"
    _regions: List[str] = ["CN", "HK", "MO", "TW"]
    _genres: List[str] = []
    _start_date: str = ""
    _end_date: str = ""
    _lookback_days: int = 30
    _lookahead_days: int = 30
    _max_pages: int = 3
    _min_vote_count: int = 0
    _min_popularity: float = 0
    _include: str = ""
    _exclude: str = "网络电影|网大|爱奇艺|优酷|腾讯视频|芒果TV|B站|哔哩哔哩|西瓜视频|抖音|线上首映|网络首映|流媒体首映|平台上线|独播上线|上线播出"
    _include_limited: bool = False
    _exclude_rerelease: bool = True
    _max_release_year_gap: int = 2
    _emby_servers: List[str] = []

    _region_names = {
        "CN": "中国大陆",
        "HK": "中国香港",
        "MO": "中国澳门",
        "TW": "中国台湾",
    }

    _release_type_names = {
        1: "首映",
        2: "院线点映",
        3: "院线上映",
        4: "数字发行",
        5: "实体发行",
        6: "电视播出",
    }

    _genre_names = {
        28: "动作",
        12: "冒险",
        16: "动画/动漫",
        35: "喜剧",
        80: "犯罪",
        99: "纪录",
        18: "剧情",
        10751: "家庭",
        14: "奇幻",
        36: "历史",
        27: "恐怖",
        10402: "音乐",
        9648: "悬疑",
        10749: "爱情",
        878: "科幻",
        10770: "电视电影",
        53: "惊悚",
        10752: "战争",
        37: "西部",
    }

    def init_plugin(self, config: dict = None):
        self.stop_service()

        config = config or {}
        if config:
            self.__validate_and_fix_config(config=config)
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._clear = bool(config.get("clear"))
            self._cron = config.get("cron") or "0 9 * * *"
            self._regions = self.__as_list(config.get("regions")) or ["CN", "HK", "MO", "TW"]
            self._genres = self.__valid_genres(config.get("genres"))
            self._start_date = config.get("start_date") or ""
            self._end_date = config.get("end_date") or ""
            self._lookback_days = self.__to_int(config.get("lookback_days"), 30)
            self._lookahead_days = self.__to_int(config.get("lookahead_days"), 30)
            self._max_pages = max(1, min(self.__to_int(config.get("max_pages"), 3), 20))
            self._min_vote_count = max(0, self.__to_int(config.get("min_vote_count"), 0))
            self._min_popularity = max(0, self.__to_float(config.get("min_popularity"), 0))
            self._include = config.get("include") or ""
            self._exclude = config.get("exclude") or self._exclude
            self._include_limited = bool(config.get("include_limited"))
            self._exclude_rerelease = bool(config.get("exclude_rerelease", True))
            self._max_release_year_gap = max(0, min(self.__to_int(config.get("max_release_year_gap"), 2), 20))
            self._emby_servers = self.__as_list(config.get("emby_servers"), upper=False)

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("院线电影订阅服务启动，立即运行一次")
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
                "summary": "删除院线电影订阅历史记录",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        if self._cron:
            return [{
                "id": "CinemaMovieSubscribe",
                "name": "院线电影订阅服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.check,
                "kwargs": {},
            }]
        return [{
            "id": "CinemaMovieSubscribe",
            "name": "院线电影订阅服务",
            "trigger": "interval",
            "func": self.check,
            "kwargs": {"hours": 24},
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
                                        "placeholder": "5位cron表达式，留空每24小时",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "regions",
                                        "label": "上映地区",
                                        "multiple": True,
                                        "chips": True,
                                        "items": [
                                            {"title": "中国大陆", "value": "CN"},
                                            {"title": "中国香港", "value": "HK"},
                                            {"title": "中国澳门", "value": "MO"},
                                            {"title": "中国台湾", "value": "TW"},
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
                                    "component": "VSelect",
                                    "props": {
                                        "model": "genres",
                                        "label": "订阅类型限制",
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "items": self.__get_genre_items(),
                                        "placeholder": "留空不限制；只订阅动漫请选择 动画/动漫",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col_text("start_date", "开始日期", "YYYY-MM-DD，留空按回溯天数", 6),
                            self.__col_text("end_date", "结束日期", "YYYY-MM-DD，留空按未来天数", 6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col_text("lookback_days", "回溯天数", "未填开始日期时生效，如：30", 3),
                            self.__col_text("lookahead_days", "未来天数", "未填结束日期时生效，如：30", 3),
                            self.__col_text("max_pages", "每地区页数", "1-20，建议 3", 3),
                            self.__col_text("min_vote_count", "最低评价数", "0 表示不限制", 3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col_text("min_popularity", "最低热度", "0 表示不限制", 6),
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
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col_text("include", "包含", "支持正则表达式，留空不限制", 6),
                            self.__col_text("exclude", "排除", "支持正则表达式，默认排除网大/流媒体首映词", 6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col_switch("include_limited", "包含点映/小规模院线", 4),
                            self.__col_switch("clear", "清理历史记录", 4),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "clear": False,
            "cron": "0 9 * * *",
            "regions": ["CN", "HK", "MO", "TW"],
            "genres": [],
            "start_date": "",
            "end_date": "",
            "lookback_days": 30,
            "lookahead_days": 30,
            "max_pages": 3,
            "min_vote_count": 0,
            "min_popularity": 0,
            "include": "",
            "exclude": self._exclude,
            "include_limited": False,
            "emby_servers": [],
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
        history_changed = False
        cards = []
        for item in history[:100]:
            poster = self.__history_poster(item)
            if poster and not item.get("poster"):
                item["poster"] = poster
                history_changed = True
            title = item.get("title") or "未知电影"
            cards.append({
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {
                        "component": "VDialogCloseBtn",
                        "props": {"innerClass": "absolute top-0 right-0"},
                        "events": {
                            "click": {
                                "api": "plugin/CinemaMovieSubscribe/delete_history",
                                "method": "get",
                                "params": {
                                    "key": item.get("key"),
                                    "apikey": settings.API_TOKEN,
                                },
                            }
                        },
                    },
                    {
                        "component": "div",
                        "props": {"class": "d-flex justify-space-start flex-nowrap flex-row"},
                        "content": [
                            {
                                "component": "div",
                                "content": [self.__poster_view(poster)],
                            },
                            {
                                "component": "div",
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "props": {"class": "pa-1 pe-8 break-words whitespace-break-spaces"},
                                        "text": title,
                                    },
                                    self.__history_text(
                                        f"订阅类型：{self.__history_type(item.get('media_type') or '电影', item.get('genres'))}"
                                    ),
                                    self.__history_text(f"上映地区：{item.get('region') or '-'}"),
                                    self.__history_text(f"上映时间：{item.get('release_date') or '-'}"),
                                    self.__history_text(f"加入时间：{item.get('time') or '-'}"),
                                ],
                            },
                        ],
                    },
                ],
            })

        if history_changed:
            self.save_data("history", history)

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
            logger.error(f"退出院线电影订阅插件失败：{str(err)}")

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
        with lock:
            logger.info("开始执行院线电影订阅 ...")
            stored_history: List[dict] = self.get_data("history") or []
            history: List[dict] = [] if self._clearflag else stored_history
            processed = self.__processed_keys(history=stored_history)

            start_date, end_date = self.__date_window()
            movies = self.__discover_movies(start_date=start_date, end_date=end_date)
            if not movies:
                logger.info("未发现符合条件的院线电影")
                self._clearflag = False
                self.save_data("history", history)
                self.__save_processed_keys(processed=processed)
                return

            stats = {
                "found": len(movies),
                "subscribed": 0,
                "exists": 0,
                "sub_exists": 0,
                "failed": 0,
                "skipped": 0,
            }

            subscribechain = SubscribeChain()
            for item in movies:
                try:
                    mediainfo: MediaInfo = item["mediainfo"]
                    key = str(mediainfo.tmdb_id)
                    if key in processed:
                        stats["skipped"] += 1
                        continue

                    exists, exists_message = self.__media_exists(mediainfo=mediainfo)
                    if exists:
                        stats["exists"] += 1
                        processed.add(key)
                        history.append(self.__history_item(
                            action="exists",
                            item=item,
                            message=exists_message,
                        ))
                        continue

                    meta = self.__meta_for_movie(mediainfo)
                    if subscribechain.exists(mediainfo=mediainfo, meta=meta):
                        stats["sub_exists"] += 1
                        processed.add(key)
                        history.append(self.__history_item(
                            action="sub_exists",
                            item=item,
                            message="电影订阅已存在",
                        ))
                        continue

                    sid, msg = subscribechain.add(
                        title=mediainfo.title,
                        year=mediainfo.year,
                        mtype=MediaType.MOVIE,
                        tmdbid=mediainfo.tmdb_id,
                        exist_ok=True,
                        username=self.plugin_name,
                        message=False,
                    )
                    if sid:
                        stats["subscribed"] += 1
                        processed.add(key)
                        history.append(self.__history_item(
                            action="subscribed",
                            item=item,
                            message=f"已添加电影订阅：{sid}",
                        ))
                        logger.info(f"{mediainfo.title_year} 已添加电影订阅：{sid}")
                    else:
                        stats["failed"] += 1
                        history.append(self.__history_item(
                            action="failed",
                            item=item,
                            message=msg or "添加订阅失败",
                        ))
                        logger.error(f"{mediainfo.title_year} 添加订阅失败：{msg}")
                except Exception as err:
                    stats["failed"] += 1
                    logger.error(f"处理院线电影失败：{str(err)} - {traceback.format_exc()}")

            self.save_data("history", history[-500:])
            self.__save_processed_keys(processed=processed)
            self._clearflag = False
            self.__notify_summary(stats=stats, start_date=start_date, end_date=end_date)
            logger.info(f"院线电影订阅执行完成：{stats}")

    def __discover_movies(self, start_date: datetime.date, end_date: datetime.date) -> List[dict]:
        tmdb = TmdbApi()
        found: Dict[int, dict] = {}

        for region in self._regions:
            region = region.upper()
            for page in range(1, self._max_pages + 1):
                params = {
                    "region": region,
                    "with_release_type": self.__release_type_query(),
                    "release_date.gte": start_date.strftime("%Y-%m-%d"),
                    "release_date.lte": end_date.strftime("%Y-%m-%d"),
                    "sort_by": "primary_release_date.desc",
                    "include_adult": "false",
                    "page": page,
                }
                items = tmdb.discover_movies(params=params) or []
                if not items:
                    break
                for movie in items:
                    tmdbid = movie.get("id")
                    if not tmdbid or tmdbid in found:
                        continue
                    detail = tmdb.get_info(mtype=MediaType.MOVIE, tmdbid=tmdbid)
                    if not detail:
                        continue
                    mediainfo = MediaInfo(tmdb_info=detail)
                    if not self.__match_genre(detail=detail, mediainfo=mediainfo):
                        continue
                    if not self.__match_keyword(mediainfo=mediainfo):
                        continue
                    if self._min_vote_count and (mediainfo.vote_count or 0) < self._min_vote_count:
                        continue
                    if self._min_popularity and (mediainfo.popularity or 0) < self._min_popularity:
                        continue
                    release = self.__matched_release(detail=detail, start_date=start_date,
                                                     end_date=end_date, regions=self._regions)
                    if not release:
                        continue
                    found[tmdbid] = {
                        "mediainfo": mediainfo,
                        "release": release,
                        "genres": self.__movie_genre_names(detail=detail, mediainfo=mediainfo),
                    }

        return list(found.values())

    def __matched_release(
            self,
            detail: dict,
            start_date: datetime.date,
            end_date: datetime.date,
            regions: List[str],
    ) -> Optional[dict]:
        region_set = {region.upper() for region in regions}
        original_releases = []
        for result in detail.get("release_dates", {}).get("results", []) or []:
            region = result.get("iso_3166_1")
            if region not in region_set:
                continue
            region_releases = []
            for release in result.get("release_dates", []) or []:
                rtype = release.get("type")
                if rtype not in self.__allowed_release_types():
                    continue
                rdate = self.__parse_tmdb_datetime(release.get("release_date"))
                if not rdate:
                    continue
                candidate = {
                    "region": region,
                    "region_name": self._region_names.get(region, region),
                    "release_date": rdate.strftime("%Y-%m-%d"),
                    "type": rtype,
                    "type_name": self._release_type_names.get(rtype, str(rtype)),
                    "note": release.get("note") or "",
                }
                if self.__is_rerelease(detail=detail, release=candidate):
                    title = detail.get("title") or detail.get("name") or detail.get("id")
                    logger.info(
                        f"{title} 命中复映/重映发行记录，忽略："
                        f"{candidate.get('region_name')} {candidate.get('release_date')} "
                        f"{candidate.get('note') or ''}"
                    )
                    continue
                region_releases.append(candidate)
            original_releases.extend(self.__first_original_releases(region_releases))

        matched = []
        for item in original_releases:
            release_date = self.__parse_date(item.get("release_date") or "")
            if release_date and start_date <= release_date <= end_date:
                matched.append(item)
        if not matched:
            return None
        matched.sort(key=lambda item: item.get("release_date") or "")
        return matched[0]

    @staticmethod
    def __first_original_releases(releases: List[dict]) -> List[dict]:
        if not releases:
            return []
        originals: Dict[int, dict] = {}
        for item in sorted(releases, key=lambda item: (item.get("release_date") or "", item.get("type") or 0)):
            rtype = item.get("type")
            if rtype not in originals:
                originals[rtype] = item
        return list(originals.values())

    def __is_rerelease(self, detail: dict, release: dict) -> bool:
        if not self._exclude_rerelease:
            return False

        note = release.get("note") or ""
        if note and re.search(
            r"重映|复映|重发|重上|重制|修复|周年|纪念|re[-\s]?release|rerelease|restor|remaster|anniversary|4k",
            note,
            re.IGNORECASE,
        ):
            return True

        original_date = self.__parse_date(detail.get("release_date") or "")
        current_date = self.__parse_date(release.get("release_date") or "")
        if not self._max_release_year_gap or not original_date or not current_date:
            return False
        return current_date.year - original_date.year > self._max_release_year_gap

    def __media_exists(self, mediainfo: MediaInfo) -> Tuple[bool, str]:
        servers = self._emby_servers or [None]
        for server in servers:
            exist_info: Optional[ExistMediaInfo] = self.chain.media_exists(mediainfo=mediainfo, server=server)
            if exist_info:
                server_name = server or "默认媒体服务器"
                return True, f"{server_name} 已存在 TMDB:{mediainfo.tmdb_id}"
        return False, ""

    @staticmethod
    def __meta_for_movie(mediainfo: MediaInfo):
        from app.core.metainfo import MetaInfo
        meta = MetaInfo(mediainfo.title)
        meta.type = MediaType.MOVIE
        meta.year = mediainfo.year
        return meta

    def __history_item(self, action: str, item: dict, message: str) -> dict:
        mediainfo: MediaInfo = item["mediainfo"]
        release = item.get("release") or {}
        return {
            "action": action,
            "key": str(mediainfo.tmdb_id),
            "title": mediainfo.title_year,
            "tmdbid": mediainfo.tmdb_id,
            "year": mediainfo.year,
            "media_type": MediaType.MOVIE.value,
            "poster": self.__poster_image(mediainfo),
            "release_date": release.get("release_date"),
            "region": release.get("region_name") or release.get("region"),
            "release_type": release.get("type_name"),
            "release_note": release.get("note"),
            "genres": "、".join(item.get("genres") or []),
            "message": message,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def __processed_keys(self, history: List[dict]) -> set:
        processed = {str(item) for item in (self.get_data(self._processed_key) or []) if str(item).strip()}
        processed.update(
            str(item.get("key")) for item in history
            if item.get("action") in self._processed_actions and item.get("key")
        )
        return processed

    def __save_processed_keys(self, processed: set):
        self.save_data(
            self._processed_key,
            sorted(processed, key=lambda item: (0, int(item)) if item.isdigit() else (1, item)),
        )

    @staticmethod
    def __poster_image(mediainfo: MediaInfo) -> str:
        try:
            return mediainfo.get_poster_image() or ""
        except Exception:
            return getattr(mediainfo, "poster_path", None) or ""

    def __history_poster(self, item: dict) -> str:
        poster = item.get("poster") or ""
        if poster:
            return poster

        tmdbid = item.get("tmdbid") or item.get("key")
        if not tmdbid:
            return ""
        try:
            detail = TmdbApi().get_info(mtype=MediaType.MOVIE, tmdbid=tmdbid)
            if not detail:
                return ""
            return self.__poster_image(MediaInfo(tmdb_info=detail))
        except Exception as err:
            logger.warning(f"获取院线电影历史海报失败：TMDB:{tmdbid} - {str(err)}")
            return ""

    @staticmethod
    def __poster_view(poster: str) -> dict:
        content = [{
            "component": "VImg",
            "props": {
                "src": poster,
                "height": 120,
                "width": 80,
                "aspect-ratio": "2/3",
                "class": "object-cover shadow ring-gray-500",
                "cover": True,
            },
        }]
        if poster:
            content.append({
                "component": "VDialog",
                "props": {"activator": "parent", "max-width": 520},
                "content": [{
                    "component": "VCard",
                    "content": [
                        {"component": "VDialogCloseBtn"},
                        {
                            "component": "VImg",
                            "props": {
                                "src": poster,
                                "max-height": "80vh",
                                "width": "100%",
                            },
                        },
                    ],
                }],
            })
        return {
            "component": "div",
            "props": {"class": "cursor-pointer"},
            "content": content,
        }

    @staticmethod
    def __history_type(media_type: str, genres: str = "") -> str:
        genres = CinemaMovieSubscribe.__display_genres(genres)
        return f"{media_type}-{genres}" if genres else media_type

    @staticmethod
    def __display_genres(genres: str = "") -> str:
        if not genres:
            return ""
        names = []
        for item in str(genres).replace(",", "、").split("、"):
            name = item.strip().replace("动画/动漫", "动漫")
            if name and name not in names:
                names.append(name)
        return "、".join(names)

    @staticmethod
    def __history_text(text: str) -> dict:
        return {
            "component": "VCardText",
            "props": {"class": "pa-0 px-2"},
            "text": text,
        }

    def __notify_summary(self, stats: dict, start_date: datetime.date, end_date: datetime.date):
        if not self._notify:
            return
        text = (
            f"上映日期：{start_date} 至 {end_date}\n"
            f"发现电影：{stats.get('found', 0)}\n"
            f"新增订阅：{stats.get('subscribed', 0)}\n"
            f"媒体库已存在：{stats.get('exists', 0)}\n"
            f"订阅已存在：{stats.get('sub_exists', 0)}\n"
            f"处理失败：{stats.get('failed', 0)}\n"
            f"历史跳过：{stats.get('skipped', 0)}"
        )
        self.post_message(mtype=NotificationType.Plugin, title=self.plugin_name, text=text)

    def __date_window(self) -> Tuple[datetime.date, datetime.date]:
        today = datetime.datetime.now(tz=pytz.timezone(settings.TZ)).date()
        start_date = self.__parse_date(self._start_date) or (today - datetime.timedelta(days=self._lookback_days))
        end_date = self.__parse_date(self._end_date) or (today + datetime.timedelta(days=self._lookahead_days))
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "clear": self._clear,
            "cron": self._cron,
            "regions": self._regions,
            "genres": self._genres,
            "start_date": self._start_date,
            "end_date": self._end_date,
            "lookback_days": self._lookback_days,
            "lookahead_days": self._lookahead_days,
            "max_pages": self._max_pages,
            "min_vote_count": self._min_vote_count,
            "min_popularity": self._min_popularity,
            "include": self._include,
            "exclude": self._exclude,
            "include_limited": self._include_limited,
            "exclude_rerelease": self._exclude_rerelease,
            "max_release_year_gap": self._max_release_year_gap,
            "emby_servers": self._emby_servers,
        })

    def __allowed_release_types(self) -> List[int]:
        return [2, 3] if self._include_limited else [3]

    def __release_type_query(self) -> str:
        return "|".join(str(item) for item in self.__allowed_release_types())

    def __validate_and_fix_config(self, config: dict):
        for field in ["start_date", "end_date"]:
            value = config.get(field)
            if value and not self.__parse_date(str(value)):
                self.systemmessage.put(f"{field} 日期格式错误：{value}，应为 YYYY-MM-DD", title=self.plugin_name)
                config[field] = ""
        for field in ["include", "exclude"]:
            pattern = config.get(field)
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as err:
                    self.systemmessage.put(f"{field} 正则表达式错误：{err}", title=self.plugin_name)
                    config[field] = ""

    def __match_genre(self, detail: dict, mediainfo: MediaInfo) -> bool:
        selected = {self.__to_int(item) for item in self._genres}
        selected = {item for item in selected if item in self._genre_names}
        if not selected:
            return True

        movie_genres = self.__movie_genre_ids(detail=detail, mediainfo=mediainfo)
        if not movie_genres:
            logger.info(f"{mediainfo.title_year} 未获取到类型信息，跳过")
            return False
        if selected.intersection(movie_genres):
            return True

        selected_names = "、".join(self._genre_names.get(item, str(item)) for item in sorted(selected))
        movie_names = "、".join(self._genre_names.get(item, str(item)) for item in sorted(movie_genres))
        logger.info(f"{mediainfo.title_year} 类型为 {movie_names or '-'}，不符合订阅类型限制：{selected_names}")
        return False

    def __movie_genre_ids(self, detail: dict, mediainfo: MediaInfo) -> set:
        genre_ids = set()
        for item in detail.get("genre_ids") or []:
            genre_id = self.__to_int(item)
            if genre_id:
                genre_ids.add(genre_id)
        for item in detail.get("genres") or []:
            genre_id = self.__to_int(item.get("id") if isinstance(item, dict) else item)
            if genre_id:
                genre_ids.add(genre_id)

        media_genre_ids = getattr(mediainfo, "genre_ids", None) or []
        for item in media_genre_ids:
            genre_id = self.__to_int(item)
            if genre_id:
                genre_ids.add(genre_id)
        return genre_ids

    def __movie_genre_names(self, detail: dict, mediainfo: MediaInfo) -> List[str]:
        names = []
        for item in sorted(self.__movie_genre_ids(detail=detail, mediainfo=mediainfo)):
            name = self._genre_names.get(item)
            if name:
                names.append(name)
        return names

    def __match_keyword(self, mediainfo: MediaInfo) -> bool:
        text = " ".join(str(item or "") for item in [
            mediainfo.title,
            mediainfo.en_title,
            mediainfo.original_title,
            mediainfo.overview,
            mediainfo.tagline,
            ",".join(mediainfo.names or []),
        ])
        if self._include and not re.search(self._include, text, re.IGNORECASE):
            logger.info(f"{mediainfo.title_year} 不符合包含规则")
            return False
        if self._exclude and re.search(self._exclude, text, re.IGNORECASE):
            logger.info(f"{mediainfo.title_year} 命中排除规则，跳过")
            return False
        return True

    @staticmethod
    def __parse_date(value: str) -> Optional[datetime.date]:
        if not value:
            return None
        try:
            return datetime.datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def __parse_tmdb_datetime(value: str) -> Optional[datetime.date]:
        if not value:
            return None
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def __as_list(value, upper: bool = True) -> List[str]:
        if not value:
            return []

        def normalize(item) -> str:
            text = str(item).strip()
            return text.upper() if upper else text

        if isinstance(value, list):
            return [normalize(item) for item in value if str(item).strip()]
        return [normalize(item) for item in str(value).split(",") if item.strip()]

    def __valid_genres(self, value) -> List[str]:
        genres = []
        for item in self.__as_list(value):
            genre_id = self.__to_int(item)
            if genre_id in self._genre_names:
                genres.append(str(genre_id))
        return genres

    @staticmethod
    def __to_int(value, default: int = 0) -> int:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __to_float(value, default: float = 0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

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

    def __get_genre_items(self) -> List[dict]:
        return [
            {"title": name, "value": str(genre_id)}
            for genre_id, name in self._genre_names.items()
        ]

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
