"""The undocumented parts of ser2netweb's dependencies that it builds on are still there.

ser2netweb subclasses a few classes of its dependencies and reads or replaces their
private attributes: uvicorn's h11 protocol (ser2netweb/web/httpguard.py: the admin UI's
connection limits and deadlines), pyserial-asyncio-fast's serial transport and pyserial's
port classes (ser2netweb/engine/serial_compat.py), pyserial's RFC 2217 port manager
(ser2netweb/engine/protocols/rfc2217.py), and Starlette's StaticFiles, BaseHTTPMiddleware
and Route hooks. SURFACE lists every such part: the class (where ser2netweb imports it
from), each method ser2netweb overrides or calls with the parameters it passes or takes,
and each attribute it reads or replaces. Checked here against the installed release's own
source code (read with ast, nothing of it run), whatever its version: a release that moves
or renames any of it fails here, and the message names the part, the release and what
moved. It runs on the pinned set, the floors (CI's job "minimum dependencies") and the
newest releases (latest-deps.yml), and on Windows, where it also reads the Windows-only
parts of pyserial.

The other direction: every self.<name> and super().<name> those ser2netweb classes use
that they don't define themselves, every overridden method and every module attribute
taken from pyserial's rfc2217 / win32 modules is in SURFACE, so a new use of a private
part comes with its entry here. Also: what httpguard's transport stand-in relies on in
uvicorn's code (the request cycles and the WebSocket upgrade take self.transport as it is
then; FlowControl keeps the real one), that uvicorn serves with the protocol class
Config(http=...) gets, and that ser2netweb imports nothing of websockets (uvicorn uses it).

The hash pins of tests/test_serial_compat.py are the stricter check for pyserial and
pyserial-asyncio-fast (any change to the code an override replaces fails there).

Run: python3 tests/test_dependency_internals.py   (binds nothing)
"""
import ast
import glob
import importlib.util
import os
import sys
from importlib import metadata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from ser2netweb.engine import serial_compat

TRANSPORT_MODULE = serial_compat._saf.__name__  # (without pyserial-asyncio-fast: the copy)


class Part:
    """One class of a dependency ser2netweb builds on.

    ``theirs``: (module, class) whose code makes up the surface, the class first and then
    its bases that define what is used; ``methods``: name -> the parameters after self,
    as ser2netweb passes or takes them ("..., " at the end: these first, more may follow);
    ``attributes``: what ser2netweb reads or replaces on an instance; ``ours``: (file,
    class) of ser2netweb's subclass, whose uses are checked against the lists."""

    def __init__(self, dist, theirs, methods=None, attributes=(), ours=None, used_by=""):
        self.dist, self.theirs = dist, theirs
        self.methods, self.attributes, self.ours = methods or {}, tuple(attributes), ours
        self.used_by = used_by or (f"{ours[0]} ({ours[1]})" if ours else "")

    @property
    def name(self):
        return ".".join(self.theirs[0])


