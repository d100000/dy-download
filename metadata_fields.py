"""元数据旁路的纯函数边界：只接收展示字段，不传递媒体线路或凭据。"""
import math
import re
import time
import unicodedata
from urllib.parse import urlsplit


_TRUNCATION_NOTICE = re.compile(r"(?:[.。…\s]*)(?:版本过低[，,\s]*升级后可展示全部信息|"
                                r"当前版本过低[，,\s]*请?升级(?:后)?(?:查看|展示)全部信息)[。.!！\s]*$")
_ELLIPSIS = re.compile(r"(?:\.{3}|…+)\s*$")
_PUBLIC_SUFFIXES = ("douyin.com", "iesdouyin.com", "douyinpic.com", "byteimg.com",
                    "ibyteimg.com", "pstatp.com", "toutiaoimg.com", "douyincdn.com")
METADATA_SOURCES = frozenset(("browser_dom", "browser_structured", "http_structured", "http_ssr"))


def text_value(value, limit=1000):
    if not isinstance(value, str):
        return ""
    value = "".join(c for c in value if unicodedata.category(c) not in ("Cc", "Cf", "Cs") or c.isspace())
    return re.sub(r"\s+", " ", value).strip()[:limit]


def is_truncated_title(title, obj=None):
    if isinstance(obj, dict) and any(obj.get(key) is True for key in
                                   ("is_truncated", "isTruncated", "desc_truncated")):
        return True
    return bool(isinstance(title, str) and (_ELLIPSIS.search(title) or _TRUNCATION_NOTICE.search(title)))


def title_prefix(title):
    """只移除已识别的尾部提示；不可用任意公共前缀覆盖原文。"""
    return _ELLIPSIS.sub("", _TRUNCATION_NOTICE.sub("", text_value(title, 10000))).strip()


def _number(value, maximum=10**15, positive=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or number < (1 if positive else 0) or number > maximum or not number.is_integer():
            return None
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return None


def _first(obj, *keys):
    for key in keys:
        value = obj.get(key)
        if value is not None and value != "":
            return value
    return None


def _public_url(value, author=False):
    if isinstance(value, dict):
        value = _first(value, "url_list", "urlList", "url")
    if isinstance(value, list):
        return next((url for item in value[:10] if (url := _public_url(item, author))), "")
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not any(host == suffix or host.endswith("." + suffix) for suffix in _PUBLIC_SUFFIXES)):
            return ""
        if author and (host not in ("www.douyin.com", "douyin.com") or not re.fullmatch(r"/user/[A-Za-z0-9_-]{1,256}/?", parsed.path)):
            return ""
        return value
    except ValueError:
        return ""


def sanitize_metadata(result):
    """规范化展示字段；身份和标题由调用者额外校验，所有媒体 URL 均丢弃。"""
    if not isinstance(result, dict):
        return {}
    out = {}
    for key, limit in (("author", 100), ("content", 10000)):
        value = text_value(result.get(key), limit)
        if value and not (key == "author" and value in ("未知作者", "unknown")):
            out[key] = value
    if "content" in out:
        raw = text_value(result.get("content"), 10001)
        out["content_status"] = "partial" if len(raw) > 10000 or is_truncated_title(raw) or result.get("content_status") == "partial" else "available"
    for key in ("avatar", "author_url", "cover"):
        value = _public_url(result.get(key), key == "author_url")
        if value:
            out[key] = value
    for key, maximum in (("create_time", 10**11), ("snapshot_at", 10**11), ("duration_ms", 30 * 86400000)):
        value = _number(result.get(key), maximum, positive=True)
        if value is not None:
            out[key] = value
    stats = result.get("stats")
    if isinstance(stats, dict):
        clean = {key: value for key in ("digg", "comment", "collect", "share")
                 if (value := _number(stats.get(key))) is not None}
        if clean:
            out["stats"] = clean
    video = result.get("video")
    if isinstance(video, dict):
        clean = {key: value for key in ("width", "height")
                 if (value := _number(video.get(key), 32768, positive=True)) is not None}
        if clean:
            out["video"] = clean
    tags = result.get("tags")
    if isinstance(tags, list):
        clean = list(dict.fromkeys(text_value(tag, 100) for tag in tags[:100] if isinstance(tag, str)))
        clean = [tag for tag in clean if tag]
        if clean:
            out["tags"] = clean
    return out


def extract_metadata(obj):
    """调用方先核验作品 ID；这里仅处理该作品对象内的公开字段。"""
    if not isinstance(obj, dict):
        return {}
    author = obj.get("author") if isinstance(obj.get("author"), dict) else {}
    video = obj.get("video") if isinstance(obj.get("video"), dict) else {}
    stats = _first(obj, "statistics", "stats")
    stats = stats if isinstance(stats, dict) else {}
    result = {
        "content": _first(obj, "full_desc", "fullDesc", "desc", "description", "title"),
        "author": _first(author, "nickname", "name") or (obj.get("author") if isinstance(obj.get("author"), str) else ""),
        "avatar": _first(author, "avatar_larger", "avatar_medium", "avatar_thumb", "avatar"),
        "create_time": _first(obj, "create_time", "createTime"),
        "cover": _first(video, "origin_cover", "cover", "originCover") or _first(obj, "cover", "thumbnailUrl"),
        "video": {key: video.get(key) for key in ("width", "height")},
        "stats": {key: _first(stats, key + "_count", key) for key in ("digg", "comment", "collect", "share")},
    }
    sec_uid = _first(author, "sec_uid", "secUid")
    if isinstance(sec_uid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", sec_uid):
        result["author_url"] = "https://www.douyin.com/user/" + sec_uid
    duration = _first(video, "duration_ms", "durationMs", "duration")
    if duration is None:
        duration = _first(obj, "duration_ms", "durationMs")
    result["duration_ms"] = duration
    if is_truncated_title(result.get("content"), obj):
        result["content_status"] = "partial"
    tags = _first(obj, "text_extra", "textExtra", "cha_list", "tags")
    if isinstance(tags, list):
        result["tags"] = [(_first(tag, "hashtag_name", "cha_name", "name") if isinstance(tag, dict) else tag)
                          for tag in tags[:100]]
    out = sanitize_metadata(result)
    if out:
        out["snapshot_at"] = int(time.time())
    return out


def metadata_fields_present(result):
    """字段级存在性；任务 ready 与全文/字段齐全互不混用。"""
    fields = [key for key in ("title", "content", "author", "avatar", "author_url", "cover")
              if isinstance(result.get(key), str) and result[key].strip()
              and result[key].strip().casefold() not in ("未知作者", "无标题", "（无标题）", "(无标题)", "unknown", "untitled", "no title")]
    fields += [key for key in ("create_time", "duration_ms") if _number(result.get(key), positive=True) is not None]
    stats = result.get("stats") if isinstance(result.get("stats"), dict) else {}
    video = result.get("video") if isinstance(result.get("video"), dict) else {}
    fields += ["stats." + key for key in ("digg", "comment", "collect", "share")
               if _number(stats.get(key)) is not None]
    fields += ["video." + key for key in ("width", "height")
               if _number(video.get(key), 32768, positive=True) is not None]
    if isinstance(result.get("tags"), list) and result["tags"]:
        fields.append("tags")
    return fields
