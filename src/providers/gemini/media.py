"""Resolve image placeholders using provider media records rather than invented URLs."""

import re
from urllib.parse import urlsplit

PLACEHOLDER = re.compile(r"https?://googleusercontent\.com/image_generation_content/[^\s)\]<>]+")


def valid_media_url(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").endswith(".googleusercontent.com")


def _nested_value(data, path, default=None):
    current = data
    for key in path:
        if (
            isinstance(current, list)
            and isinstance(key, int)
            and -len(current) <= key < len(current)
        ):
            current = current[key]
        elif isinstance(current, dict) and isinstance(key, str):
            current = current.get(key, default)
        else:
            return default
        if current is None:
            return default
    return current


def _field(container, index, default=None):
    if not isinstance(container, list) or not container:
        return default
    value = container[index] if index < len(container) else None
    if value in (None, [], {}) or isinstance(value, dict):
        sparse = container[-1] if isinstance(container[-1], dict) else {}
        value = sparse.get(str(index + 1))
    return default if value in (None, [], {}) else value


def candidate_images(candidate):
    values = _nested_value(_field(_nested_value(candidate, [12]), 7), [0], [])
    images = []
    web = _field(_nested_value(candidate, [12]), 1, [])
    for index, item in enumerate(web if isinstance(web, list) else []):
        url = _nested_value(item, [0, 0, 0])
        if isinstance(url, str) and valid_media_url(url):
            images.append(
                {"url": url, "name": f"web-image-{index + 1}", "alias": f"web-image:{index}"}
            )
    for index, item in enumerate(values or []):
        url = _nested_value(item, [0, 3, 3])
        if isinstance(url, str) and valid_media_url(url):
            images.append(
                {
                    "url": url,
                    "media_type": _nested_value(item, [0, 3, 11]),
                    "name": _nested_value(item, [0, 3, 2]) or f"image-{index + 1}.png",
                    "alias": _nested_value(item, [1, 0])
                    or f"http://googleusercontent.com/image_generation_content/{index}",
                }
            )
    return images


def render_images(text, images):
    by_alias = {image["alias"]: image for image in images}

    def replace(match):
        image = by_alias.get(match.group())
        if image is None:
            return match.group()
        if match.start() and text[match.start() - 1] == "(":
            return image["url"]
        return f"[Image]({image['url']})"

    rendered = PLACEHOLDER.sub(replace, text or "")
    for image in images:
        if image["url"] not in rendered:
            rendered += f"\n\n[Image]({image['url']})"
    return rendered
