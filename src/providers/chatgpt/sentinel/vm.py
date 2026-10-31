"""Virtual browser surface and strict register VM for Turnstile and SessionObserver."""

from __future__ import annotations

import json
import math
import random
import re
import time
import urllib.parse
import uuid
from collections.abc import Callable
from typing import Any

from ..errors import SentinelCompatibilityError
from .proof import (
    _UNDEFINED,
    _atob,
    _btoa,
    _format_js_date,
    _js_number,
    _json_value,
    _platform_for_user_agent,
    _xor_strings,
)
from .spec import DEFAULT_SENTINEL_SPEC, SentinelSpec


def _js_str(value: Any) -> str:
    if value is _UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _js_number(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ",".join("" if item in (None, _UNDEFINED) else _js_str(item) for item in value)
    if isinstance(value, dict):
        custom = value.get("__toString__")
        if callable(custom):
            return str(custom())
        return "[object Object]"
    return str(value)


def _js_to_number(value: Any) -> float:
    if value is _UNDEFINED:
        return math.nan
    if value is None or value is False or value == "":
        return 0.0
    if value is True:
        return 1.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0.0
        try:
            if stripped.startswith(("0x", "0X")):
                return float(int(stripped, 16))
            return float(stripped)
        except ValueError:
            return math.nan
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return 0.0
        if len(value) == 1:
            return _js_to_number(value[0])
    return math.nan


def _js_equals(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if left in (None, _UNDEFINED) and right in (None, _UNDEFINED):
        return True
    if type(left) is type(right):
        return left == right
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return float(left) == float(right)
    return left == right


class _JSTypeError(Exception):
    """Recoverable JS TypeError inside VM try-opcodes (13, 17, 22)."""


def _js_getprop(target: Any, prop: Any) -> Any:
    if target is None or target is _UNDEFINED:
        raise _JSTypeError(f"TypeError: Cannot read properties of {_js_str(target)}")
    key_str = _js_str(prop)

    if isinstance(target, dict):
        if prop in target:
            return target[prop]
        if key_str in target:
            return target[key_str]
        if key_str == "toString":
            return lambda *_args: _js_str(target)
        if key_str == "hasOwnProperty":
            return lambda k, *_args: _js_str(k) in target
        return _UNDEFINED

    if isinstance(target, (list, tuple)):
        if key_str == "length":
            return len(target)
        if isinstance(prop, (int, float)) and not isinstance(prop, bool) and int(prop) == prop:
            idx = int(prop)
            return target[idx] if 0 <= idx < len(target) else _UNDEFINED
        if key_str.isdigit():
            idx = int(key_str)
            return target[idx] if 0 <= idx < len(target) else _UNDEFINED
        return _js_bind_sequence_method(target, key_str)

    if isinstance(target, str):
        if key_str == "length":
            return len(target)
        if isinstance(prop, (int, float)) and not isinstance(prop, bool) and int(prop) == prop:
            idx = int(prop)
            return target[idx] if 0 <= idx < len(target) else _UNDEFINED
        if key_str.isdigit():
            idx = int(key_str)
            return target[idx] if 0 <= idx < len(target) else _UNDEFINED
        return _js_bind_string_method(target, key_str)

    if callable(target) and key_str == "bind":
        return lambda _this=None, *bound_args: lambda *call_args: target(*bound_args, *call_args)

    if hasattr(target, key_str):
        return getattr(target, key_str)
    return _UNDEFINED


def _js_bind_sequence_method(seq: list[Any] | tuple[Any, ...], name: str) -> Any:
    if name == "join":
        return lambda sep=",", *_a: _js_str(sep).join(
            "" if item in (None, _UNDEFINED) else _js_str(item) for item in seq
        )
    if name == "slice":
        return lambda start=0, end=None, *_a: list(
            seq[int(_js_to_number(start)) : (None if end is None else int(_js_to_number(end)))]
        )
    if name == "includes":
        return lambda item=None, *_a: item in seq
    if name == "indexOf":
        return lambda item=None, *_a: seq.index(item) if item in seq else -1
    if name == "push" and isinstance(seq, list):

        def _push(*items: Any) -> int:
            seq.extend(items)
            return len(seq)

        return _push
    if name == "pop" and isinstance(seq, list):
        return lambda *_a: seq.pop() if seq else _UNDEFINED
    if name == "shift" and isinstance(seq, list):
        return lambda *_a: seq.pop(0) if seq else _UNDEFINED
    if name == "map":
        return lambda fn, *_a: [fn(item, idx, seq) for idx, item in enumerate(seq)]
    if name == "filter":
        return lambda fn, *_a: [item for idx, item in enumerate(seq) if fn(item, idx, seq)]
    if name == "toString":
        return lambda *_a: _js_str(seq)
    return _UNDEFINED


def _js_bind_string_method(text: str, name: str) -> Any:
    if name == "charCodeAt":

        def _char_code_at(index: Any = 0, *_a: Any) -> int | float:
            idx = int(_js_to_number(index))
            return ord(text[idx]) if 0 <= idx < len(text) else math.nan

        return _char_code_at
    if name == "charAt":

        def _char_at(index: Any = 0, *_a: Any) -> str:
            idx = int(_js_to_number(index))
            return text[idx] if 0 <= idx < len(text) else ""

        return _char_at
    if name in ("slice", "substring"):

        def _slice(start: Any = 0, end: Any = None, *_a: Any) -> str:
            s = int(_js_to_number(start))
            e = None if end in (None, _UNDEFINED) else int(_js_to_number(end))
            return text[s:e]

        return _slice
    if name == "split":
        return lambda sep="", *_a: list(text) if sep == "" else text.split(_js_str(sep))
    if name == "indexOf":
        return lambda sub="", *_a: text.find(_js_str(sub))
    if name == "includes":
        return lambda sub="", *_a: _js_str(sub) in text
    if name == "toLowerCase":
        return lambda *_a: text.lower()
    if name == "toUpperCase":
        return lambda *_a: text.upper()
    if name == "trim":
        return lambda *_a: text.strip()
    if name == "replace":
        return lambda old="", new="", *_a: text.replace(_js_str(old), _js_str(new), 1)
    if name == "match":

        def _match(pattern: Any = "", *_a: Any) -> list[str] | None:
            found = re.search(_js_str(pattern), text)
            if not found:
                return None
            return [found.group(0), *found.groups()]

        return _match
    if name == "toString":
        return lambda *_a: text
    return _UNDEFINED


def _js_bind_method(target: Any, method_name: Any) -> Callable[..., Any]:
    member = _js_getprop(target, method_name)
    if not callable(member):
        raise _JSTypeError(f"TypeError: {_js_str(method_name)} is not a function")
    return member


def _build_virtual_window(
    *,
    user_agent: str,
    spec: SentinelSpec,
    time_origin_ms: float,
    started_ns: int,
) -> dict[str, Any]:
    storage_store: dict[str, str] = {}
    local_storage = {
        "getItem": lambda k, *_a: storage_store.get(_js_str(k)),
        "setItem": lambda k, v="", *_a: storage_store.__setitem__(_js_str(k), _js_str(v)),
        "removeItem": lambda k, *_a: storage_store.pop(_js_str(k), None),
        "clear": lambda *_a: storage_store.clear(),
    }
    location = {
        "href": "https://chatgpt.com/",
        "origin": "https://chatgpt.com",
        "protocol": "https:",
        "host": "chatgpt.com",
        "hostname": "chatgpt.com",
        "pathname": "/",
        "search": "",
        "hash": "",
        "__toString__": lambda: "https://chatgpt.com/",
    }
    navigator = {
        "userAgent": user_agent,
        "language": spec.language,
        "languages": list(spec.languages),
        "platform": _platform_for_user_agent(user_agent),
        "vendor": "Google Inc.",
        "webdriver": False,
        "maxTouchPoints": 0,
        "hardwareConcurrency": spec.hardware_concurrency,
        "deviceMemory": spec.device_memory,
        "storage": {
            "estimate": lambda *_a: {"quota": spec.heap_size_limit, "usage": 0},
        },
    }
    screen = {
        "width": 1920,
        "height": 1080,
        "availWidth": 1920,
        "availHeight": 1080,
        "availLeft": 0,
        "availTop": 0,
        "colorDepth": 24,
        "pixelDepth": 24,
    }
    history = {
        "length": 1,
        "state": None,
        "pushState": lambda *_a: None,
        "replaceState": lambda *_a: None,
    }
    scripts = [{"src": url, "getAttribute": lambda _name, *_a: None} for url in spec.scripts]
    current_script = scripts[-1] if scripts else {"src": spec.sdk_url}

    def _create_element(tag: Any = "div", *_a: Any) -> dict[str, Any]:
        return {
            "tagName": _js_str(tag).upper(),
            "style": {},
            "src": "",
            "innerText": "",
            "appendChild": lambda child, *_args: child,
            "removeChild": lambda child, *_args: child,
            "remove": lambda *_args: None,
            "setAttribute": lambda *_args: None,
            "getAttribute": lambda *_args: None,
            "getBoundingClientRect": lambda *_args: {
                "x": 0,
                "y": 0,
                "width": 0,
                "height": 0,
                "top": 0,
                "right": 0,
                "bottom": 0,
                "left": 0,
            },
            "getContext": lambda *_args: None,
        }

    document = {
        "location": location,
        "cookie": "",
        "scripts": scripts,
        "currentScript": current_script,
        "documentElement": _create_element("html"),
        "head": _create_element("head"),
        "body": _create_element("body"),
        "createElement": _create_element,
        "addEventListener": lambda *_a: None,
        "removeEventListener": lambda *_a: None,
        "querySelector": lambda *_a: None,
    }
    performance = {
        "timeOrigin": time_origin_ms,
        "memory": {
            "jsHeapSizeLimit": spec.heap_size_limit,
            "totalJSHeapSize": 20_971_520,
            "usedJSHeapSize": 10_485_760,
        },
        "now": lambda *_a: round(max(0.0, (time.perf_counter_ns() - started_ns) / 1_000_000.0), 3),
    }
    crypto = {
        "randomUUID": lambda *_a: str(uuid.uuid4()),
        "getRandomValues": lambda arr, *_a: arr,
    }

    def _reflect_set(target: Any, prop: Any, value: Any, *_a: Any) -> bool:
        if isinstance(target, dict):
            target[_js_str(prop)] = value
            return True
        if isinstance(target, list):
            idx = _js_to_number(prop)
            if math.isfinite(idx) and int(idx) == idx and 0 <= int(idx) < len(target):
                target[int(idx)] = value
                return True
        return False

    def _reflect_construct(ctor: Any, args: Any = (), *_a: Any) -> Any:
        if callable(ctor):
            seq = list(args) if isinstance(args, (list, tuple)) else []
            return ctor(*seq)
        return {}

    reflect_ns = {
        "set": _reflect_set,
        "get": lambda target, prop, *_a: _js_getprop(target, prop),
        "has": lambda target, prop, *_a: isinstance(target, dict) and _js_str(prop) in target,
        "construct": _reflect_construct,
    }
    object_ns = {
        "create": lambda _proto=None, *_a: {},
        "assign": lambda target, *sources: (
            target.update(
                {k: v for src in sources if isinstance(src, dict) for k, v in src.items()}
            )
            or target
            if isinstance(target, dict)
            else target
        ),
        "keys": lambda obj, *_a: (
            [str(k) for k in obj.keys() if not str(k).startswith("__")]
            if isinstance(obj, dict)
            else [str(i) for i in range(len(obj))]
            if isinstance(obj, (list, tuple, str))
            else []
        ),
        "values": lambda obj, *_a: (
            [v for k, v in obj.items() if not str(k).startswith("__")]
            if isinstance(obj, dict)
            else list(obj)
            if isinstance(obj, (list, tuple, str))
            else []
        ),
        "entries": lambda obj, *_a: (
            [[str(k), v] for k, v in obj.items() if not str(k).startswith("__")]
            if isinstance(obj, dict)
            else []
        ),
        "getOwnPropertyNames": lambda obj, *_a: (
            [str(k) for k in obj.keys() if not str(k).startswith("__")]
            if isinstance(obj, dict)
            else []
        ),
        "getPrototypeOf": lambda obj, *_a: obj if isinstance(obj, dict) else {},
    }
    math_ns = {
        "abs": lambda x=0, *_a: abs(_js_to_number(x)),
        "floor": lambda x=0, *_a: math.floor(_js_to_number(x)),
        "ceil": lambda x=0, *_a: math.ceil(_js_to_number(x)),
        "round": lambda x=0, *_a: math.floor(_js_to_number(x) + 0.5),
        "sqrt": lambda x=0, *_a: math.sqrt(max(0.0, _js_to_number(x))),
        "hypot": lambda *vals: math.hypot(*(_js_to_number(v) for v in vals)),
        "max": lambda *vals: max((_js_to_number(v) for v in vals), default=-math.inf),
        "min": lambda *vals: min((_js_to_number(v) for v in vals), default=math.inf),
        "random": lambda *_a: random.random(),
    }
    json_ns = {
        "parse": lambda text, *_a: json.loads(_js_str(text)),
        "stringify": lambda val, *_a: _json_value(val),
    }

    def _string_ctor(val: Any = "", *_a: Any) -> str:
        return "" if val is _UNDEFINED else _js_str(val)

    _string_ctor.fromCharCode = lambda *codes: "".join(  # type: ignore[attr-defined]
        chr(int(_js_to_number(code)) & 0xFFFF) for code in codes
    )

    def _number_ctor(val: Any = 0, *_a: Any) -> float | int:
        num = _js_to_number(val)
        return int(num) if math.isfinite(num) and int(num) == num else num

    _number_ctor.isSafeInteger = lambda val=None, *_a: (  # type: ignore[attr-defined]
        isinstance(val, (int, float))
        and not isinstance(val, bool)
        and math.isfinite(val)
        and int(val) == val
        and abs(int(val)) <= 9_007_199_254_740_991
    )
    _number_ctor.isFinite = lambda val=None, *_a: (  # type: ignore[attr-defined]
        isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val)
    )
    _number_ctor.isNaN = lambda val=None, *_a: (  # type: ignore[attr-defined]
        isinstance(val, float) and math.isnan(val)
    )

    def _array_ctor(*items: Any) -> list[Any]:
        if len(items) == 1 and isinstance(items[0], int) and not isinstance(items[0], bool):
            return [None] * max(0, items[0])
        return list(items)

    _array_ctor.isArray = lambda val, *_a: isinstance(val, list)  # type: ignore[attr-defined]
    _array_ctor.from_ = lambda val, *_a: (  # type: ignore[attr-defined]
        list(val) if isinstance(val, (list, tuple, str)) else []
    )

    def _date_ctor(*_a: Any) -> str:
        return _format_js_date()

    _date_ctor.now = lambda *_a: round(time.time() * 1000)  # type: ignore[attr-defined]

    def _weak_set_ctor(*_a: Any) -> dict[str, Any]:
        items: set[int] = set()
        return {
            "add": lambda obj, *_args: items.add(id(obj)),
            "has": lambda obj, *_args: id(obj) in items,
            "delete": lambda obj, *_args: id(obj) in items and (items.remove(id(obj)) or True),
        }

    window: dict[str, Any] = {
        "crypto": crypto,
        "performance": performance,
        "document": document,
        "navigator": navigator,
        "location": location,
        "screen": screen,
        "history": history,
        "localStorage": local_storage,
        "sessionStorage": local_storage,
        "console": {
            "log": lambda *_a: None,
            "warn": lambda *_a: None,
            "error": lambda *_a: None,
        },
        "SentinelSDK": {},
        "__sentinel_token_pending": [],
        "__sentinel_init_pending": [],
        "__sentinel_script_loads": len(scripts),
        "atob": _atob,
        "btoa": _btoa,
        "Reflect": reflect_ns,
        "Object": object_ns,
        "Math": math_ns,
        "JSON": json_ns,
        "String": _string_ctor,
        "Number": _number_ctor,
        "Boolean": lambda val=False, *_a: bool(
            val and val is not _UNDEFINED and val != 0 and val != ""
        ),
        "Array": _array_ctor,
        "Date": _date_ctor,
        "WeakSet": _weak_set_ctor,
        "addEventListener": lambda *_a: None,
        "removeEventListener": lambda *_a: None,
        "parseInt": lambda text="0", radix=10, *_a: int(
            str(text).strip(), int(radix) if radix else 10
        ),
        "parseFloat": lambda text="0", *_a: _js_to_number(text),
        "encodeURIComponent": lambda text="", *_a: urllib.parse.quote(_js_str(text), safe=""),
        "decodeURIComponent": lambda text="", *_a: urllib.parse.unquote(_js_str(text)),
        "isNaN": lambda val=math.nan, *_a: math.isnan(_js_to_number(val)),
        "isFinite": lambda val=math.nan, *_a: math.isfinite(_js_to_number(val)),
        "setTimeout": lambda fn, _ms=0, *args: fn(*args) if callable(fn) else 0,
        "clearTimeout": lambda *_a: None,
    }
    window["window"] = window
    window["self"] = window
    window["top"] = window
    window["parent"] = window
    window["globalThis"] = window
    return window


class _VMRejection(Exception):
    def __init__(self, encoded: str) -> None:
        super().__init__(encoded)
        self.encoded = encoded


class SentinelVM:
    """Strict register-based bytecode VM for Sentinel Turnstile and SO challenges.

    Never emits fallback error tokens or Python exception strings to the server:
    any undecodable bytecode, unknown opcode, script-version mismatch, or
    unresolved challenge raises ``SentinelCompatibilityError`` before submission.
    """

    def __init__(
        self,
        window_env: dict[str, Any],
        *,
        spec: SentinelSpec = DEFAULT_SENTINEL_SPEC,
    ) -> None:
        self.window_env = window_env
        self.spec = spec

    def execute(self, dx: str, key: str, *, allow_empty_resolve: bool = False) -> str:
        """Decrypt and execute a base64 ``dx`` program with ``key`` (initial proof)."""
        if not isinstance(dx, str) or not dx.strip():
            raise SentinelCompatibilityError("Sentinel challenge bytecode (dx) was empty")
        if not isinstance(key, str) or not key:
            raise SentinelCompatibilityError("Sentinel challenge key was empty")

        opcodes = self.spec.vm_opcodes
        queue_reg = self.spec.vm_queue_register
        window_reg = self.spec.vm_window_register
        key_reg = self.spec.vm_key_register
        max_steps = self.spec.max_vm_steps

        reg: dict[Any, Any] = {}
        step_count = 0
        resolved: list[str] = []

        def invoke(fn_slot: Any, *args: Any) -> Any:
            fn = reg.get(fn_slot)
            if not callable(fn):
                raise SentinelCompatibilityError(
                    f"Unsupported or non-callable Sentinel VM register: {fn_slot!r}"
                )
            return fn(*args)

        def run_queue() -> None:
            nonlocal step_count
            while True:
                queue = reg.get(queue_reg)
                if not isinstance(queue, list) or not queue:
                    break
                step_count += 1
                if step_count > max_steps:
                    raise SentinelCompatibilityError(
                        "Sentinel VM exceeded maximum instruction step limit"
                    )
                instruction = queue.pop(0)
                if not isinstance(instruction, list) or not instruction:
                    raise SentinelCompatibilityError(
                        "Sentinel VM encountered a malformed instruction"
                    )
                op, *args = instruction
                invoke(op, *args)

        def op_recurse(sub_dx: Any, *_a: Any) -> str:
            target_dx = reg.get(sub_dx, sub_dx) if not isinstance(sub_dx, str) else sub_dx
            current_key = _js_str(reg.get(key_reg, key))
            return self.execute(_js_str(target_dx), current_key)

        def op_xor(dst: Any, src_key: Any, *_a: Any) -> None:
            reg[dst] = _xor_strings(_js_str(reg.get(dst, "")), _js_str(reg.get(src_key, "")))

        def op_load_imm(dst: Any, immediate: Any = None, *_a: Any) -> None:
            reg[dst] = immediate

        def op_resolve(val: Any = "", *_a: Any) -> None:
            nonlocal step_count
            step_count = 0
            if val is None or val is _UNDEFINED:
                if not allow_empty_resolve:
                    raise SentinelCompatibilityError(
                        "Sentinel VM attempted to resolve an undefined challenge value"
                    )
                rendered = _js_str(val)
            else:
                rendered = _js_str(val)
                if not rendered and not allow_empty_resolve:
                    raise SentinelCompatibilityError(
                        "Sentinel VM attempted to resolve an empty challenge value"
                    )
            if not resolved:
                resolved.append(_btoa(rendered))

        def op_reject(val: Any = "", *_a: Any) -> None:
            nonlocal step_count
            step_count = 0
            raise _VMRejection(_btoa(_js_str(val)))

        def op_add_or_push(dst: Any, src: Any, *_a: Any) -> None:
            current = reg.get(dst)
            rhs = reg.get(src)
            if isinstance(current, list):
                current.append(rhs)
            elif (
                isinstance(current, (int, float))
                and not isinstance(current, bool)
                and isinstance(rhs, (int, float))
                and not isinstance(rhs, bool)
            ):
                reg[dst] = current + rhs
            else:
                reg[dst] = _js_str(current) + _js_str(rhs)

        def op_getprop(dst: Any, obj_slot: Any, prop_slot: Any, *_a: Any) -> None:
            reg[dst] = _js_getprop(reg.get(obj_slot), reg.get(prop_slot))

        def op_call(fn_slot: Any, *arg_slots: Any) -> Any:
            return invoke(fn_slot, *(reg.get(slot) for slot in arg_slots))

        def op_copy(dst: Any, src: Any, *_a: Any) -> None:
            reg[dst] = reg.get(src)

        def op_match_script(dst: Any, pattern_slot: Any, *_a: Any) -> None:
            pattern = _js_str(reg.get(pattern_slot, ""))
            try:
                regex = re.compile(pattern)
            except re.error as error:
                raise SentinelCompatibilityError(
                    "Sentinel VM script matcher received an invalid pattern"
                ) from error
            scripts = self.window_env.get("document", {}).get("scripts", [])
            matched: str | None = None
            for script in scripts:
                src_url = script.get("src", "") if isinstance(script, dict) else ""
                found = regex.search(src_url)
                if found:
                    matched = found.group(0)
                    break
            if matched is None and "sentinel/" in pattern:
                raise SentinelCompatibilityError(
                    "Sentinel challenge expected a newer SDK script version"
                )
            reg[dst] = matched

        def op_load_reg_map(dst: Any, *_a: Any) -> None:
            reg[dst] = reg

        def op_try_call_raw(err_dst: Any, fn_slot: Any, *raw_args: Any) -> None:
            try:
                invoke(fn_slot, *raw_args)
            except SentinelCompatibilityError:
                raise
            except Exception as error:
                reg[err_dst] = f"Error: {error}"

        def op_json_parse(dst: Any, src: Any, *_a: Any) -> None:
            try:
                reg[dst] = json.loads(_js_str(reg.get(src)))
            except Exception as error:
                raise SentinelCompatibilityError(
                    "Sentinel VM failed to parse JSON register value"
                ) from error

        def op_json_stringify(dst: Any, src: Any, *_a: Any) -> None:
            reg[dst] = _json_value(reg.get(src))

        def op_try_call(dst: Any, fn_slot: Any, *arg_slots: Any) -> None:
            try:
                reg[dst] = invoke(fn_slot, *(reg.get(slot) for slot in arg_slots))
            except SentinelCompatibilityError:
                raise
            except Exception as error:
                reg[dst] = f"Error: {error}"

        def op_atob(slot: Any, *_a: Any) -> None:
            reg[slot] = _atob(_js_str(reg.get(slot)))

        def op_btoa(slot: Any, *_a: Any) -> None:
            reg[slot] = _btoa(_js_str(reg.get(slot)))

        def op_branch_eq(left_slot: Any, right_slot: Any, fn_slot: Any, *raw_args: Any) -> Any:
            if _js_equals(reg.get(left_slot), reg.get(right_slot)):
                return invoke(fn_slot, *raw_args)
            return None

        def op_branch_diff_gt(
            left_slot: Any, right_slot: Any, tol_slot: Any, fn_slot: Any, *raw_args: Any
        ) -> Any:
            diff = abs(_js_to_number(reg.get(left_slot)) - _js_to_number(reg.get(right_slot)))
            if diff > _js_to_number(reg.get(tol_slot)):
                return invoke(fn_slot, *raw_args)
            return None

        def op_try_subqueue(err_dst: Any, subqueue: Any, *_a: Any) -> None:
            if not isinstance(subqueue, list):
                raise SentinelCompatibilityError("Sentinel VM subqueue was not a list")
            saved = list(reg.get(queue_reg) or [])
            reg[queue_reg] = [
                list(instr) if isinstance(instr, list) else instr for instr in subqueue
            ]
            try:
                run_queue()
            except SentinelCompatibilityError:
                raise
            except Exception as error:
                reg[err_dst] = f"Error: {error}"
            finally:
                reg[queue_reg] = saved

        def op_branch_defined(cond_slot: Any, fn_slot: Any, *raw_args: Any) -> Any:
            if reg.get(cond_slot, _UNDEFINED) is not _UNDEFINED:
                return invoke(fn_slot, *raw_args)
            return None

        def op_bind_method(dst: Any, obj_slot: Any, method_slot: Any, *_a: Any) -> None:
            reg[dst] = _js_bind_method(reg.get(obj_slot), reg.get(method_slot))

        def op_noop(*_a: Any) -> None:
            return None

        def op_sub_or_remove(dst: Any, src: Any, *_a: Any) -> None:
            current = reg.get(dst)
            rhs = reg.get(src)
            if isinstance(current, list):
                try:
                    current.remove(rhs)
                except ValueError:
                    pass
            else:
                reg[dst] = _js_to_number(current) - _js_to_number(rhs)

        def op_lt(dst: Any, left_slot: Any, right_slot: Any, *_a: Any) -> None:
            reg[dst] = _js_to_number(reg.get(left_slot)) < _js_to_number(reg.get(right_slot))

        def op_def_subroutine(
            fn_dst: Any, ret_slot: Any, params_or_body: Any, body_or_none: Any = None, *_a: Any
        ) -> None:
            has_params = body_or_none is not None
            param_slots = list(params_or_body) if has_params else []
            body_template = list(body_or_none if has_params else params_or_body)

            def _subroutine(*call_arg_slots: Any) -> Any:
                saved_params: list[tuple[Any, Any]] = []
                if has_params:
                    for idx, p_slot in enumerate(param_slots):
                        saved_params.append((p_slot, reg.get(p_slot, _UNDEFINED)))
                        arg = call_arg_slots[idx] if idx < len(call_arg_slots) else None
                        reg[p_slot] = (
                            reg[arg]
                            if isinstance(arg, int) and not isinstance(arg, bool) and arg in reg
                            else arg
                        )
                saved_queue = list(reg.get(queue_reg) or [])
                reg[queue_reg] = [
                    list(instr) if isinstance(instr, list) else instr for instr in body_template
                ]
                try:
                    run_queue()
                    return reg.get(ret_slot)
                finally:
                    reg[queue_reg] = saved_queue
                    for p_slot, previous in saved_params:
                        if previous is _UNDEFINED:
                            reg.pop(p_slot, None)
                        else:
                            reg[p_slot] = previous

            reg[fn_dst] = _subroutine

        def op_mul(dst: Any, left_slot: Any, right_slot: Any, *_a: Any) -> None:
            reg[dst] = _js_to_number(reg.get(left_slot)) * _js_to_number(reg.get(right_slot))

        def op_div(dst: Any, left_slot: Any, right_slot: Any, *_a: Any) -> None:
            divisor = _js_to_number(reg.get(right_slot))
            reg[dst] = 0.0 if divisor == 0 else _js_to_number(reg.get(left_slot)) / divisor

        handler_by_name: dict[str, Callable[..., Any]] = {
            "recurse": op_recurse,
            "xor": op_xor,
            "load_imm": op_load_imm,
            "resolve": op_resolve,
            "reject": op_reject,
            "add_or_push": op_add_or_push,
            "getprop": op_getprop,
            "call": op_call,
            "copy": op_copy,
            "match_script": op_match_script,
            "load_reg_map": op_load_reg_map,
            "try_call_raw": op_try_call_raw,
            "json_parse": op_json_parse,
            "json_stringify": op_json_stringify,
            "try_call": op_try_call,
            "atob": op_atob,
            "btoa": op_btoa,
            "branch_eq": op_branch_eq,
            "branch_diff_gt": op_branch_diff_gt,
            "try_subqueue": op_try_subqueue,
            "branch_defined": op_branch_defined,
            "bind_method": op_bind_method,
            "noop_25": op_noop,
            "noop_26": op_noop,
            "sub_or_remove": op_sub_or_remove,
            "noop_28": op_noop,
            "lt": op_lt,
            "def_subroutine": op_def_subroutine,
            "mul": op_mul,
            "move": op_copy,
            "div": op_div,
        }
        for name, opcode_id in opcodes.items():
            handler = handler_by_name.get(name)
            if handler is not None:
                reg[opcode_id] = handler

        reg[window_reg] = self.window_env
        reg[key_reg] = key

        try:
            decrypted = _xor_strings(_atob(dx), _js_str(reg[key_reg]))
            program = json.loads(decrypted)
        except SentinelCompatibilityError:
            raise
        except Exception as error:
            raise SentinelCompatibilityError(
                "Sentinel challenge bytecode could not be decoded"
            ) from error

        if (
            not isinstance(program, list)
            or not program
            or any(not isinstance(instr, list) or not instr for instr in program)
        ):
            raise SentinelCompatibilityError(
                "Sentinel challenge bytecode did not contain a valid instruction list"
            )

        reg[queue_reg] = [list(instr) for instr in program]
        try:
            run_queue()
        except SentinelCompatibilityError:
            raise
        except _VMRejection:
            raise SentinelCompatibilityError("Sentinel VM rejected challenge") from None
        except Exception as error:
            raise SentinelCompatibilityError(
                "Sentinel challenge bytecode execution failed"
            ) from error

        if not resolved or not resolved[0]:
            raise SentinelCompatibilityError(
                "Sentinel challenge bytecode completed without resolving a token"
            )
        return resolved[0]
