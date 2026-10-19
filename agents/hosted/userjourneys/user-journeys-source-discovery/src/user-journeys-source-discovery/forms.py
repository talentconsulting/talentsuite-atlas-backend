"""Deterministically find the form fields a Razor view asks for, and what its view model says about them.

Fields come from tag helpers (asp-for on input/select/textarea, GOV.UK <govuk-*> tag helpers), Html.*For helpers
and plain named inputs. Only field definitions are read (name, control, label, fixed options, required, length);
never any values a user enters. Hidden fields and the anti-forgery token are left out.
"""

from __future__ import annotations

import html
import re

MAX_FIELDS_PER_PAGE = 60
INPUT_TYPES = {"text": "text", "email": "email", "tel": "telephone", "number": "number", "password": "password",
               "date": "date", "radio": "radios", "checkbox": "checkboxes", "file": "file", "search": "text", "url": "text"}
GOVUK_CONTROLS = {"input": "text", "radios": "radios", "checkboxes": "checkboxes", "select": "select", "textarea": "textarea",
                  "character-count": "textarea", "date-input": "date", "file-upload": "file"}
HELPER_CONTROLS = {"TextBox": "text", "TextArea": "textarea", "DropDownList": "select", "CheckBox": "checkboxes",
                   "RadioButton": "radios", "Password": "password", "ListBox": "select"}

TAG_RE = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.IGNORECASE)
GOVUK_RE = re.compile(r"<govuk-(input|radios|checkboxes|select|textarea|character-count|date-input|file-upload)\b([^>]*)>",
                      re.IGNORECASE)
HELPER_RE = re.compile(r"Html\.(TextBox|TextArea|DropDownList|CheckBox|RadioButton|Password|ListBox|Hidden)For\s*\(\s*\w+\s*=>\s*\w+\.([\w.\[\]]+)"
                       r'(?:\s*,\s*"([^"]*)")?')
ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
LABEL_RE = re.compile(r'<label\b[^>]*?\basp-for="@?(?:Model\.)?([\w.\[\]]+)"[^>]*>(.*?)</label>', re.IGNORECASE | re.S)
FOR_LABEL_RE = re.compile(r'<label\b[^>]*?\bfor="([^"@]+)"[^>]*>(.*?)</label>', re.IGNORECASE | re.S)
MODEL_RE = re.compile(r"^\s*@model\s+([\w.<>,\s]+?)\s*$", re.MULTILINE)


