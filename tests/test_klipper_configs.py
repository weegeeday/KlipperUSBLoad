"""Validation tests for Klipper .cfg files and Jinja2 macro/menu templates."""

from __future__ import annotations

import configparser
import re
from pathlib import Path
from typing import Any

import jinja2

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_CFG = REPO_ROOT / "config" / "klipper_usb_load.cfg"
LCD_CFG = REPO_ROOT / "config" / "klipper_usb_load_lcd.cfg"


def _parse_klipper_cfg(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    parser.read(path, encoding="utf-8")
    return parser


def _render_klipper_template(template_text: str, context: dict[str, Any]) -> str:
    # Klipper's gcode_macro uses single-brace variable delimiters { ... }
    env = jinja2.Environment(
        block_start_string="{%",
        block_end_string="%}",
        variable_start_string="{",
        variable_end_string="}",
        comment_start_string="{#",
        comment_end_string="#}",
        undefined=jinja2.StrictUndefined,
    )
    rendered_lines: list[str] = []
    for line in template_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rendered_lines.append(line)
    tmpl = env.from_string("\n".join(rendered_lines))
    return tmpl.render(**context)


def test_core_klipper_cfg_sections_and_jinja_render() -> None:
    cfg = _parse_klipper_cfg(CORE_CFG)
    assert cfg.has_section("gcode_macro _USB_LOAD_STATE")
    assert cfg.has_section("gcode_macro _USB_LOAD_SET_STATE")
    assert cfg.has_section("gcode_macro _USB_LOAD_NOTIFY")
    assert cfg.has_section("gcode_macro PRINT_NEWEST_USB")

    errors: list[str] = []

    def action_respond_info(msg: str) -> str:
        return f"// {msg}"

    def action_raise_error(msg: str) -> str:
        errors.append(msg)
        return f"!! {msg}"

    base_ctx = {
        "action_respond_info": action_respond_info,
        "action_raise_error": action_raise_error,
        "params": {
            "FILENAME": "usb/port_1-1.2/benchy.gcode",
            "DISPLAY_NAME": "benchy.gcode",
            "READY": 1,
            "COUNT": 3,
            "STATUS": "Ready: benchy.gcode",
            "MSG": "USB Ready: benchy.gcode",
        },
        "printer": {
            "idle_timeout": {"state": "Idle"},
            "print_stats": {"state": "standby"},
            "gcode_macro _USB_LOAD_STATE": {
                "filename": "usb/port_1-1.2/benchy.gcode",
                "display_name": "benchy.gcode",
                "ready": 1,
                "count": 3,
                "status": "Ready: benchy.gcode",
            },
        },
    }

    set_out = _render_klipper_template(cfg.get("gcode_macro _USB_LOAD_SET_STATE", "gcode"), base_ctx)
    assert 'VALUE="usb/port_1-1.2/benchy.gcode"' in set_out
    assert "VALUE=1" in set_out

    notify_out = _render_klipper_template(cfg.get("gcode_macro _USB_LOAD_NOTIFY", "gcode"), base_ctx)
    assert "M117 USB Ready: benchy.gcode" in notify_out

    print_out = _render_klipper_template(cfg.get("gcode_macro PRINT_NEWEST_USB", "gcode"), base_ctx)
    assert 'SDCARD_PRINT_FILE FILENAME="usb/port_1-1.2/benchy.gcode"' in print_out
    assert errors == []

    # When printing, PRINT_NEWEST_USB must raise error via action_raise_error
    busy_ctx = dict(base_ctx)
    busy_ctx["printer"] = {
        **base_ctx["printer"],
        "idle_timeout": {"state": "Printing"},
        "print_stats": {"state": "printing"},
    }
    _render_klipper_template(cfg.get("gcode_macro PRINT_NEWEST_USB", "gcode"), busy_ctx)
    assert len(errors) == 1


def test_lcd_menu_cfg_expressions() -> None:
    cfg = _parse_klipper_cfg(LCD_CFG)
    assert cfg.has_section("menu __main __print_usb")
    assert cfg.get("menu __main __print_usb", "type") == "command"

    enable_expr = cfg.get("menu __main __print_usb", "enable")
    name_expr = cfg.get("menu __main __print_usb", "name")

    # Extract {expr} from Klipper menu attribute syntax
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    enable_inner = re.sub(r"^\{|\}$", "", enable_expr.strip())
    name_converted = re.sub(r"\{([^}]+)\}", r"{{ \1 }}", name_expr)

    idle_ready_printer = {
        "idle_timeout": {"state": "Ready"},
        "gcode_macro _USB_LOAD_STATE": {
            "ready": 1,
            "filename": "usb/port_1-1.2/very_long_part_name.gcode",
            "display_name": "very_long_part_name.gcode",
        },
    }
    assert env.compile_expression(enable_inner)(printer=idle_ready_printer) is True
    rendered_name = env.from_string(name_converted).render(printer=idle_ready_printer)
    assert rendered_name == "USB: very_long_pa"

    printing_printer = {
        **idle_ready_printer,
        "idle_timeout": {"state": "Printing"},
    }
    assert env.compile_expression(enable_inner)(printer=printing_printer) is False
