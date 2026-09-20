"""Статические гарантии дизайн-системы: токены, контраст WCAG, ассеты, таблицы на 320px.

Браузерный axe-прогон в CI дорогой и падает на первом же нарушении, поэтому
базовые инварианты палитры проверяются здесь — быстро и с внятной причиной.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

CSS_DIR = Path(__file__).resolve().parents[1] / "static" / "lms" / "css"
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "lms"

# Ступени текста: обязаны держать AA (4.5:1) на любой светлой поверхности проекта.
TEXT_TOKENS = [
    "--text",
    "--text-muted",
    "--text-subtle",
    "--link",
    "--link-hover",
    "--accent-600",
    "--accent-700",
    "--accent-800",
    "--accent-900",
    "--neutral-ink",
    "--info-ink",
    "--success-ink",
    "--warning-ink",
    "--danger-ink",
    "--sand-800",
]
# Инверсный текст: белый на тёмном акценте кнопок и активных вкладок.
INVERSE_TOKENS = ["--on-accent", "--text-inverse"]
INVERSE_BACKGROUNDS = ["--accent", "--accent-700", "--accent-800", "--accent-900"]
# Светлые поверхности, на которых может оказаться текст (включая тинты статусов).
LIGHT_SURFACES = [
    "--surface",
    "--surface-raised",
    "--canvas",
    "--canvas-alt",
    "--surface-sunken",
    "--border-subtle",
    "--accent-50",
    "--accent-100",
    "--sand-100",
    "--neutral-bg",
    "--info-bg",
    "--success-bg",
    "--warning-bg",
    "--danger-bg",
]
# Декор: маркеры списков, иконки, разделители крошек (в шаблонах — aria-hidden).
# Для них действует порог 3:1 для графики, а не 4.5:1 для текста.
DECORATIVE_SELECTORS = {
    "li::marker",
    ".card-title .icon",
    ".tree-icon",
    ".empty-icon",
    ".search .icon",
    ".task-dot",
    ".crumbs .sep",
}
AA_TEXT = 4.5
AA_GRAPHICS = 3.0


def parse_tokens():
    """Словарь токенов из tokens.css с раскрытием var(...) и переводом в RGB."""
    raw = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", _read("tokens.css")))

    def resolve(value, depth=0):
        value = value.strip()
        if depth > 8:
            return value
        reference = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value)
        if reference:
            return resolve(raw.get(reference.group(1), ""), depth + 1)
        return value

    return {name: resolve(value) for name, value in raw.items()}


def _read(name):
    return (CSS_DIR / name).read_text(encoding="utf-8")


def _rgb(value):
    match = re.fullmatch(r"#([0-9a-f]{6}|[0-9a-f]{3})", value.strip(), re.IGNORECASE)
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    return tuple(int(digits[index : index + 2], 16) for index in (0, 2, 4))


def _luminance(rgb):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second):
    """Контраст по WCAG 2.1; None, если хотя бы один цвет не hex."""
    left, right = _rgb(first), _rgb(second)
    if not left or not right:
        return None
    first_lum, second_lum = _luminance(left), _luminance(right)
    lighter, darker = max(first_lum, second_lum), min(first_lum, second_lum)
    return (lighter + 0.05) / (darker + 0.05)


def color_rules():
    """Все объявления `color:` с селектором, размером шрифта и фоном того же правила."""
    found = []
    for path in sorted(CSS_DIR.glob("*.css")):
        stack = []
        buffer = ""
        line = 1
        for raw_line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            buffer += raw_line
            while True:
                if "{" in buffer:
                    head, buffer = buffer.split("{", 1)
                    stack.append((head.strip().split(";")[-1].strip(), line))
                elif "}" in buffer:
                    body, buffer = buffer.split("}", 1)
                    if stack:
                        selector, start = stack.pop()
                        declaration = re.search(r"(?<![-\w])color:\s*([^;]+);", body)
                        if declaration:
                            found.append(
                                {
                                    "file": f"{path.name}:{start}",
                                    "selector": selector,
                                    "value": declaration.group(1).strip(),
                                    "font_size": re.search(r"font-size:\s*([^;]+);", body),
                                    "background": re.search(
                                        r"background(?:-color)?:\s*([^;]+);", body
                                    ),
                                }
                            )
                else:
                    break
            line += 1
    return found


class DesignTokenTests(SimpleTestCase):
    def test_every_used_custom_property_is_defined(self):
        defined = set()
        used = {}
        for path in sorted(CSS_DIR.glob("*.css")):
            text = path.read_text(encoding="utf-8")
            defined.update(re.findall(r"(--[a-z0-9-]+)\s*:", text))
            for match in re.finditer(r"var\((--[a-z0-9-]+)", text):
                line = text[: match.start()].count("\n") + 1
                used.setdefault(match.group(1), []).append(f"{path.name}:{line}")
        missing = {name: places for name, places in used.items() if name not in defined}
        self.assertEqual(
            missing,
            {},
            f"Неопределённые CSS-переменные молча ломают стиль (цвет/радиус/фон): {missing}",
        )

    def test_text_tokens_meet_aa_on_every_light_surface(self):
        tokens = parse_tokens()
        failures = []
        for token in TEXT_TOKENS:
            for surface in LIGHT_SURFACES:
                ratio = contrast(tokens[token], tokens[surface])
                self.assertIsNotNone(ratio, f"{token} или {surface} не hex-цвет")
                if ratio < AA_TEXT:
                    failures.append(
                        f"{token} {tokens[token]} на {surface} {tokens[surface]} = {ratio:.2f}"
                    )
        self.assertEqual(failures, [], "Текст ниже WCAG AA 4.5:1: " + "; ".join(failures))

    def test_inverse_text_meets_aa_on_accent(self):
        tokens = parse_tokens()
        for token in INVERSE_TOKENS:
            for background in INVERSE_BACKGROUNDS:
                ratio = contrast(tokens[token], tokens[background])
                self.assertGreaterEqual(
                    ratio,
                    AA_TEXT,
                    f"{token} на {background} = {ratio:.2f} — ниже AA",
                )

    def test_low_contrast_colors_are_declared_decorative(self):
        """Любой `color:` ниже 4.5:1 обязан быть декором из явного списка (порог 3:1).

        Тест ловит ситуацию, когда новый приглушённый стиль уезжает в текст:
        тогда его нужно либо затемнить до AA, либо осознанно добавить в список
        декора и пометить в шаблоне aria-hidden.
        """
        tokens = parse_tokens()
        offenders = []
        for rule in color_rules():
            reference = re.fullmatch(r"var\((--[a-z0-9-]+)\)", rule["value"])
            name = reference.group(1) if reference else None
            color = tokens.get(name) if name else rule["value"]
            if not color:
                continue
            if name in INVERSE_TOKENS or color.lower() in {"#fff", "#ffffff"}:
                # Инверсный текст живёт на тёмном акценте — проверяется отдельным тестом.
                continue
            surfaces = LIGHT_SURFACES
            background = rule["background"]
            if background:
                token_name = re.fullmatch(r"var\((--[a-z0-9-]+)\)", background.group(1).strip())
                if token_name and tokens.get(token_name.group(1)):
                    surfaces = [token_name.group(1)]
            worst = min(
                (contrast(color, tokens[surface]) for surface in surfaces),
                default=None,
            )
            if worst is None or worst >= AA_TEXT:
                continue
            if rule["selector"] not in DECORATIVE_SELECTORS:
                offenders.append(
                    f"{rule['file']} {rule['selector']} → {rule['value']} = {worst:.2f}"
                )
                continue
            self.assertGreaterEqual(
                worst,
                AA_GRAPHICS,
                f"Декор {rule['selector']} ниже порога графики 3:1 ({worst:.2f})",
            )
        self.assertEqual(offenders, [], "Текст с контрастом ниже AA: " + "; ".join(offenders))


class DesignAssetTests(SimpleTestCase):
    def test_base_template_uses_design_system_and_vendored_htmx(self):
        base = _base_template()
        for asset in [
            "lms/css/tokens.css",
            "lms/css/base.css",
            "lms/css/components.css",
            "lms/css/layout.css",
            "lms/js/lms.js",
            "lms/vendor/htmx.min.js",
        ]:
            self.assertIn(asset, base, f"В base.html нет ссылки на {asset}")
            self.assertTrue((CSS_DIR.parents[1] / asset).is_file(), f"Файл {asset} отсутствует")

    def test_monolithic_stylesheet_is_gone(self):
        self.assertFalse((CSS_DIR / "app.css").exists())
        self.assertNotIn("app.css", _base_template())

    def test_htmx_is_vendored_without_build_step(self):
        vendor = CSS_DIR.parent / "vendor" / "htmx.min.js"
        self.assertTrue(vendor.is_file())
        self.assertGreater(vendor.stat().st_size, 10_000)
        self.assertFalse((CSS_DIR.parents[2] / "webpack.config.js").exists())
        self.assertFalse((CSS_DIR.parents[2] / "vite.config.js").exists())

    def test_tables_are_wrapped_for_narrow_screens(self):
        """На 320px таблица обязана скроллиться в обёртке, а не растягивать страницу."""
        offenders = []
        for path in sorted(TEMPLATE_DIR.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<table\b", text):
                before = text[max(0, match.start() - 400) : match.start()]
                if "table-scroll" not in before:
                    offenders.append(f"{path.name}:{text[: match.start()].count(chr(10)) + 1}")
        self.assertEqual(offenders, [], f"<table> без обёртки .table-scroll: {offenders}")
        self.assertIn("overflow-x: auto", _read("components.css"))

    def test_narrow_screen_guards_are_present(self):
        layout = _read("layout.css")
        self.assertIn("overflow-wrap: anywhere", layout)
        self.assertIn("min-width: 0", layout)
        # Сетки не могут требовать трек шире контейнера — иначе overflow на 320px.
        for path in sorted(CSS_DIR.glob("*.css")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"minmax\(([^,]+),", text):
                track = match.group(1).strip()
                pixels = re.fullmatch(r"(\d+)px", track)
                if pixels:
                    self.assertLessEqual(
                        int(pixels.group(1)),
                        288,
                        f"{path.name}: трек {track} шире содержимого на 320px — "
                        "используйте minmax(min(100%, Npx), 1fr)",
                    )

    def test_motion_and_focus_are_respected(self):
        base = _read("base.css")
        self.assertIn("prefers-reduced-motion", base)
        self.assertIn(":focus-visible", base)


def _base_template():
    return (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
