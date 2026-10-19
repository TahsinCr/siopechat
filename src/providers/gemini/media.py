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


def candidate_images(candidate):
    from gemini_webapi.utils.parsing import get_nested_value, get_rich_content_field

    values = get_nested_value(get_rich_content_field(candidate, 7), [0], [])
    images = []
    for index, item in enumerate(values or []):
        url = get_nested_value(item, [0, 3, 3])
        if isinstance(url, str) and valid_media_url(url):
            images.append(
                {
                    "url": url,
                    "media_type": get_nested_value(item, [0, 3, 11]),
                    "name": get_nested_value(item, [0, 3, 2]) or f"image-{index + 1}.png",
                    "alias": get_nested_value(item, [1, 0])
                    or f"http://googleusercontent.com/image_generation_content/{index}",
                }
            )
    return images


def sdk_images(output):
    images = []
    values = getattr(output, "images", None)
    for index, image in enumerate(values if isinstance(values, (list, tuple)) else []):
        url = getattr(image, "url", None)
        if isinstance(url, str) and valid_media_url(url):
            images.append(
                {
                    "url": url,
                    "name": f"image-{index + 1}",
                    "alias": getattr(image, "image_id", None)
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