def _line(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _clean(fragment: str) -> str | None:
    """Visible text of a label: tags and Razor expressions removed, whitespace collapsed."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = re.sub(r"@\([^)]*\)|@[\w.]+(?:\([^)]*\))?", " ", text)
    text = " ".join(html.unescape(text).split())
    text = re.sub(r"\s+([?.,!:;])", r"\1", text)   # removed inline tags leave a gap before punctuation
    return text[:200] or None


def _legend(text: str, position: int) -> str | None:
    """The legend of the fieldset enclosing position, if any."""
    start = text.rfind("<fieldset", 0, position)
    if start == -1 or text.rfind("</fieldset>", start, position) != -1:
        return None
    legend = re.search(r"<legend\b[^>]*>(.*?)</legend>", text[start:position], re.S)
    return _clean(legend.group(1)) if legend else None


def _field_name(value: str) -> str:
    return re.sub(r"^@?(?:Model\.)?", "", value.strip())


def view_fields(text: str) -> list[dict]:
    """Form fields in one view, in document order, each with name, control, label, options and line."""
    fields: dict[str, dict] = {}
    labels = {_field_name(name): _clean(body) for name, body in LABEL_RE.findall(text)}
    labels_by_id = {target: _clean(body) for target, body in FOR_LABEL_RE.findall(text)}

    def add(name: str, control: str, position: int, option: str | None = None, element_id: str | None = None):
        if not name or name.startswith("__") or name.lower() == "__requestverificationtoken":
            return
        by_id = labels_by_id.get(name.replace(".", "_").replace("[", "_").replace("]", "_"))
        if control in {"radios", "checkboxes", "date"}:
            # GOV.UK puts a group's question in the fieldset's legend; a label by id would be the first option's.
            label = _legend(text, position) or labels.get(name)
        else:
            label = labels.get(name) or by_id
        field = fields.setdefault(name, {"name": name, "control": control, "label": label,
                                         "options": [], "line": _line(text, position)})
        if option is not None and option not in [o["value"] for o in field["options"]]:
            option_label = labels_by_id.get(element_id) if element_id else None
            if option_label is None:   # a GOV.UK radio's label usually follows its input
                after = re.match(r"\s*<label\b[^>]*>(.*?)</label>", text[position:position + 600].split(">", 1)[-1], re.S)
                option_label = _clean(after.group(1)) if after else None
            field["options"].append({"value": option, "label": option_label})

    for m in TAG_RE.finditer(text):
        tag, attrs = m.group(1).lower(), dict(ATTR_RE.findall(m.group(2)))
        name = _field_name(attrs.get("asp-for") or attrs.get("name") or "")
        input_type = attrs.get("type", "text").lower()
        if not name or input_type in {"hidden", "submit", "button", "image", "reset"} or "@" in name:
            continue
        control = "select" if tag == "select" else "textarea" if tag == "textarea" else INPUT_TYPES.get(input_type, "text")
        option = attrs.get("value") if control in {"radios", "checkboxes"} else None
        add(name, control, m.start(), option if option and "@" not in option else None, attrs.get("id"))
        if tag == "select":   # fixed <option>s written in the view
            block = text[m.end():text.find("</select>", m.end())]
            for value, body in re.findall(r'<option\b[^>]*?value="([^"@]*)"[^>]*>(.*?)</option>', block, re.S):
                if value:
                    fields[name]["options"].append({"value": value, "label": _clean(body)})
    for m in GOVUK_RE.finditer(text):
        attrs = dict(ATTR_RE.findall(m.group(2)))
        name = _field_name(attrs.get("asp-for") or attrs.get("name") or "")
        if not name or "@" in name:
            continue
        add(name, GOVUK_CONTROLS[m.group(1).lower()], m.start())
        end = text.find(f"</govuk-{m.group(1).lower()}>", m.end())
        block = text[m.end():end if end != -1 else m.end()]
        if not fields[name]["label"]:
            label = re.search(r"<govuk-[\w-]*(?:label|legend)\b[^>]*>(.*?)</govuk-[\w-]*(?:label|legend)>", block, re.S)
            fields[name]["label"] = _clean(label.group(1)) if label else None
        for value, body in re.findall(r'<govuk-(?:radios|checkboxes)-item\b[^>]*?value="([^"@]+)"[^>]*>(.*?)</govuk-(?:radios|checkboxes)-item>', block, re.S):
            fields[name]["options"].append({"value": value, "label": _clean(body)})
    for m in HELPER_RE.finditer(text):
        helper, name, option = m.group(1), m.group(2), m.group(3)
        if helper == "Hidden":
            continue
        add(name, HELPER_CONTROLS[helper], m.start(), option if helper == "RadioButton" else None)
    return sorted(fields.values(), key=lambda field: field["line"])


def view_model_type(text: str) -> str | None:
    """The view's model class name (last segment, generics unwrapped to their item type is not attempted)."""
    m = MODEL_RE.search(text)
    if not m or "<" in m.group(1):
        return None
    return m.group(1).strip().rsplit(".", 1)[-1]


def _class_end(body: str) -> int:
    """Index of the brace closing a class whose body starts just after its opening brace."""
    depth = 1
    for i, char in enumerate(body):
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth == 0:
            return i
    return len(body)


class ModelIndex:
    """View-model properties (type and validation attributes) and FluentValidation validators, by class name."""

    def __init__(self, sources: dict[str, str]):
        self.classes: dict[str, tuple[str, str, str | None]] = {}   # name -> (path, body onwards, base class)
        self.validators: dict[str, str] = {}
        for path, text in sources.items():
            for m in re.finditer(r"\bclass\s+(\w+)(?:<[^>]*>)?\s*(?::\s*([^{]+))?\{", text):
                base = m.group(2) or ""
                first_base = re.match(r"\s*(\w+)", base)
                self.classes.setdefault(m.group(1), (path, text[m.end():], first_base.group(1) if first_base else None))
                validator = re.search(r"AbstractValidator<\s*(\w+)\s*>", base)
                if validator:
                    self.validators.setdefault(validator.group(1), path)

    def properties(self, class_name: str | None, depth: int = 0) -> dict[str, dict]:
        """Public properties declared on a class and its in-repository base classes."""
        if not class_name or class_name not in self.classes or depth > 3:
            return {}
        path, body, base = self.classes[class_name]
        body = body[:_class_end(body)]
        found: dict[str, dict] = self.properties(base, depth + 1) if base else {}
        for m in re.finditer(r"((?:\[[^\]]*\]\s*)*)public\s+(?:virtual\s+|required\s+|override\s+)*([\w<>?,.\[\]\s]+?)\s+(\w+)\s*\{\s*get", body):
            attrs, type_name, name = m.group(1), " ".join(m.group(2).split()), m.group(3)
            info = {"type": type_name, "required": True if re.search(r"\[(?:Required|BindRequired)\b", attrs) else None,
                    "constraints": [], "displayName": None}
            length = re.search(r"\[(?:MaxLength|StringLength)\(\s*(\d+)", attrs)
            if length:
                info["constraints"].append(f"max {length.group(1)} characters")
            if re.search(r"\[EmailAddress\b", attrs):
                info["constraints"].append("email address")
            rng = re.search(r"\[Range\(\s*([\d.-]+)\s*,\s*([\d.-]+)", attrs)
            if rng:
                info["constraints"].append(f"between {rng.group(1)} and {rng.group(2)}")
            display = re.search(r'\[Display\([^)]*Name\s*=\s*"([^"]+)"', attrs)
            if display:
                info["displayName"] = display.group(1)
            found[name] = info
        return found


def describe_fields(fields: list[dict], model: dict[str, dict]) -> list[dict]:
    """Attach view-model facts (type, required, constraints, display name) to extracted fields."""
    described = []
    for field in fields[:MAX_FIELDS_PER_PAGE]:
        prop = model.get(field["name"].split(".")[-1].split("[")[0], {})
        described.append({
            "name": field["name"],
            "control": field["control"],
            "label": field["label"] or prop.get("displayName"),
            "options": field["options"],
            "dataType": prop.get("type"),
            "required": prop.get("required"),
            "constraints": prop.get("constraints", []),
            "validation": None,
            "line": field["line"],
        })
    return described
