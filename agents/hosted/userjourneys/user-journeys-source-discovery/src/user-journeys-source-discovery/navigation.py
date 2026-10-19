"""Deterministically extract the navigation graph of an ASP.NET Core web project (MVC controllers and Razor Pages).

Pages are named routes (``RouteNames.X``, controller route constants, or Controller.Action / Razor page paths when
unnamed). Transitions are redirects and links found in action bodies, page handlers and the views each action renders
(including partials). Every transition records each file and line that evidences it. No model is used.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath

# Stubs, health checks and error pages exist for local development or plumbing, not user journeys.
DEV_ONLY = re.compile(r"stub|ping|health|error|pagenotfound|problemwithservice", re.IGNORECASE)
ROUTE_REF = r"((?:RouteNames\.)?[A-Z]\w*(?:\.\w+)*)"
# A route reference in code: nameof(X.Y), a string literal, or a constant (RouteNames.X, RouteConstants.X, X).
CODE_REF = r'(?:nameof\(([\w.]+)\)|"([^"]+)"|' + ROUTE_REF + r")"
# Classes that hold route names: RouteNames, RouteConstants, AccountRoutes, ...
ROUTE_CLASS = re.compile(r"^\w*Route(?:s|Names?|Constants?)$")
METHOD_RE = re.compile(
    r"((?:\s*\[[^\]]*\]\s*)*)\s*public\s+(?:async\s+)?(?:Task<)?"
    r"(?:IActionResult|ActionResult|RedirectResult|ViewResult|RedirectToRouteResult)>?\s+(\w+)\s*\("
)
VIEW_LINK_RE = re.compile(
    r'<(a|form|button)\b[^>]*?asp-route="@?([\w.-]+)"'
    r"|Url\.RouteUrl\s*\(\s*" + ROUTE_REF
    + r'|<(a|form|button)\b[^>]*?asp-page="([^"]+)"'
    r'|<(a|form|button)\b[^>]*?asp-action="(\w+)"(?:[^>]*?asp-controller="(\w+)")?',
    re.S,
)
PARTIAL_RE = re.compile(r'<partial\b[^>]*?\bname="([^"]+)"|Partial(?:Async)?\(\s*"([^"]+)"')
SETUP_AUTH_RE = re.compile(r'AuthorizeFolder\("/"\)|new AuthorizeFilter\(|FallbackPolicy\s*=')


def _line(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _body_end(text: str, start: int) -> int:
    brace, arrow = text.find("{", start), text.find("=>", start)
    if brace == -1 or (arrow != -1 and arrow < brace and ";" in text[arrow:brace]):
        return text.find(";", arrow if arrow != -1 else start) + 1   # expression-bodied action
    depth, i = 0, brace
    while i < len(text):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return i
        i += 1
    return len(text)


class _Extractor:
    def __init__(self, files: dict[str, str], web_root: str):
        root = web_root.rstrip("/") + "/"
        self.root = root
        self.files = {path: text for path, text in files.items()
                      if path.startswith(root) and not any("test" in part.lower() for part in path.split("/"))}
        self.cs = {p: t for p, t in self.files.items() if p.endswith(".cs")}
        # Route-name classes are often in a shared project (e.g. *.Common), so they are read from the whole repository.
        self.all_cs = {p: t for p, t in files.items()
                       if p.endswith(".cs") and not any("test" in part.lower() for part in p.split("/"))}
        self.views = {p: t for p, t in self.files.items() if p.endswith(".cshtml")}
        self.route_values: dict[str, str] = {}
        self.pages: dict[str, dict] = {}
        self.raw: list[dict] = []
        self.view_owners: dict[str, set[str]] = defaultdict(set)
        self.action_index: dict[tuple[str, str], str] = {}
        self.page_files: dict[str, set[str]] = defaultdict(set)   # page id -> files that define its behaviour

    # ---- route names ----
    def collect_route_names(self) -> None:
        for path, text in self.all_cs.items():
            if re.search(r"class\s+\w*Route(?:s|Names?|Constants?)\b", text):
                stack: list[tuple[str, int]] = []
                depth = 0
                for line in text.splitlines():
                    m = re.search(r"\bclass\s+(\w+)", line)
                    if m:
                        stack.append((m.group(1), depth))
                    for c in re.finditer(r'const\s+string\s+(\w+)\s*=\s*(?:"([^"]*)"|nameof\((\w+)\))', line):
                        if not stack:
                            break
                        names = [name for name, _ in stack]
                        names = names[1:] if ROUTE_CLASS.match(names[0]) else names
                        key = ".".join([*names, c.group(1)])
                        self.route_values[key] = c.group(2) if c.group(2) is not None else c.group(3)
                    depth += line.count("{") - line.count("}")
                    while stack and depth <= stack[-1][1] and "}" in line:
                        stack.pop()
            if path not in self.cs:
                continue
            # Some apps keep route names as constants on each controller instead.
            cm = re.search(r"class\s+(\w+Controller)\b", text)
            if cm:
                for name, value, nameof in re.findall(
                        r'const\s+string\s+(\w+)\s*=\s*(?:"([^"]+)"|nameof\((\w+)\))', text):
                    if not re.search(r"[/~]|\.cshtml$", value):   # view paths are not route names
                        self.route_values.setdefault(f"{cm.group(1)}.{name}", value or nameof)

    def route_key(self, ref: str | None) -> str | None:
        if not ref:
            return None
        head, _, rest = ref.partition(".")
        if rest and ROUTE_CLASS.match(head):
            ref = rest
        if ref in self.route_values:
            return ref
        matches = [key for key in self.route_values if key.endswith("." + ref)]
        if len(matches) == 1:
            return matches[0]
        # A string literal is the route name's value, e.g. asp-route="Home" for RouteConstants.Home = "Home".
        by_value = [key for key, value in self.route_values.items() if value == ref]
        return by_value[0] if len(by_value) == 1 else None

    def name_key(self, nameof: str | None, literal: str | None, constant: str | None) -> str | None:
        """The page key for a route name given as nameof(X), a "literal" or a constant (from a CODE_REF match)."""
        if constant or nameof:
            return self.page_key(self.route_key(constant or nameof))
        return self.page_key(self.route_key(literal) or literal)   # an unknown literal is the route name itself

    @staticmethod
    def page_key(key: str | None) -> str | None:
        # XRouteGet / XRoutePost (or XGet, X_Post, ...) name the same screen's GET and POST.
        return (re.sub(r"[_-]?(?:Route)?(?:Get|Post)$", "", key) or key) if key else key

    def target_key(self, ref: str) -> str:
        return self.page_key(self.route_key(ref)) or ref

    # ---- pages ----
    def add_page(self, page_id: str, *, source_file: str, line: int, kind: str, auth, route=None, method=None, controller=None):
        page = self.pages.setdefault(page_id, {
            "id": page_id, "kind": kind, "routes": [], "methods": [], "sourceFile": source_file, "line": line,
            "requiresAuthentication": auth, "controller": controller,
        })
        if route and route not in page["routes"]:
            page["routes"].append(route)
        if method and method not in page["methods"]:
            page["methods"].append(method)
        if auth is False:
            page["requiresAuthentication"] = False
        self.page_files[page_id].add(source_file)

    def transition(self, source: str, target, trigger: str, path: str, line: int) -> None:
        self.raw.append({"from": source, "to": target, "trigger": trigger, "sourceFile": path, "line": line})

    def collect_controllers(self, global_auth: bool) -> None:
        for path, text in self.cs.items():
            cm = re.search(r"((?:\[[^\]]*\]\s*)*)public\s+(?:sealed\s+|abstract\s+)?class\s+(\w+)Controller\b", text)
            if not cm:
                continue
            controller, class_attrs = cm.group(2), cm.group(1)
            if controller.lower() in {"error", "ping", "health"} or DEV_ONLY.search(controller):
                continue
            prefix = (re.search(r'\[Route\("([^"]*)"', class_attrs) or [None, ""])[1]
            class_name = re.search(r"Name\s*=\s*" + CODE_REF, class_attrs)
            class_name = self.name_key(*class_name.groups()) if class_name else None
            class_auth = True if "[Authorize" in class_attrs else (False if "[AllowAnonymous" in class_attrs else None)
            consts = dict(re.findall(r'const\s+string\s+(\w+)\s*=\s*"([^"]+)"', text))
            for m in METHOD_RE.finditer(text, cm.end()):
                attrs, action = m.group(1), m.group(2)
                name = re.search(r"Name\s*=\s*" + CODE_REF, attrs)
                name = self.name_key(*name.groups()) if name else None
                if name is None and class_name and not re.search(r'(?:Route|Http\w+)\("', attrs):
                    name = class_name   # the controller's route name applies to actions without their own template
                page_id = self.page_key(name) or f"{controller}.{action}"
                if DEV_ONLY.search(page_id):
                    continue
                template = re.search(r'(?:Route|Http\w+)\("([^"]*)"', attrs)
                method = (re.search(r"\[Http(Get|Post|Put|Delete)", attrs) or [None, "Get"])[1].upper()
                # No rule anywhere in the repo means unknown: sign-in may still come from a package.
                auth = (True if "[Authorize" in attrs else False if "[AllowAnonymous" in attrs
                        else class_auth if class_auth is not None else (True if global_auth else None))
                route = "/".join(part for part in [prefix, template.group(1) if template else ""] if part) or None
                self.add_page(page_id, source_file=path, line=_line(text, m.start(2)), kind="mvc-action",
                              auth=auth, route=route, method=method, controller=controller)
                self.action_index[(controller, action)] = page_id
                start, end = m.end(), _body_end(text, m.end())
                body = text[start:end]
                for e in re.finditer(r"(RedirectToRoute|Url\.RouteUrl)\s*\(\s*" + CODE_REF, body):
                    target = self.name_key(*e.groups()[1:4])
                    if target:
                        trigger = "redirect" if e.group(1) == "RedirectToRoute" else "link"
                        self.transition(page_id, target, trigger, path, _line(text, start + e.start()))
                for e in re.finditer(r"RedirectToRoute\s*\(([^;]*?\?[^;]*?:[^;]*?)\)\s*;", body):
                    for ref in re.findall(ROUTE_REF, e.group(1)):   # cond ? A : B
                        key = self.page_key(self.route_key(ref))
                        if key:
                            self.transition(page_id, key, "redirect", path, _line(text, start + e.start()))
                for e in re.finditer(r'RedirectToAction\s*\(\s*(?:nameof\((\w+)\)|"(\w+)")(?:\s*,\s*"(\w+)")?', body):
                    self.transition(page_id, ("action", e.group(3) or controller, e.group(1) or e.group(2)), "redirect",
                                    path, _line(text, start + e.start()))
                for v in re.finditer(r"View\(\s*(\"[^\"]+\"|\w+)?", body):
                    arg = v.group(1)
                    if arg and arg.startswith('"'):
                        view = arg.strip('"')
                    elif arg in consts:
                        view = consts[arg]
                    else:
                        view = f"~/Views/{controller}/{action}.cshtml"
                    if not view.endswith(".cshtml"):
                        view = f"~/Views/{controller}/{view}.cshtml"
                    self.view_owners[(self.root + view.lstrip("~/")).lower()].add(page_id)

    def collect_razor_pages(self, global_auth: bool, anonymous_pages: set[str]) -> None:
        pages_root = self.root + "Pages/"
        for path, text in self.views.items():
            if not path.startswith(pages_root) or not text.lstrip().startswith("@page"):
                continue
            page_path = "/" + path[len(pages_root):].removesuffix(".cshtml")
            if DEV_ONLY.search(page_path):
                continue
            route = (re.match(r'\s*@page\s+"([^"]*)"', text) or [None, page_path])[1]
            code_path = path + ".cs"
            code = self.files.get(code_path, "")
            auth = (False if ("[AllowAnonymous" in code or page_path in anonymous_pages)
                    else True if ("[Authorize" in code or global_auth) else None)
            self.add_page(page_path, source_file=path, line=1, kind="razor-page", auth=auth, route=route)
            if code:
                self.page_files[page_path].add(code_path)
            self.view_owners[path.lower()].add(page_path)
            for e in re.finditer(r'RedirectToPage\(\s*"([^"]+)"', code):
                target = e.group(1) if e.group(1).startswith("/") else str(PurePosixPath(page_path).parent / e.group(1))
                self.transition(page_path, ("page", target), "redirect", code_path, _line(code, e.start()))

    # ---- views ----
    def view_links(self, text: str) -> list[tuple]:
        found = []
        for e in VIEW_LINK_RE.finditer(text):
            if e.group(2) or e.group(3):
                target, tag = self.target_key(e.group(2) or e.group(3)), e.group(1) or "a"
            elif e.group(5):
                target, tag = ("page", e.group(5)), e.group(4)
            else:
                target, tag = ("action", e.group(8), e.group(7)), e.group(6)
            found.append((target, "form-post" if tag == "form" else "link", _line(text, e.start())))
        return found

    def links_with_partials(self, path: str, depth: int = 0, seen: set | None = None) -> list[tuple]:
        seen = seen if seen is not None else set()
        if path in seen or depth > 4:
            return []
        seen.add(path)
        text = self.views[path]
        links = [(target, trigger, path, line) for target, trigger, line in self.view_links(text)]
        partials = {PurePosixPath(p).stem.lower().lstrip("_"): p for p in self.views}
        for a, b in PARTIAL_RE.findall(text):
            partial = partials.get(PurePosixPath(a or b).stem.lower().lstrip("_"))
            if partial:
                links += self.links_with_partials(partial, depth + 1, seen)
        return links

    def collect_view_links(self) -> list[dict]:
        site_navigation = []
        for path in self.views:
            if PurePosixPath(path).name.lower() == "_layout.cshtml":   # header/footer links are on every page
                site_navigation += [{"to": t, "trigger": trig, "sourceFile": src, "line": ln}
                                    for t, trig, src, ln in self.links_with_partials(path)]
        for path in self.views:
            owners = self.view_owners.get(path.lower(), set())
            if not owners or PurePosixPath(path).name.lower() == "_layout.cshtml":
                continue
            links = self.links_with_partials(path)
            for owner in owners:
                for target, trigger, src, line in links:
                    if isinstance(target, tuple) and target[0] == "action" and not target[1]:
                        target = ("action", self.pages[owner].get("controller"), target[2])   # same controller
                    self.transition(owner, target, trigger, src, line)
        return site_navigation

    # ---- resolve ----
    def resolve(self, target) -> str | None:
        if isinstance(target, tuple) and target[0] == "action":
            return self.action_index.get((target[1], target[2])) if target[1] else None
        if isinstance(target, tuple) and target[0] == "page":
            # Razor Pages resolve relative names from the current folder upwards; match case-insensitively.
            by_lower = {pid.lower(): pid for pid in self.pages if pid.startswith("/")}
            candidate = PurePosixPath(target[1])
            for folder in [candidate.parent, *candidate.parent.parents]:
                hit = by_lower.get(str(folder / candidate.name).lower()) or by_lower.get(str(folder / candidate.name / "Index").lower())
                if hit:
                    return hit
            return None
        return target if target in self.pages else None


def extract(files: dict[str, str], web_root: str) -> dict:
    """Return {pages, transitions, siteNavigation, unresolved, selfLinkCount} for one web project."""
    x = _Extractor(files, web_root)
    x.collect_route_names()
    setup = "\n".join(x.cs.values())
    global_auth = bool(SETUP_AUTH_RE.search(setup))
    anonymous_pages = set(re.findall(r'AllowAnonymousToPage\("([^"]+)"\)', setup))
    x.collect_controllers(global_auth)
    x.collect_razor_pages(global_auth, anonymous_pages)
    site = x.collect_view_links()

    transitions: list[dict] = []
    seen: dict[tuple, dict] = {}
    unresolved: list[dict] = []
    self_links = 0
    for t in x.raw:
        target = x.resolve(t["to"])
        if target is None and isinstance(t["to"], str) and DEV_ONLY.search(t["to"]):
            continue   # links to error/stub pages are not part of any journey
        if target is None:
            reference = t["to"] if isinstance(t["to"], str) else ".".join(str(part) for part in t["to"][1:] if part)
            unresolved.append({"from": t["from"], "reference": reference, "sourceFile": t["sourceFile"], "line": t["line"]})
            continue
        if target == t["from"]:
            self_links += 1   # e.g. a POST re-showing its own page after a validation error
            continue
        key = (t["from"], target, t["trigger"])
        evidence = f'{t["sourceFile"]}:{t["line"]}'
        if key in seen:
            if evidence not in seen[key]["evidence"]:
                seen[key]["evidence"].append(evidence)
            continue
        seen[key] = {"from": t["from"], "to": target, "trigger": t["trigger"], "condition": None, "evidence": [evidence]}
        transitions.append(seen[key])

    incoming = {t["to"] for t in transitions}
    pages = []
    for page in sorted(x.pages.values(), key=lambda p: p["id"]):
        pages.append({key: page[key] for key in ("id", "kind", "routes", "methods", "sourceFile", "line", "requiresAuthentication")}
                     | {"isEntryPoint": page["id"] not in incoming, "purpose": None})
    site_navigation = []
    for link in site:
        target = x.resolve(link["to"])
        if target and not any(s["to"] == target for s in site_navigation):
            site_navigation.append({"to": target, "sourceFile": link["sourceFile"], "line": link["line"]})
    return {
        "pages": pages,
        "transitions": transitions,
        "siteNavigation": site_navigation,
        "unresolved": unresolved,
        "selfLinkCount": self_links,
        "pageFiles": {pid: sorted(paths) for pid, paths in x.page_files.items()},
    }


def find_web_projects(paths: list[str]) -> list[str]:
    """Project folders (containing a .csproj) with Pages/ or Views/ that are not test projects.

    API projects have Controllers/ too, but no pages, so they are not web projects here.
    """
    projects = {str(PurePosixPath(p).parent) for p in paths if p.endswith(".csproj")}
    web = []
    for project in sorted(projects):
        if any("test" in part.lower() for part in project.split("/")):
            continue
        prefix = project + "/"
        if any(p.startswith(prefix + folder) and p.endswith(".cshtml") for p in paths for folder in ("Pages/", "Views/")):
            web.append(project)
    return web
