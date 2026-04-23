import os
import re
import base64
import mimetypes
import jinja2
from astrbot.api.star import Star
from astrbot.api import logger
from typing import Dict, Any, Optional


class Renderer:
    _jinja_env: Optional[jinja2.Environment] = None

    @classmethod
    def _get_jinja_env(cls) -> jinja2.Environment:
        if cls._jinja_env is None:
            cls._jinja_env = jinja2.Environment(
                autoescape=True,
                keep_trailing_newline=True,
            )
        return cls._jinja_env

    def __init__(self, res_path: str, plugin: Star, render_timeout: int = 30000):
        self.plugin = plugin
        self.res_path = res_path
        self.render_timeout = render_timeout

    def get_res_path(self, sub_path: str) -> str:
        return "file:///" + os.path.abspath(
            os.path.join(self.res_path, sub_path)
        ).replace("\\", "/")

    def get_template(self, name: str) -> str:
        path = os.path.join(self.res_path, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    async def render_html(
        self, template_name: str, data: Dict[str, Any], options: Optional[Dict] = None
    ) -> Optional[str]:
        """Render HTML template to image via AstrBot framework html_render."""
        tmpl_content = self.get_template(template_name)
        if not tmpl_content:
            return None

        adapted = self._adapt_template(tmpl_content)
        adapted = self._inline_assets(adapted)
        html_content = self._render_jinja(adapted, data)
        if not html_content:
            return None

        # Wrap in {% raw %} to prevent the framework t2i service from
        # interpreting any Jinja2-like syntax in the already-rendered HTML
        safe_html = "{% raw %}" + html_content + "{% endraw %}"

        render_options = {
            "full_page": True,
            "type": "png",
            "timeout": self.render_timeout,
        }
        if options:
            render_options.update(options)

        try:
            result = await self.plugin.html_render(
                tmpl=safe_html,
                data={},
                return_url=False,
                options=render_options,
            )
            if result and not self._validate_image(result):
                return None
            return result
        except Exception as e:
            logger.error(f"[Endfield Render] html_render error: {e}")
            return None

    # PNG / JPEG / WebP 文件头魔数
    _IMAGE_SIGNATURES = (
        b"\x89PNG",      # PNG
        b"\xff\xd8\xff",  # JPEG
        b"RIFF",          # WebP (RIFF....WEBP)
    )

    @staticmethod
    def _validate_image(path: str) -> bool:
        """检查文件是否为有效图片（通过文件头魔数）。"""
        if not isinstance(path, str) or not os.path.isfile(path):
            return True  # 非本地路径（如 URL），跳过验证
        try:
            with open(path, "rb") as f:
                header = f.read(12)
            if len(header) < 3:
                logger.error(f"[Endfield Render] 图片文件过小 ({len(header)} bytes): {path}")
                return False
            if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
                return True
            if any(header.startswith(sig) for sig in Renderer._IMAGE_SIGNATURES[:2]):
                return True
            preview = header[:200]
            try:
                text_preview = preview.decode("utf-8", errors="replace")
            except Exception:
                text_preview = repr(preview)
            logger.error(
                f"[Endfield Render] 渲染结果非有效图片: {path}, 文件头: {text_preview}"
            )
            return False
        except OSError as e:
            logger.error(f"[Endfield Render] 读取渲染结果失败: {path}, {e}")
            return False

    def _adapt_template(self, content: str) -> str:
        """Converts Yunzai (art-template) syntax to Jinja2."""
        adapted = content.replace("$index+1", "loop.index").replace(
            "$index", "loop.index0"
        )
        adapted = adapted.replace("$value", "item")

        def fix_condition(match):
            cond = (
                match.group(1)
                .replace("===", "==")
                .replace("!==", "!=")
                .replace("&&", "and")
                .replace("||", "or")
                .replace("null", "none")
                .replace(".length", "|length")
            )
            cond = re.sub(r"!\s*([\w\.]+)", r"not \1", cond)
            return f"{{% if {cond} %}}"

        adapted = re.sub(r"\{\{if\s+(.+?)\}\}", fix_condition, adapted)
        adapted = adapted.replace("{{/if}}", "{% endif %}").replace(
            "{{else}}", "{% else %}"
        )
        adapted = re.sub(
            r"\{\{else if\s+(.+?)\}\}",
            lambda m: fix_condition(m).replace("{% if", "{% elif"),
            adapted,
        )

        def replace_each(match):
            inner = match.group(1).strip().split()
            if len(inner) >= 2:
                return f"{{% for {inner[1]} in {inner[0]} %}}"
            return f"{{% for item in {inner[0]} %}}"

        def replace_interpolation(match):
            content = (
                match.group(1)
                .split("||")[0]
                .replace("&&", "and")
                .replace("null", "none")
                .replace(".length", "|length")
            )
            return "{{" + content + "}}"

        adapted = re.sub(r"\{\{\s*each\s+(.+?)\s*\}\}", replace_each, adapted)
        adapted = adapted.replace("{{/each}}", "{% endfor %}")
        adapted = re.sub(
            r"\{\{@\s*(.+?)\s*\}\}",
            lambda m: (
                "{{"
                + m.group(1)
                .split("||")[0]
                .replace("&&", "and")
                .replace("null", "none")
                .replace(".length", "|length")
                + "|safe}}"
            ),
            adapted,
        )
        adapted = re.sub(r"\{\{([^%\}]+?)\}\}", replace_interpolation, adapted)
        return adapted

    def _inline_assets(self, html: str) -> str:
        """Inlines CSS and Images to ensure rendering works correctly."""

        def inline_css(match):
            path = os.path.join(self.res_path, match.group(1))
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    css_content = f.read()
                css_content = self._adapt_template(css_content)
                return f"<style>\n{css_content}\n</style>"
            return ""

        def inline_image(match):
            path = os.path.join(self.res_path, match.group(1))
            if os.path.exists(path):
                mime = mimetypes.guess_type(path)[0] or "image/png"
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                    return (
                        f'src="data:{mime};base64,{b64}"'
                        if match.group(0).startswith("src")
                        else f"url(data:{mime};base64,{b64})"
                    )
            return match.group(0)

        html = re.sub(
            r'<link\s+rel="stylesheet"\s+href="\{\{(?:_res_path|pluResPath)\}\}([^"]+\.css)">',
            inline_css,
            html,
        )
        html = re.sub(
            r'src="\{\{(?:_res_path|pluResPath)\}\}([^"]+\.(?:png|jpg|jpeg|gif|svg|webp))"',
            inline_image,
            html,
        )
        html = re.sub(
            r'url\(\s*[\'"]?\{\{(?:_res_path|pluResPath)\}\}([^)"\']+?)[\'"]?\s*\)',
            inline_image,
            html,
        )

        def inline_style_bg(m):
            path = os.path.join(self.res_path, m.group(1))
            if os.path.exists(path):
                mime = mimetypes.guess_type(path)[0] or "image/png"
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                return f"url(data:{mime};base64,{b64})"
            return m.group(0)

        html = re.sub(
            r"url\(\{\{(?:pluResPath|_res_path)\}\}([^)]+)\)", inline_style_bg, html
        )
        return html

    def _render_jinja(self, template_str: str, data: Dict[str, Any]) -> Optional[str]:
        """Renders the adapted template with data using the shared Jinja2 Environment."""
        try:
            env = self._get_jinja_env()
            data_copy = data.copy()
            data_copy["_res_path"] = data_copy.get("pluResPath", "X")
            return env.from_string(template_str).render(**data_copy)
        except Exception as e:
            logger.error(f"[Endfield Render] Jinja2 error: {e}")
            return None

    async def close(self):
        pass