SURFACE = [
    Part("uvicorn", [("uvicorn.protocols.http.h11_impl", "H11Protocol")],
         methods={"__init__": "config, server_state, app_state, ...",  # as uvicorn.Server makes it
                  "connection_made": "transport", "data_received": "data",
                  "on_response_complete": "", "handle_websocket_upgrade": "event",
                  "pause_writing": "", "resume_writing": "", "connection_lost": "exc"},
         attributes=("conn", "flow", "transport", "loop"),
         ours=("ser2netweb/web/httpguard.py", "GuardedH11Protocol")),
    Part("uvicorn", [("uvicorn.protocols.http.flow_control", "FlowControl")],
         methods={"resume_reading": ""}, attributes=("read_paused", "write_paused"),
         used_by="ser2netweb/web/httpguard.py (GuardedH11Protocol.flow)"),
    Part("uvicorn", [("uvicorn.server", "Server")],
         methods={"serve": "sockets"}, attributes=("should_exit", "started"),
         used_by="ser2netweb/runtime.py, ser2netweb/web/httpguard.py (AdminGuard.server)"),
    Part("uvicorn", [("uvicorn.config", "Config")],
         attributes=("backlog", "ssl_version", "ssl_cert_reqs", "ssl_ca_certs", "ssl_ciphers"),
         used_by="ser2netweb/runtime.py, ser2netweb/web/httpguard.py (ssl_context)"),
    Part("pyserial-asyncio-fast", [(TRANSPORT_MODULE, "SerialTransport")],
         methods={"__init__": "loop, protocol, serial_instance", "write": "data",
                  "_write_ready": "", "_write_data": "data", "_poll_read": "",
                  "_poll_write": "", "_read_ready": "", "_remove_writer": "",
                  "_ensure_writer": "", "_maybe_resume_protocol": "",
                  "_maybe_pause_protocol": "", "_fatal_error": "exc, message"},
         attributes=("_write_buffer", "_max_read_size", "_closing", "_has_reader",
                     "_has_writer", "_loop", "_serial", "_poll_wait_time", "_max_out_waiting"),
         ours=("ser2netweb/engine/serial_compat.py", "Transport")),
    Part("pyserial", [("serial.serialposix", "Serial"), ("serial.serialposix", "PlatformSpecific"),
                      ("serial.serialutil", "SerialBase")],
         methods={"open": "", "close": "", "read": "size", "write": "data", "cancel_read": "",
                  "cancel_write": "", "_reconfigure_port": "force_update",
                  "_set_special_baudrate": "baudrate"},
         attributes=("fd", "is_open", "_baudrate", "_bytesize", "_parity", "_stopbits",
                     "in_waiting", "portstr", "timeout", "write_timeout",
                     *serial_compat._PIPES),
         ours=("ser2netweb/engine/serial_compat.py", "PosixSerial")),
    Part("pyserial", [("serial.serialwin32", "Serial"), ("serial.serialutil", "SerialBase")],
         methods={"__init__": "...", "open": "", "write": "data", "out_waiting": "",
                  "flush": "", "cancel_write": "", "_close": ""},
         attributes=("_port_handle", "is_open", "write_timeout"),
         ours=("ser2netweb/engine/serial_compat.py", "WindowsSerial")),
    Part("pyserial", [("serial.rfc2217", "PortManager")],
         methods={"__init__": "serial_port, connection, ...", "filter": "data",
                  "_telnet_process_subnegotiation": "suboption",
                  "_telnet_negotiate_option": "command, option",
                  "_telnet_process_command": "command",
                  "rfc2217_send_subnegotiation": "option, value",
                  "check_modem_lines": "force_notification"},
         attributes=("serial", "mode", "suboption", "telnet_command"),
         ours=("ser2netweb/engine/protocols/rfc2217.py", "_PortManager")),
    Part("pyserial", [("serial.tools.list_ports_linux", "SysFS")], methods={"__init__": "device"},
         used_by="ser2netweb/engine/portlist.py"),
    Part("starlette", [("starlette.staticfiles", "StaticFiles")],
         methods={"__init__": "directory, ...", "get_response": "path, scope"},
         ours=("ser2netweb/web/routes/__init__.py", "StaticAssets")),
    Part("starlette", [("starlette.middleware.base", "BaseHTTPMiddleware")],
         methods={"dispatch": "request, call_next"},
         ours=("ser2netweb/web/server.py", "GuardMiddleware")),
    Part("starlette", [("starlette.routing", "Route")],
         methods={"__init__": "path, endpoint, ..."}, attributes=("app",),
         ours=("ser2netweb/web/starlette_compat.py", "_CopyRoute")),
]

