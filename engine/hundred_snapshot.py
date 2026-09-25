"""JSON object-graph checkpoints preserve XI/player aliases without executable data."""
import json
import random
from collections import defaultdict, deque
from engine.format_config import FormatConfig, Phase
from engine.hundred_bowler_manager import HundredBowlerManager
from engine.dls import ResourceLedger
from engine.pressure_engine import PressureEngine
from engine.commentary_engine import CommentaryEngine

_TYPES = {c.__name__: c for c in (FormatConfig, Phase, HundredBowlerManager,
                                ResourceLedger, PressureEngine, CommentaryEngine)}


def serialize(match):
    nodes, seen = [], {}

    def encode(value):
        if value is None or type(value) in (bool, int, float, str):
            return value
        ident = id(value)
        if ident in seen:
            return {"ref": seen[ident]}
        index = len(nodes)
        seen[ident] = index
        node = {}
        nodes.append(node)
        if isinstance(value, dict):
            node.update(kind="dict", items=[[encode(k), encode(v)] for k, v in value.items()])
            if isinstance(value, defaultdict):
                node["factory"] = {int: "int", list: "list", dict: "dict", float: "float", set: "set", deque: "deque"}.get(value.default_factory)
        elif isinstance(value, (list, tuple, set, deque)):
            node.update(kind=type(value).__name__, items=[encode(v) for v in value])
            if isinstance(value, deque):
                node["maxlen"] = value.maxlen
        elif isinstance(value, random.Random):
            node.update(kind="Random", state=encode(value.getstate()))
        elif type(value).__name__ in _TYPES:
            node.update(kind=type(value).__name__, attrs=encode(vars(value)))
        else:
            raise ValueError(f"Unsupported checkpoint value: {type(value).__name__}")
        return {"ref": index}

    state = {k: v for k, v in vars(match).items()
             if k not in ("short_manager_class", "_delivery", "_delivery_guard", "last_accessed") and not callable(v)}
    state["data"] = {k: v for k, v in match.data.items() if k not in ("hundred_snapshot", "super_over_snapshot")}
    state["match_data"] = state["data"]
    root = encode(state)
    return {"version": 1, "root": root, "nodes": nodes}


def restore(match, snapshot):
    if snapshot.get("version") != 1 or not isinstance(snapshot.get("nodes"), list):
        raise ValueError("Unsupported Hundred checkpoint")
    nodes, restored = snapshot["nodes"], {}

    def decode(value):
        if not isinstance(value, dict):
            return value
        index = value["ref"]
        if index in restored:
            return restored[index]
        node = nodes[index]
        kind = node["kind"]
        if kind == "dict":
            factory = {"int": int, "list": list, "dict": dict, "float": float, "set": set, "deque": deque}.get(node.get("factory"))
            obj = defaultdict(factory) if factory else {}
            restored[index] = obj
            obj.update((decode(k), decode(v)) for k, v in node["items"])
        elif kind in ("list", "tuple", "set", "deque"):
            obj = []
            restored[index] = obj
            obj.extend(decode(v) for v in node["items"])
            if kind != "list":
                obj = deque(obj, maxlen=node.get("maxlen")) if kind == "deque" else tuple(obj) if kind == "tuple" else set(obj)
                restored[index] = obj
        elif kind == "Random":
            obj = random.Random()
            restored[index] = obj
            obj.setstate(decode(node["state"]))
        elif kind in _TYPES:
            obj = object.__new__(_TYPES[kind])
            restored[index] = obj
            vars(obj).update(decode(node["attrs"]))
        else:
            raise ValueError("Unknown checkpoint object type")
        return obj

    state = decode(snapshot["root"])
    if state.get("is_hundred") is not True:
        raise ValueError("Checkpoint is not a Hundred match")
    vars(match).update(state)
    match.short_manager_class = HundredBowlerManager
