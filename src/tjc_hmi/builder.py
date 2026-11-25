"""Generate the greenhouse example using fixed resource slots."""

import copy
import json
from pathlib import Path

from PIL import Image

from .checksum import finalize_page
from .container import HmiProject, atomic_write
from .image import compile_picture, rgb565
from .page import EVENT_PREFIXES, parse_page_data
from .project import validate_project


def control_templates(project):
    templates = {}
    for entry in project.manifest(b"pa"):
        page = parse_page_data(project.payload(entry))
        for block in page.blocks:
            templates.setdefault(block.type_code, block)
    missing = {"y", "t", "m"} - templates.keys()
    if missing:
        raise ValueError(f"Controls template needs page/text/touch types; missing {sorted(missing)}")
    return templates


def clone_control(template, name, index, geometry=None):
    result = copy.deepcopy(template)
    result.set_string("objname", name)
    result.set_int("id", index)
    result.event_tokens = []
    for prefix in EVENT_PREFIXES:
        if any(token.startswith(prefix) for token in template.event_tokens):
            result.set_event(prefix, [])
    if geometry is not None:
        for field in ("x", "y", "w", "h"):
            result.set_int(field, geometry[field])
        result.set_int("endx", geometry["x"] + geometry["w"] - 1)
        result.set_int("endy", geometry["y"] + geometry["h"] - 1)
    return result


def allocate_pictures(project, definitions, directory):
    entries = project.manifest(b"i")
    used, replacements, indices = set(), {}, []
    for definition in definitions:
        image_path = (directory / definition["background"]).resolve()
        if not image_path.is_relative_to(directory.resolve()):
            raise ValueError("Background image must be inside the layout directory")
        with Image.open(image_path) as image:
            image.load()
            if image.size != (480, 272):
                raise ValueError(f"Expected a 480x272 background: {image_path.name}")
            candidates = []
            for colors in (256, 128, 64, 32):
                runtime, source = compile_picture(image, colors)
                candidates = [key for key in entries if key not in used and key + "s" in project.resources
                              and project.resources[key].length >= len(runtime)
                              and project.resources[key + "s"].length >= len(source)]
                if candidates:
                    break
        if not candidates:
            raise ValueError(f"No paired image slots fit {image_path.name}")
        key = min(candidates, key=lambda name: project.resources[name].length)
        replacements[key], replacements[key + "s"] = runtime, source
        used.add(key)
        indices.append(entries.index(key))
    return replacements, indices


def build_project(template, controls, layout_path):
    configuration = json.loads(Path(layout_path).read_text(encoding="utf-8"))
    definitions = configuration["pages"]
    if configuration.get("resolution") != [480, 272] or len(definitions) != 4:
        raise ValueError("This builder supports four 480x272 greenhouse pages")
    if [page["name"] for page in definitions] != [f"page{i}" for i in range(4)]:
        raise ValueError("Layout pages must be ordered page0 through page3")
    templates = control_templates(controls)
    slots = template.manifest(b"pa")
    if len(slots) < 4:
        raise ValueError("Template needs at least four page slots")
    replacements, pictures = allocate_pictures(template, definitions, Path(layout_path).resolve().parent)
    commands = configuration["commands"]
    for index, entry in enumerate(slots):
        page = parse_page_data(template.payload(entry))
        page.page_name = f"page{index}" if index < 4 else f"spare{index}"
        root = clone_control(templates["y"], page.page_name, 0)
        page.blocks = [root]
        if index < 4:
            root.set_int("pic", pictures[index])
            root.set_event("codesloadend-", ["sendme"])
            for field in definitions[index]["fields"]:
                block = clone_control(templates["t"], field["n"], len(page.blocks), field)
                for name, value in {"bco": rgb565(field["bg"]), "pco": rgb565(field["fg"]),
                                    "font": field["font"], "xcen": 2 if field["align"] == "right" else 0,
                                    "ycen": 1, "txt_maxl": 24}.items():
                    block.set_int(name, value)
                block.set_string("txt", "--")
                page.blocks.append(block)
            for hit in definitions[index]["hits"]:
                block = clone_control(templates["m"], hit["n"], len(page.blocks), hit)
                action = hit["act"]
                if action.startswith("page"):
                    event = "page " + action
                else:
                    event = f"printh {int(commands[action]):02x} 00 00 ff ff ff"
                block.set_event("codesup-", [event])
                page.blocks.append(block)
        else:
            root.set_event("codesloadend-", ["page page0"])
        replacements[entry] = finalize_page(page.serialize(), template.resources[entry].length)
    startup = b"baud=115200\r\ndim=100\r\nrecmod=0\r\npage page0\r\n"
    capacity = template.resources["Program.s"].length
    if len(startup) > capacity:
        raise ValueError("Startup script slot is too small")
    replacements["Program.s"] = startup.ljust(capacity, b" ")
    result = template.replace(replacements)
    validate_project(result, greenhouse=True, pictures=True)
    return result


def build_file(template_path, controls_path, layout_path, output):
    if Path(output).resolve() in {Path(template_path).resolve(), Path(controls_path).resolve()}:
        raise ValueError("Output must have a different path from the input templates")
    result = build_project(HmiProject.load(template_path), HmiProject.load(controls_path), layout_path)
    atomic_write(output, result.raw)
    return result