# module attributes of pyserial's that ser2netweb takes: name -> (module, where it's used)
MODULE_NAMES = {
    "serial.rfc2217": ("ser2netweb/engine/protocols/rfc2217.py", "_rfc2217", None),
    # serial_compat.OverlappedWrites calls pyserial's ctypes bindings as w = self._w32
    "serial.win32": ("ser2netweb/engine/serial_compat.py", "w", "OverlappedWrites"),
}


# -------------------------------------------------------------------- reading the code
def _origin(module):
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin, f"{module} is not installed"
    return spec.origin


def _tree(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _classes(tree, name):
    """Every class ``name`` in a module, also those defined in if / try branches (pyserial
    defines its PlatformSpecific once per OS)."""
    out = []

    def walk(body):
        for node in body:
            if isinstance(node, ast.ClassDef):
                if node.name == name:
                    out.append(node)
                walk(node.body)
            elif isinstance(node, (ast.If, ast.Try)):
                walk(node.body)
                walk(node.orelse)
                for handler in getattr(node, "handlers", []):
                    walk(handler.body)
    walk(tree.body)
    return out


def _functions(cls):
    """{name: FunctionDef} of a class body, Windows-only / POSIX-only branches included."""
    out = {}

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(node.name, node)
            elif isinstance(node, ast.If):
                walk(node.body)
                walk(node.orelse)
    walk(cls.body)
    return out


def _params(func):
    """The named parameters after self (positional, then keyword-only)."""
    args = [a.arg for a in func.args.posonlyargs + func.args.args][1:]
    return args + [a.arg for a in func.args.kwonlyargs]


def _self_attributes(cls):
    """Names a class's code sets on self, plus its class-level names."""
    out = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and \
                isinstance(node.value, ast.Name) and node.value.id == "self":
            out.add(node.attr)
    for node in cls.body:
        if isinstance(node, ast.Assign):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def surface_of(part):
    """(methods {name: FunctionDef}, attribute names) of a part's classes, as installed."""
    methods, attributes = {}, set()
    for module, name in part.theirs:
        found = _classes(_tree(_origin(module)), name)
        assert found, f"{module} has no class {name} any more"
        for cls in found:
            for fname, func in _functions(cls).items():
                methods.setdefault(fname, func)
            attributes |= _self_attributes(cls)
    return methods, attributes


def _version(dist):
    if dist == "pyserial-asyncio-fast" and serial_compat.BUILT_IN:
        return "0.16 (ser2netweb's built-in copy)"
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "?"


def uses_of(path, name):
    """What a ser2netweb class uses: (its methods, self.<x> loads, self.<x> stores,
    super().<x> names, its annotated names)."""
    found = _classes(_tree(os.path.join(ROOT, path)), name)
    assert found, f"{path} has no class {name}"
    cls = found[0]
    loads, stores, supers = set(), set(), set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                (stores if isinstance(node.ctx, ast.Store) else loads).add(node.attr)
            elif isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) \
                    and node.value.func.id == "super":
                supers.add(node.attr)
    annotated = {n.target.id for n in cls.body
                 if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    class_names = {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets
                   if isinstance(t, ast.Name)}
    return set(_functions(cls)), loads, stores | class_names, supers, annotated


def names_used_on(path, var, in_class=None):
    """The attributes taken from the name ``var`` in a file (or in one class of it)."""
    tree = _tree(os.path.join(ROOT, path))
    scope = _classes(tree, in_class)[0] if in_class else tree
    return {n.attr for n in ast.walk(scope) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == var}


def module_names(module):
    """The names a module defines at its top level (any branch), imported ones too."""
    out = set()

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            out.add(n.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                out.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                out.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, (ast.If, ast.Try)):
                walk(node.body)
                walk(node.orelse)
                for handler in getattr(node, "handlers", []):
                    walk(handler.body)
    walk(_tree(_origin(module)).body)
    return out


# ------------------------------------------------------------------------------ checks
def test_surface():
    problems, checked = [], 0
    for part in SURFACE:
        where = (f"{part.name} ({part.dist} {_version(part.dist)}; ser2netweb's user: "
                 f"{part.used_by})")
        try:
            methods, attributes = surface_of(part)
        except AssertionError as e:
            problems.append(f"{where}: {e}")
            continue
        for mname, want in part.methods.items():
            checked += 1
            if mname not in methods:
                problems.append(f"{where}: no method {mname}() any more")
                continue
            have = _params(methods[mname])
            names = [p.strip() for p in want.split(",") if p.strip()]
            prefix = names[-1:] == ["..."]
            names = [p for p in names if p != "..."]
            ok = have[:len(names)] == names if prefix else have == names
            if not ok:
                problems.append(f"{where}: {mname}({', '.join(have)}), ser2netweb expects "
                                f"{mname}({want})")
        for attr in part.attributes:
            checked += 1
            if attr not in attributes and attr not in methods:
                problems.append(f"{where}: its code no longer sets self.{attr}")
    assert not problems, ("a dependency moved what ser2netweb builds on: look at the user "
                          "named, then update SURFACE in tests/test_dependency_internals.py:"
                          "\n  " + "\n  ".join(problems))
    print(f"{checked} methods and attributes of {len(SURFACE)} dependency classes are "
          "where ser2netweb expects them  OK")


def test_every_use_is_listed():
    """What ser2netweb's subclasses use of their bases is all in SURFACE."""
    problems = []
    for part in SURFACE:
        if part.ours is None:
            continue
        methods, attributes = surface_of(part)
        theirs = set(methods) | attributes
        mine, loads, stores, supers, annotated = uses_of(*part.ours)
        own = (mine - set(methods)) | (stores - theirs) | (annotated - theirs)
        used = (loads | stores | annotated | supers | (mine & set(methods))) - own
        # (a name ser2netweb sets that its base has too replaces the base's, so it counts)
        used |= stores & theirs
        listed = set(part.methods) | set(part.attributes)
        for name in sorted(used - listed):
            kind = "overrides" if name in mine else "uses"
            problems.append(f"{part.ours[0]} {part.ours[1]} {kind} {part.name}.{name}: "
                            "add it to SURFACE (a method with the parameters it passes or "
                            "takes, else an attribute)")
    for module, (path, var, in_class) in MODULE_NAMES.items():
        missing = names_used_on(path, var, in_class) - module_names(module)
        for name in sorted(missing):
            problems.append(f"{path} takes {module}.{name}, which pyserial "
                            f"{_version('pyserial')} doesn't define")
    assert not problems, "\n  ".join(["SURFACE is incomplete or out of date:", *problems])
    print("every private name ser2netweb's subclasses use is in SURFACE; pyserial's rfc2217 "
          "and win32 modules define every name taken from them  OK")


def test_transport_stand_in():
    """httpguard serves a connection through _Transport (closes held back while a refused
    body still comes in): it replaces H11Protocol.transport after connection_made, which
    works because uvicorn takes self.transport where it needs it."""
    funcs = _functions(_classes(_tree(_origin("uvicorn.protocols.http.h11_impl")),
                                "H11Protocol")[0])
    where = f"uvicorn {_version('uvicorn')}'s H11Protocol"

    def reads_transport(func):
        return any(isinstance(n, ast.Attribute) and n.attr == "transport"
                   and isinstance(n.value, ast.Name) and n.value.id == "self"
                   and isinstance(n.ctx, ast.Load) for n in ast.walk(func))

    made = funcs["connection_made"]
    sets = {n.attr for n in ast.walk(made) if isinstance(n, ast.Attribute)
            and isinstance(n.ctx, ast.Store)}
    assert {"transport", "flow"} <= sets, \
        f"{where}: connection_made no longer sets self.transport and self.flow"
    flow_from_argument = any(
        isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) ==
        "FlowControl" and any(isinstance(a, ast.Name) and a.id == "transport" for a in n.args)
        for n in ast.walk(made))
    assert flow_from_argument, f"{where}: FlowControl is no longer made from the transport given"
    cycle = any(isinstance(n, ast.keyword) and n.arg == "transport"
                and isinstance(n.value, ast.Attribute) and n.value.attr == "transport"
                for func in funcs.values() for n in ast.walk(func))
    assert cycle, f"{where}: no request cycle is given transport=self.transport any more"
    for name in ("handle_websocket_upgrade", "on_response_complete"):
        assert reads_transport(funcs[name]), f"{where}: {name} no longer uses self.transport"
    print("uvicorn's H11Protocol takes self.transport for each request, the WebSocket upgrade "
          "and the end of a response; FlowControl keeps the real one  OK")


def test_config_takes_the_protocol():
    import uvicorn

    class Marker:
        pass

    tree = _tree(os.path.join(ROOT, "ser2netweb", "runtime.py"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "Config"
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "uvicorn"]
    assert len(calls) == 1, "runtime.py makes one uvicorn.Config"
    passed = {k.arg for k in calls[0].keywords}
    init = surface_of(Part("uvicorn", [("uvicorn.config", "Config")]))[0]["__init__"]
    unknown = passed - set(_params(init))
    assert not unknown, (f"uvicorn {_version('uvicorn')}'s Config takes no {sorted(unknown)} "
                         "(ser2netweb/runtime.py passes them)")
    config = uvicorn.Config(lambda scope, receive, send: None, http=Marker, log_config=None)
    config.load()
    assert config.http_protocol_class is Marker, config.http_protocol_class
    # what ser2netweb reads of a Config and a Server, besides SURFACE's lists
    config_reads = (names_used_on("ser2netweb/runtime.py", "uconfig")
                    | names_used_on("ser2netweb/web/httpguard.py", "config"))
    server_reads = names_used_on("ser2netweb/runtime.py", "server")
    listed = {p.theirs[0][1]: set(p.methods) | set(p.attributes) for p in SURFACE
              if p.dist == "uvicorn"}
    assert config_reads <= listed["Config"], sorted(config_reads - listed["Config"])
    assert server_reads <= listed["Server"], sorted(server_reads - listed["Server"])
    flow_reads = {n.attr for n in ast.walk(_tree(os.path.join(ROOT, "ser2netweb", "web",
                                                              "httpguard.py")))
                  if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)
                  and n.value.attr == "flow"}
    assert flow_reads <= listed["FlowControl"], sorted(flow_reads - listed["FlowControl"])
    print(f"uvicorn.Config takes the {len(passed)} settings ser2netweb passes and serves with "
          "the protocol class given as http=  OK")


def test_no_websockets_internals():
    importing = []
    for path in glob.glob(os.path.join(ROOT, "ser2netweb", "**", "*.py"), recursive=True):
        for node in ast.walk(_tree(path)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                     [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            if any(n == "websockets" or n.startswith("websockets.") for n in names):
                importing.append(os.path.relpath(path, ROOT))
    assert not importing, ("ser2netweb imports websockets itself: add what it uses to "
                           f"SURFACE: {importing}")
    import h11
    for name in ("Connection", "Data", "Request", "IDLE", "SEND_BODY", "NEED_DATA",
                 "RemoteProtocolError"):
        assert hasattr(h11, name), f"h11 {_version('h11')} has no {name}"
    for name in ("their_state", "they_are_waiting_for_100_continue", "receive_data",
                 "next_event"):
        assert hasattr(h11.Connection, name), f"h11 {_version('h11')}: Connection.{name}"
    print("websockets: nothing of it is used by ser2netweb (uvicorn serves the WebSockets); "
          "h11: the public names httpguard uses are there  OK")


if __name__ == "__main__":
    test_surface()
    test_every_use_is_listed()
    test_transport_stand_in()
    test_config_takes_the_protocol()
    test_no_websockets_internals()
    print("\nPASS: the dependencies' parts ser2netweb builds on are where it expects them")
